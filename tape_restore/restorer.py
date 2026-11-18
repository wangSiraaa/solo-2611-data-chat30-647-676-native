"""The file restorer.

Responsibilities:

* follow a strategy (``file`` finish-one-at-a-time or ``volume`` aggregate-sweep),
* fetch fragment bodies *only* through :class:`device.DeviceManager`,
* meter every staged byte through :class:`staging.ScratchLedger`,
* verify each fragment's SHA256 + structural length, then the whole file's
  SHA256, and only then publish atomically to the output directory,
* journal confirmed fragments and checkpoint device positions at block
  boundaries so an interrupted run can resume with a real, re-verified head
  position -- never trusting an unfinished log line.

Nothing reads the synthetic media file directly on the restore path; planning
may inspect volume structure, but body bytes come from ``drive.read``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .device import DeviceManager, Drive, EndOfTape, MediaLibrary, PROVENANCE
from .errors import (
    CatalogError,
    EndOfTape as _EOT,
    FragmentValidationError,
    MediaError,
    QuotaExceeded,
    RestoreError,
    StagingError,
    StateConflict,
)
from .journal import Journal, atomic_write_json, new_run_id
from .model import Catalog, FileEntry, Fragment
from .planner import Plan, build_plan, predict_file_strategy, predict_volume_strategy
from .staging import ScratchLedger, Workspace

# file statuses
COMPLETE = "complete"
FAILED_FRAGMENT = "failed_fragment"
FAILED_MEDIA = "failed_media"
FAILED_VERIFY = "failed_verify"
SKIPPED_QUOTA = "skipped_quota"
INCOMPLETE = "incomplete"
NOT_STARTED = "not_started"


class Interrupted(RestoreError):
    code = "interrupted"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def catalog_fingerprint(catalog: Catalog) -> str:
    """Stable digest of the *structure* (not the media) of the catalog."""
    return _sha256_bytes(catalog.to_json().encode())


def fragment_offset(fe: FileEntry, frag: Fragment) -> int:
    off = 0
    for f in fe.fragments:
        if f.fragment_id == frag.fragment_id:
            return off
        off += f.valid_bytes
    raise RestoreError("fragment not part of file", file_id=fe.file_id,
                       fragment_id=frag.fragment_id)


@dataclass
class FileOutcome:
    file_id: str
    path: str
    total_length: int
    status: str = NOT_STARTED
    reason: Optional[str] = None
    published_path: Optional[str] = None
    fragments_confirmed: list[str] = field(default_factory=list)
    fragments_failed: list[str] = field(default_factory=list)
    bytes_published: int = 0
    verified: bool = False
    lease_id: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "path": self.path,
            "total_length": self.total_length,
            "status": self.status,
            "reason": self.reason,
            "published_path": self.published_path,
            "fragments_confirmed": list(self.fragments_confirmed),
            "fragments_failed": list(self.fragments_failed),
            "bytes_published": self.bytes_published,
            "verified": self.verified,
        }


class TapeScheduler:
    """Keeps up to two volumes mounted, mounts on demand, moves the head legally.

    Volumes are pinned while being swept and evicted least-recently-used only
    when a new volume must be mounted and both drives are busy.  All movement
    uses ``rewind`` then forward-only ``skip``; there is no seek.
    """

    def __init__(self, device: DeviceManager, drive_ids: Iterable[str] = ("D0", "D1")) -> None:
        self.device = device
        self.drive_ids = list(drive_ids)
        self.available: dict[str, bool] = {}
        self.mounted: dict[str, str] = {}
        self.pin: set[str] = set()
        self._touch_seq = 0
        self._touched: dict[str, int] = {}
        self.mount_events: list[dict] = []

    # -- availability ----------------------------------------------------
    def check_volume(self, vid: str) -> bool:
        if vid in self.available:
            return self.available[vid]
        try:
            self.device.library.load(vid)
            self.available[vid] = True
        except (MediaError, RestoreError):
            self.available[vid] = False
        return self.available[vid]

    # -- mounting --------------------------------------------------------
    def _drive_state(self, did: str) -> Drive:
        return self.device.drives[did]

    def sync(self) -> None:
        """Adopt volumes already mounted on the device (e.g. after a resume's
        position verification) so the scheduler and real drives agree."""
        for did in self.drive_ids:
            drive = self._drive_state(did)
            if drive.occupied and drive.volume_id:
                vid = drive.volume_id
                owner = self.mounted.get(vid)
                if owner is None:
                    self.mounted[vid] = did
                    self._touch(vid)
                elif owner != did:
                    # same volume mounted on two drives would be illegal; keep
                    # the recorded owner and leave the stray one alone
                    pass
        for vid in list(self.mounted):
            did = self.mounted[vid]
            d = self._drive_state(did)
            if not d.occupied or d.volume_id != vid:
                self.mounted.pop(vid, None)

    def _touch(self, vid: str) -> None:
        self._touch_seq += 1
        self._touched[vid] = self._touch_seq

    def _free_drive(self) -> Optional[str]:
        for did in self.drive_ids:
            if not self._drive_state(did).occupied:
                return did
        return None

    def _evictable(self) -> Optional[str]:
        candidates = [
            vid for vid in self.mounted
            if vid not in self.pin
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda v: self._touched.get(v, 0))

    def ensure(self, vid: str) -> str:
        self.sync()
        if vid in self.mounted:
            self._touch(vid)
            drive = self._drive_state(self.mounted[vid])
            if drive.occupied and drive.volume_id == vid:
                return drive.drive_id
            self.mounted.pop(vid, None)
        did = self._free_drive()
        if did is None:
            victim = self._evictable()
            if victim is None:
                raise StagingError(
                    "no drive can be freed to mount volume (all pinned)",
                    volume_id=vid, mounted=sorted(self.mounted),
                )
            vdrive = self.mounted.pop(victim)
            self.device.unmount(vdrive)
            did = vdrive
        try:
            self.device.mount(did, vid)
        except (MediaError, RestoreError) as exc:
            self.available[vid] = False
            # device leaves the drive nominally occupied after a bad mount
            d = self._drive_state(did)
            if d.occupied:
                try:
                    self.device.unmount(did)
                except Exception:
                    pass
            self.mount_events.append(
                {"volume_id": vid, "drive_id": did, "ok": False, "error": exc.code}
            )
            raise
        self.mounted[vid] = did
        self._touch(vid)
        self.mount_events.append({"volume_id": vid, "drive_id": did, "ok": True})
        return did

    def unmount(self, vid: str) -> None:
        did = self.mounted.pop(vid, None)
        if did is None:
            return
        self.device.unmount(did)

    def release_all(self) -> None:
        for vid in list(self.mounted):
            try:
                self.unmount(vid)
            except Exception:
                pass
        # sweep any drive the scheduler did not track
        for did in self.drive_ids:
            d = self._drive_state(did)
            if d.occupied:
                try:
                    self.device.unmount(did)
                except Exception:
                    pass
        self.mounted.clear()

    # -- reading ---------------------------------------------------------
    def read_blocks(self, vid: str, start: int, count: int) -> list:
        did = self.ensure(vid)
        drive = self._drive_state(did)
        pos = drive.position
        if pos > start:
            self.device.rewind(did)
            pos = 0
        if pos < start:
            self.device.skip(did, start - pos)
        out = []
        for expected in range(start, start + count):
            got = self.device.read(did)
            if got.block_index != expected:  # pragma: no cover - device guarantees
                raise RestoreError(
                    "device returned unexpected block",
                    expected=expected, got=got.block_index,
                )
            out.append(got)
        return out

    def position(self, vid: str) -> Optional[int]:
        did = self.mounted.get(vid)
        if did is None:
            return None
        return self.device.drives[did].position


class RestoreEngine:
    def __init__(
        self,
        catalog: Catalog,
        library: MediaLibrary,
        workspace: Workspace,
        *,
        quota: int,
        strategy: str = "file",
        run_id: Optional[str] = None,
        block_size: int = 1024,
        interrupt_after: Optional[int] = None,
        clock: Optional[Callable[[], float]] = None,
        initial_peak: int = 0,
    ) -> None:
        self.catalog = catalog
        self.library = library
        self.workspace = workspace
        self.quota = quota
        self.strategy = strategy
        self.run_id = run_id or new_run_id()
        self.block_size = block_size
        self.interrupt_after = interrupt_after
        self._clock = clock or time.time

        self.journal = Journal(workspace.state_dir, self.run_id)
        self.device = DeviceManager(library, drive_ids=("D0", "D1"))
        self.scheduler = TapeScheduler(self.device)
        self.ledger = ScratchLedger(workspace.work_dir, quota, initial_peak=initial_peak)
        self.outcomes: dict[str, FileOutcome] = {}
        self.quota_steps: list[dict] = []
        self._confirmed: dict[str, dict[str, dict]] = {}
        self._confirmed_total = 0
        self._interrupted = False

    # -- public entry points --------------------------------------------
    def restore(self, file_ids: Optional[Iterable[str]] = None,
                *, allow_fallback: bool = False) -> dict:
        started = self._clock()
        self._prepare_run(file_ids)
        plan = build_plan(
            self.catalog, self.library, strategy=self.strategy,
            file_ids=self.requested_ids, quota=self.quota,
        )
        effective = self.strategy
        if not plan.fits and self.strategy == "volume":
            alt = predict_file_strategy(
                self.catalog, self._selected_files(), self.quota,
                self.catalog.volume_block_counts,
            )
            if allow_fallback and alt.fits:
                effective = "file"
                plan = alt
                self.journal.append("strategy_fallback", requested="volume", effective="file")
            else:
                self.journal.append("plan_infeasible", strategy="volume",
                                    reasons=plan.infeasible)
        self.plan = plan
        self._write_meta(plan, effective)

        try:
            if effective == "volume":
                self._run_volume_major()
            else:
                self._run_file_major()
        except Interrupted:
            self._note_interrupt()
            raise
        finally:
            self.scheduler.release_all()
            self.journal.close()
        return self._result(started, effective)

    def _prepare_run(self, file_ids: Optional[Iterable[str]]) -> None:
        if file_ids is None:
            self.requested_ids = self.catalog.file_ids()
        else:
            self.requested_ids = list(dict.fromkeys(file_ids))
        missing = [f for f in self.requested_ids if f not in self.catalog.files]
        if missing:
            raise RestoreError("unknown file_id(s) requested", file_ids=missing)
        for fid in self.requested_ids:
            fe = self.catalog.files[fid]
            self.outcomes[fid] = FileOutcome(fid, fe.path, fe.total_length)

    def _selected_files(self) -> list[FileEntry]:
        return [self.catalog.files[f] for f in self.requested_ids]

    def _write_meta(self, plan: Plan, effective: str) -> None:
        self.journal.write_meta({
            "run_id": self.run_id,
            "strategy_requested": self.strategy,
            "strategy_effective": effective,
            "quota_bytes": self.quota,
            "block_size": self.block_size,
            "catalog_fingerprint": catalog_fingerprint(self.catalog),
            "volume_digests": self._volume_digests(),
            "requested_files": self.requested_ids,
            "provenance": PROVENANCE,
            "started_at": round(self._clock(), 6),
        })

    def _volume_digests(self) -> dict[str, str]:
        out = {}
        for vid in self.catalog.volume_ids():
            try:
                out[vid] = self.library.image_digest(vid)
            except Exception:
                out[vid] = "unavailable"
        return out

    # -- file-major ------------------------------------------------------
    def _run_file_major(self) -> None:
        order = self.plan.file_ids
        for fid in order:
            fe = self.catalog.files[fid]
            if self._interrupted:
                return
            self._restore_file_direct(fe)

    def _restore_file_direct(self, fe: FileEntry) -> None:
        outcome = self.outcomes[fe.file_id]
        if fe.total_length == 0:
            self._publish_empty(fe, outcome)
            return
        if fe.total_length > self.quota:
            outcome.status = SKIPPED_QUOTA
            outcome.reason = (
                f"incomplete target needs {fe.total_length} B but quota is {self.quota} B"
            )
            self.journal.append(
                "file_skipped_quota", file_id=fe.file_id,
                total_length=fe.total_length, quota_bytes=self.quota,
            )
            return

        lease = self.ledger.reserve(
            owner=f"file:{fe.file_id}", size=fe.total_length,
            label=f"target:{fe.file_id}",
        )
        self.ledger.preallocate(lease)
        outcome.lease_id = lease
        self._quota_step("target_open", fe.file_id, None, lease)
        confirmed: dict[str, dict] = {}

        # Visit fragments grouped by volume, blocks ascending, to minimise mounts.
        groups: dict[str, list[Fragment]] = {}
        for frag in fe.fragments:
            groups.setdefault(frag.volume_id, []).append(frag)
        volume_order = sorted(groups, key=lambda v: (min(f.start_block for f in groups[v]), v))

        failure = None
        for vid in volume_order:
            if self._interrupted:
                return
            for frag in sorted(groups[vid], key=lambda f: f.start_block):
                if frag.fragment_id in confirmed:
                    continue
                try:
                    self._stream_fragment(fe, frag, lease, confirmed)
                except FragmentValidationError as exc:
                    failure = (FAILED_FRAGMENT, exc)
                except (MediaError, RestoreError) as exc:
                    failure = (FAILED_MEDIA, exc)
                if failure:
                    break
                self._maybe_save_checkpoint(fe, lease, confirmed)
            if failure:
                break

        if failure:
            status, exc = failure
            outcome.status = status
            outcome.reason = exc.message
            outcome.fragments_confirmed = sorted(confirmed)
            self.journal.append(
                "file_failed", file_id=fe.file_id, status=status,
                reason=exc.message, detail=exc.detail,
            )
            self.ledger.release(lease, reason="file_failed")
            self._quota_step("target_release", fe.file_id, None, None)
            return

        # whole-file verification from the metered target
        actual = self.ledger.hash_file(lease)
        if actual != fe.sha256:
            outcome.status = FAILED_VERIFY
            outcome.reason = "whole-file sha256 mismatch after reassembly"
            outcome.fragments_confirmed = sorted(confirmed)
            self.journal.append(
                "file_failed", file_id=fe.file_id, status=FAILED_VERIFY,
                reason=outcome.reason, expected=fe.sha256, actual=actual,
            )
            self.ledger.release(lease, reason="verify_failed")
            self._quota_step("target_release", fe.file_id, None, None)
            return

        self._publish_from_lease(fe, outcome, lease, confirmed)

    # -- volume-major ----------------------------------------------------
    def _run_volume_major(self) -> None:
        plan = self.plan
        # All non-empty targets must be open simultaneously for a sweep.
        open_fit = sum(
            fe.total_length for fe in self._selected_files() if fe.total_length
        )
        if open_fit > self.quota:
            for fe in self._selected_files():
                o = self.outcomes[fe.file_id]
                if fe.total_length == 0:
                    # empty files need no scratch and never depend on the sweep
                    self._publish_empty(fe, o)
                    continue
                o.status = SKIPPED_QUOTA
                o.reason = (
                    "volume-major needs all targets open: "
                    f"{open_fit} B > quota {self.quota} B"
                )
            self.journal.append("strategy_infeasible", strategy="volume",
                                required_open_bytes=open_fit, quota_bytes=self.quota)
            return

        # Open a direct target per non-empty file; empty files publish immediately
        # (no media access and no scratch required).
        leases: dict[str, str] = {}
        confirmed: dict[str, dict[str, dict]] = {fid: {} for fid in self.requested_ids}
        for fe in self._selected_files():
            if fe.total_length == 0:
                self._publish_empty(fe, self.outcomes[fe.file_id])
                continue
            try:
                lease = self.ledger.reserve(
                    owner=f"file:{fe.file_id}", size=fe.total_length,
                    label=f"target:{fe.file_id}",
                )
            except QuotaExceeded as exc:
                o = self.outcomes[fe.file_id]
                o.status = SKIPPED_QUOTA
                o.reason = exc.message
                continue
            self.ledger.preallocate(lease)
            leases[fe.file_id] = lease
            self.outcomes[fe.file_id].lease_id = lease
            self._quota_step("target_open", fe.file_id, None, lease)

        failure: dict[str, tuple[str, Exception]] = {}
        for run in plan.runs:
            if not self.scheduler.check_volume(run.volume_id):
                for fid, frag in run.fragments:
                    if fid in leases:
                        failure.setdefault(fid, (FAILED_MEDIA, MediaError(
                            "volume unavailable", volume_id=run.volume_id)))
                self.journal.append("volume_unavailable", volume_id=run.volume_id)
                continue
            self.scheduler.pin.add(run.volume_id)
            try:
                for fid, frag in sorted(run.fragments, key=lambda p: p[1].start_block):
                    if fid in failure or fid not in leases or frag.fragment_id in confirmed[fid]:
                        continue
                    fe = self.catalog.files[fid]
                    try:
                        self._stream_fragment(fe, frag, leases[fid], confirmed[fid])
                    except FragmentValidationError as exc:
                        failure.setdefault(fid, (FAILED_FRAGMENT, exc))
                    except (MediaError, RestoreError) as exc:
                        failure.setdefault(fid, (FAILED_MEDIA, exc))
                    self._maybe_save_checkpoint(fe, leases[fid], confirmed[fid])
                    # publish any file that now has every fragment confirmed
                    if (fid not in failure and
                            len(confirmed[fid]) == len(fe.fragments)):
                        self._finalize_volume_file(fe, leases, confirmed)
            finally:
                self.scheduler.pin.discard(run.volume_id)

        # Anything still open and fully confirmed but unpublishable?
        for fe in self._selected_files():
            o = self.outcomes[fe.file_id]
            if fe.total_length == 0:
                continue
            if o.status not in (COMPLETE, FAILED_FRAGMENT, FAILED_MEDIA, FAILED_VERIFY, SKIPPED_QUOTA):
                if fe.file_id in failure:
                    st, exc = failure[fe.file_id]
                    o.status = st
                    o.reason = exc.message
                    o.fragments_confirmed = sorted(confirmed[fe.file_id])
                    self.journal.append("file_failed", file_id=fe.file_id,
                                        status=st, reason=exc.message)
                elif len(confirmed[fe.file_id]) == len(fe.fragments):
                    self._finalize_volume_file(fe, leases, confirmed)
                else:
                    o.status = INCOMPLETE
                    o.reason = "run ended before all fragments were confirmed"
                    o.fragments_confirmed = sorted(confirmed[fe.file_id])
            if fe.file_id in leases:
                self.ledger.release(leases[fe.file_id], reason="sweep_end")
                self._quota_step("target_release", fe.file_id, None, None)

    def _finalize_volume_file(self, fe: FileEntry, leases: dict[str, str],
                              confirmed: dict[str, dict[str, dict]]) -> None:
        o = self.outcomes[fe.file_id]
        if o.status == COMPLETE:
            return
        lease = leases[fe.file_id]
        actual = self.ledger.hash_file(lease)
        if actual != fe.sha256:
            o.status = FAILED_VERIFY
            o.reason = "whole-file sha256 mismatch after reassembly"
            o.fragments_confirmed = sorted(confirmed[fe.file_id])
            self.journal.append("file_failed", file_id=fe.file_id,
                                status=FAILED_VERIFY, reason=o.reason,
                                expected=fe.sha256, actual=actual)
            return
        self._publish_from_lease(fe, o, lease, confirmed[fe.file_id])
        leases.pop(fe.file_id, None)

    # -- shared streaming + verification ---------------------------------
    def _stream_fragment(self, fe: FileEntry, frag: Fragment, lease: str,
                         confirmed: dict[str, dict]) -> None:
        if not self.scheduler.check_volume(frag.volume_id):
            raise MediaError("volume unavailable", volume_id=frag.volume_id)
        offset = fragment_offset(fe, frag)
        h = hashlib.sha256()
        written = 0
        remaining = frag.valid_bytes
        blocks = self.scheduler.read_blocks(frag.volume_id, frag.start_block, frag.block_count)
        if len(blocks) != frag.block_count:
            raise FragmentValidationError(
                "fragment read returned wrong block count",
                file_id=fe.file_id, fragment_id=frag.fragment_id,
            )
        for j, blk in enumerate(blocks):
            if blk.kind != "data":
                raise FragmentValidationError(
                    "fragment covers a filemark (media/structure mismatch)",
                    file_id=fe.file_id, fragment_id=frag.fragment_id, block=blk.block_index,
                )
            take = min(self.block_size, remaining)
            if take < 0:
                take = 0
            chunk = blk.data[:take]
            if len(chunk) != take:
                raise FragmentValidationError(
                    "block shorter than fragment requires",
                    file_id=fe.file_id, fragment_id=frag.fragment_id,
                    block=blk.block_index, wanted=take, got=len(chunk),
                )
            h.update(chunk)
            self.ledger.write_at(lease, offset + written, chunk)
            written += take
            remaining -= take
        if written != frag.valid_bytes:
            raise FragmentValidationError(
                "fragment length does not match valid_bytes",
                file_id=fe.file_id, fragment_id=frag.fragment_id,
                expected=frag.valid_bytes, actual=written,
            )
        digest = h.hexdigest()
        if digest != frag.sha256:
            raise FragmentValidationError(
                "fragment sha256 mismatch",
                file_id=fe.file_id, fragment_id=frag.fragment_id,
                expected=frag.sha256, actual=digest,
                volume_id=frag.volume_id, start_block=frag.start_block,
            )
        rec = {
            "fragment_id": frag.fragment_id,
            "offset": offset,
            "valid_bytes": frag.valid_bytes,
            "sha256": digest,
            "volume_id": frag.volume_id,
            "start_block": frag.start_block,
            "block_count": frag.block_count,
        }
        confirmed[frag.fragment_id] = rec
        self._confirmed.setdefault(fe.file_id, {})[frag.fragment_id] = rec
        self._confirmed_total += 1
        self.journal.append(
            "fragment_confirmed", file_id=fe.file_id, **rec,
        )
        self._quota_step("fragment", fe.file_id, frag.fragment_id, lease)

    # -- publishing ------------------------------------------------------
    def _publish_empty(self, fe: FileEntry, o: FileOutcome) -> None:
        actual = _sha256_bytes(b"")
        if actual != fe.sha256:
            o.status = FAILED_VERIFY
            o.reason = "zero-length file digest mismatch"
            self.journal.append("file_failed", file_id=fe.file_id,
                                status=FAILED_VERIFY, reason=o.reason)
            return
        dest = self._output_path(fe.path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name("." + dest.name + f".{self.run_id}.tmp")
        with open(tmp, "wb"):
            pass
        os.replace(tmp, dest)
        o.status = COMPLETE
        o.published_path = str(dest)
        o.bytes_published = 0
        o.verified = True
        self.journal.append(
            "file_published", file_id=fe.file_id, path=fe.path,
            output=str(dest), total_length=0, sha256=fe.sha256,
            lease=None, fragments=[],
        )
        self._checkpoint()

    def _publish_from_lease(self, fe: FileEntry, o: FileOutcome, lease: str,
                            confirmed: dict[str, dict]) -> None:
        dest = self._output_path(fe.path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name("." + dest.name + f".{self.run_id}.tmp")
        # copy staged bytes -> output temp, hashing what we copy to confirm
        h = hashlib.sha256()
        with open(self.ledger.path(lease), "rb") as src, open(tmp, "wb") as dst:
            copied = 0
            while True:
                buf = src.read(65536)
                if not buf:
                    break
                h.update(buf)
                dst.write(buf)
                copied += len(buf)
        if copied != fe.total_length or h.hexdigest() != fe.sha256:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            o.status = FAILED_VERIFY
            o.reason = "output copy failed whole-file verification"
            self.journal.append("file_failed", file_id=fe.file_id,
                                status=FAILED_VERIFY, reason=o.reason,
                                copied=copied)
            return
        os.replace(tmp, dest)
        o.status = COMPLETE
        o.published_path = str(dest)
        o.bytes_published = copied
        o.verified = True
        o.fragments_confirmed = sorted(confirmed)
        self.journal.append(
            "file_published", file_id=fe.file_id, path=fe.path, output=str(dest),
            total_length=fe.total_length, sha256=fe.sha256, lease=lease,
            fragments=[
                {"fragment_id": fr["fragment_id"], "offset": fr["offset"],
                 "valid_bytes": fr["valid_bytes"], "sha256": fr["sha256"]}
                for fr in sorted(confirmed.values(), key=lambda r: r["offset"])
            ],
        )
        self.ledger.release(lease, reason="published")
        o.lease_id = None
        self._quota_step("target_release", fe.file_id, None, None)
        self._checkpoint()

    def _output_path(self, rel_path: str) -> Path:
        dest = (self.workspace.output_dir / rel_path).resolve()
        root = self.workspace.output_dir.resolve()
        if root not in dest.parents and dest != root:
            raise RestoreError("output path escapes output dir", path=rel_path)
        return dest

    # -- checkpoint / interruption --------------------------------------
    def _quota_step(self, phase: str, fid: str, frag_id: Optional[str],
                    lease: Optional[str]) -> None:
        self.quota_steps.append({
            "seq": len(self.quota_steps) + 1,
            "phase": phase,
            "file_id": fid,
            "fragment_id": frag_id,
            "lease": lease,
            "current_bytes": self.ledger.current,
            "peak_bytes": self.ledger.peak,
            "quota_bytes": self.quota,
        })

    def _maybe_save_checkpoint(self, fe: FileEntry, lease: str,
                               confirmed: dict[str, dict]) -> None:
        self._checkpoint(extra_file=fe, lease=lease, confirmed=confirmed)
        if (self.interrupt_after is not None
                and self._confirmed_total >= self.interrupt_after):
            self.journal.append("interrupted", file_id=fe.file_id,
                                confirmed_total=self._confirmed_total,
                                reason="simulated interruption after confirmed fragment")
            self._checkpoint(extra_file=fe, lease=lease, confirmed=confirmed)
            self._interrupted = True
            raise Interrupted("simulated interruption", run_id=self.run_id,
                              confirmed_fragments=self._confirmed_total)

    def _checkpoint(self, *, extra_file: Optional[FileEntry] = None,
                    lease: Optional[str] = None,
                    confirmed: Optional[dict[str, dict]] = None) -> None:
        if extra_file is not None and confirmed is not None:
            self._confirmed[extra_file.file_id] = {
                k: dict(v) for k, v in confirmed.items()
            }
        files_state: dict[str, dict] = {}
        for fid, o in self.outcomes.items():
            last_lease = o.lease_id
            if extra_file is not None and fid == extra_file.file_id and lease is not None:
                last_lease = lease
            files_state[fid] = {
                "status": o.status,
                "lease": last_lease,
                "confirmed": {
                    frag_id: dict(rec)
                    for frag_id, rec in self._confirmed.get(fid, {}).items()
                },
            }
            if extra_file is not None and fid == extra_file.file_id:
                if files_state[fid]["status"] in (NOT_STARTED,):
                    files_state[fid]["status"] = INCOMPLETE
        self.journal.write_checkpoint({
            "run_id": self.run_id,
            "strategy": self.strategy,
            "quota_bytes": self.quota,
            "catalog_fingerprint": catalog_fingerprint(self.catalog),
            "volume_digests": self._volume_digests(),
            "journal_seq": self.journal.seq,
            "confirmed_total": self._confirmed_total,
            "files": files_state,
            "scratch_current": self.ledger.current,
            "scratch_peak": self.ledger.peak,
            "device": {
                "drives": [d.as_dict() for d in self.device.drives.values()],
                "next_scan": {
                    vid: (self.scheduler.position(vid) or 0) for vid in self.scheduler.mounted
                },
            },
            "saved_at": round(self._clock(), 6),
        })

    def _note_interrupt(self) -> None:
        try:
            self.journal.close()
        except Exception:
            pass

    # -- reporting -------------------------------------------------------
    def _result(self, started: float, effective: str) -> dict:
        published = sorted(f for f, o in self.outcomes.items() if o.status == COMPLETE)
        failed = sorted(f for f, o in self.outcomes.items()
                        if o.status in (FAILED_FRAGMENT, FAILED_MEDIA, FAILED_VERIFY))
        skipped = sorted(f for f, o in self.outcomes.items() if o.status == SKIPPED_QUOTA)
        return {
            "run_id": self.run_id,
            "strategy_requested": self.strategy,
            "strategy_effective": effective,
            "provenance": PROVENANCE,
            "quota_bytes": self.quota,
            "requested": list(self.requested_ids),
            "published": published,
            "failed": failed,
            "skipped_quota": skipped,
            "files": {fid: o.as_dict() for fid, o in sorted(self.outcomes.items())},
            "plan": self.plan.as_dict() if hasattr(self, "plan") else None,
            "scratch": {
                **self.ledger.snapshot(),
                "steps": self.quota_steps,
                "events": self.ledger.events(),
            },
            "device": {
                "calls": list(self.device.log.entries),
                "summary": self.device.log.summary(),
                "state": self.device.state(),
                "mount_events": self.scheduler.mount_events,
            },
            "journal": self.journal.run_summary(),
            "elapsed_seconds": round(self._clock() - started, 6),
        }


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

def resume(
    catalog: Catalog,
    library: MediaLibrary,
    workspace: Workspace,
    *,
    quota: int,
    run_id: Optional[str] = None,
    interrupt_after: Optional[int] = None,
    allow_fallback: bool = True,
) -> dict:
    """Resume an interrupted run, re-verifying staged bytes and head positions.

    * If ``run_id`` is None the checkpoint's run id is used.
    * A changed catalog or changed volume image invalidates affected confirmed
      fragments; nothing is skipped on the strength of a bare log line.
    """
    if run_id is None:
        cp = Journal(workspace.state_dir, "?").read_checkpoint()
        if cp is None:
            raise StateConflict("no checkpoint to resume")
        run_id = cp["run_id"]
    journal = Journal(workspace.state_dir, run_id)
    meta = journal.read_meta()
    checkpoint = journal.read_checkpoint()
    if meta is None or checkpoint is None:
        raise StateConflict("missing run metadata or checkpoint", run_id=run_id)

    journ = journal.replay()
    requested = list(meta.get("requested_files") or checkpoint.get("files", {}).keys())
    for fid in journ["published"]:
        if fid not in requested:
            requested.append(fid)

    engine = RestoreEngine(
        catalog, library, workspace, quota=quota,
        strategy=meta["strategy_effective"], run_id=run_id,
        block_size=meta.get("block_size", 1024), interrupt_after=interrupt_after,
        initial_peak=checkpoint.get("scratch_peak", 0),
    )
    engine.requested_ids = [f for f in requested if f in catalog.files]
    for fid in engine.requested_ids:
        fe = catalog.files[fid]
        engine.outcomes[fid] = FileOutcome(fid, fe.path, fe.total_length)

    invalidation = {"catalog_changed": False, "changed_volumes": [],
                    "dropped_fragments": [], "restored_fragments": [],
                    "position_checks": [], "restarted": False}

    # 1) catalog identity -------------------------------------------------
    if checkpoint.get("catalog_fingerprint") != catalog_fingerprint(catalog):
        invalidation["catalog_changed"] = True
        invalidation["restarted"] = True
        journal.append("catalog_changed", reason="catalog fingerprint differs; old plan invalid")
        # discard all confirmed state; stage files are stale
        _discard_stale_stage(workspace, checkpoint)
        journal.append("resume_restart", reason="catalog changed")
        journal.close()
        engine.journal = journal
        # re-run from scratch under the new catalog
        result = engine.restore(engine.requested_ids, allow_fallback=allow_fallback)
        result["invalidation"] = invalidation
        return result

    # 2) volume identity --------------------------------------------------
    old_digests = checkpoint.get("volume_digests", {})
    new_digests = engine._volume_digests()
    changed = [vid for vid, d in old_digests.items()
               if vid in new_digests and new_digests[vid] != d]
    invalidation["changed_volumes"] = changed
    if changed:
        journal.append("media_changed", volumes=changed,
                       reason="volume image digest changed since checkpoint")

    # 3) re-adopt staged leases, drop files already published -------------
    file_states = checkpoint.get("files", {})
    for fid, st in file_states.items():
        if fid not in engine.outcomes:
            continue
        o = engine.outcomes[fid]
        pub = journ["published"].get(fid)
        if pub:
            o.status = COMPLETE
            o.published_path = pub.get("output")
            o.bytes_published = pub.get("total_length", o.total_length)
            o.verified = True
            continue
        lease = st.get("lease")
        if lease and o.total_length:
            adopted = engine.ledger.adopt(
                owner=f"file:{fid}", size=o.total_length, key=f"target:{fid}",
                lease_id=lease,
            )
            if adopted:
                o.lease_id = lease
                engine._quota_step("target_adopt", fid, None, lease)

    # 4) re-hash every confirmed fragment region before trusting it --------
    # Confirmed fragments come from BOTH the checkpoint and the journal replay;
    # neither is trusted on its own -- each region is re-hashed below.
    for fid, st in file_states.items():
        if fid not in engine.outcomes:
            continue
        o = engine.outcomes[fid]
        if o.status == COMPLETE:
            continue
        fe = catalog.files[fid]
        catalog_by_id = {f.fragment_id: f for f in fe.fragments}
        lease = o.lease_id
        merged_confirmed: dict[str, dict] = {}
        for frag_id, rec in (st.get("confirmed") or {}).items():
            merged_confirmed[frag_id] = rec
        for frag_id, rec in (journ["confirmed"].get(fid) or {}).items():
            merged_confirmed.setdefault(frag_id, rec)
        for frag_id, rec in merged_confirmed.items():
            frag = catalog_by_id.get(frag_id)
            ok = frag is not None and lease is not None
            reason = None
            if not ok:
                reason = "fragment no longer in catalog / no staged target"
            elif rec.get("sha256") != frag.sha256:
                reason = "staged record digest disagrees with catalog"
            elif rec.get("volume_id") in changed:
                reason = "source volume image changed"
            elif rec.get("offset") != fragment_offset(fe, frag):
                reason = "staged offset disagrees with catalog layout"
            elif rec.get("valid_bytes") != frag.valid_bytes:
                reason = "staged length disagrees with catalog"
            else:
                try:
                    got = engine.ledger.hash_region(lease, rec["offset"], rec["valid_bytes"])
                except StagingError as exc:
                    got = None
                    reason = f"staged region unreadable: {exc.message}"
                if got is not None and got != frag.sha256:
                    reason = "staged region digest mismatch (re-check failed)"
            if reason:
                invalidation["dropped_fragments"].append(
                    {"file_id": fid, "fragment_id": frag_id, "reason": reason}
                )
                journal.append("fragment_dropped_on_resume", file_id=fid,
                               fragment_id=frag_id, reason=reason)
            else:
                invalidation["restored_fragments"].append(
                    {"file_id": fid, "fragment_id": frag_id}
                )
                o.fragments_confirmed.append(frag_id)

    # 5) re-establish real device positions -------------------------------
    engine.journal = journal
    journal.open_for_append()
    pos_result = engine.device.restore_positions(checkpoint.get("device", {}))
    invalidation["position_checks"] = pos_result["checks"]
    journal.append("run_resumed", confirmed_total=checkpoint.get("confirmed_total", 0),
                   position_checks=pos_result["checks"],
                   restored_fragments=len(invalidation["restored_fragments"]),
                   dropped_fragments=len(invalidation["dropped_fragments"]))

    # 6) continue remaining fragments -------------------------------------
    engine._resume_continue()
    engine.scheduler.release_all()
    journal.close()
    result = engine._result(0.0, meta["strategy_effective"])
    result["invalidation"] = invalidation
    return result


def _discard_stale_stage(workspace: Workspace, checkpoint: dict) -> None:
    for fid, st in checkpoint.get("files", {}).items():
        lease = st.get("lease")
        if not lease:
            continue
        p = workspace.work_dir / "scratch" / f"{lease}.bin"
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _resume_continue_impl(self: RestoreEngine) -> None:
    """Continue only the fragments not re-verified as confirmed."""
    # Rebuild confirmed maps from outcome state (regions were re-hashed).
    for fid in self.requested_ids:
        o = self.outcomes[fid]
        fe = self.catalog.files[fid]
        if o.status == COMPLETE:
            continue
        if o.lease_id is None:
            # nothing usable staged; restore this file from scratch
            self._restore_file_direct(fe)
            continue
        confirmed = {f.fragment_id: {"fragment_id": f.fragment_id,
                                     "offset": fragment_offset(fe, f),
                                     "valid_bytes": f.valid_bytes, "sha256": f.sha256,
                                     "volume_id": f.volume_id, "start_block": f.start_block,
                                     "block_count": f.block_count}
                     for f in fe.fragments if f.fragment_id in o.fragments_confirmed}
        # stream any missing fragments grouped by volume
        missing = [f for f in fe.fragments if f.fragment_id not in confirmed]
        groups: dict[str, list[Fragment]] = {}
        for f in missing:
            groups.setdefault(f.volume_id, []).append(f)
        failure = None
        for vid in sorted(groups, key=lambda v: (min(f.start_block for f in groups[v]), v)):
            for frag in sorted(groups[vid], key=lambda f: f.start_block):
                try:
                    self._stream_fragment(fe, frag, o.lease_id, confirmed)
                except FragmentValidationError as exc:
                    failure = (FAILED_FRAGMENT, exc)
                except (MediaError, RestoreError) as exc:
                    failure = (FAILED_MEDIA, exc)
                if failure:
                    break
                self._maybe_save_checkpoint(fe, o.lease_id, confirmed)
            if failure:
                break
        if failure:
            st, exc = failure
            o.status = st
            o.reason = exc.message
            o.fragments_confirmed = sorted(confirmed)
            self.journal.append("file_failed", file_id=fid, status=st, reason=exc.message)
            self.ledger.release(o.lease_id, reason="failed_on_resume")
            o.lease_id = None
            self._quota_step("target_release", fid, None, None)
            continue
        actual = self.ledger.hash_file(o.lease_id)
        if actual != fe.sha256:
            o.status = FAILED_VERIFY
            o.reason = "whole-file sha256 mismatch after resume"
            self.journal.append("file_failed", file_id=fid, status=FAILED_VERIFY,
                                reason=o.reason)
            self.ledger.release(o.lease_id, reason="verify_failed_resume")
            o.lease_id = None
            self._quota_step("target_release", fid, None, None)
            continue
        self._publish_from_lease(fe, o, o.lease_id, confirmed)


RestoreEngine._resume_continue = _resume_continue_impl  # type: ignore[attr-defined]
