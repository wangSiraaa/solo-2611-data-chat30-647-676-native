"""Two-drive synthetic tape device with a full, deterministic call log.

The device is the *only* way a client can obtain bytes from a volume.  There is
no seek API: ``rewind`` takes a drive back to block 0, ``skip`` moves forward
only, and ``read`` consumes the block under the head and advances by one.  Every
mount, unmount, skip, rewind, read and filemark crossing is appended to the log
together with the returned byte count, so a recovery run can be audited call by
call.

Two drives may hold two different volumes at once; a single volume may never be
mounted on both drives simultaneously.  A drive holds at most one volume.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import media
from .errors import (
    BlockOutOfRange,
    DeviceError,
    DriveEmpty,
    DriveOccupied,
    EndOfTape,
    MediaError,
    NoSuchDrive,
    NoSuchVolume,
    VolumeInUse,
)
from .model import Catalog, Volume

PROVENANCE = "simulated-media"  # no real hardware is ever touched


@dataclass(frozen=True)
class BlockRead:
    kind: str  # "data" | "filemark"
    data: bytes
    volume_id: str
    block_index: int
    drive_id: str

    @property
    def bytes_returned(self) -> int:
        return len(self.data)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "volume_id": self.volume_id,
            "block_index": self.block_index,
            "drive_id": self.drive_id,
            "bytes_returned": self.bytes_returned,
        }


@dataclass
class Drive:
    drive_id: str
    volume_id: Optional[str] = None
    volume: Optional[Volume] = None
    position: int = 0
    load_error: Optional[str] = None

    @property
    def occupied(self) -> bool:
        return self.volume_id is not None

    @property
    def mounted(self) -> bool:
        return self.volume_id is not None and self.volume is not None

    def as_dict(self) -> dict:
        return {
            "drive_id": self.drive_id,
            "mounted_volume": self.volume_id,
            "position": self.position,
            "block_count": self.volume.block_count if self.volume else None,
            "load_error": self.load_error,
        }


@dataclass
class CallLog:
    entries: list[dict] = field(default_factory=list)
    seq: int = 0

    def record(self, op: str, **fields: object) -> dict:
        self.seq += 1
        entry = {"seq": self.seq, "op": op}
        entry.update(fields)
        self.entries.append(entry)
        return entry

    def summary(self) -> dict:
        def count_op(name: str) -> int:
            return sum(1 for e in self.entries if e["op"] == name)

        return {
            "calls": len(self.entries),
            "mounts": count_op("mount"),
            "mount_failures": sum(
                1 for e in self.entries if e["op"] == "mount" and not e.get("ok", True)
            ),
            "unmounts": count_op("unmount"),
            "rewinds": count_op("rewind"),
            "skip_calls": count_op("skip"),
            "blocks_skipped": sum(e.get("actual", 0) for e in self.entries if e["op"] == "skip"),
            "read_calls": count_op("read"),
            "blocks_read": count_op("read"),
            "data_blocks_read": sum(
                1 for e in self.entries if e["op"] == "read" and e.get("kind") == "data"
            ),
            "filemarks_read": sum(
                1 for e in self.entries if e["op"] == "read" and e.get("kind") == "filemark"
            ),
            "bytes_returned": sum(e.get("bytes_returned", 0) for e in self.entries if e["op"] == "read"),
            "errors": count_op("error"),
        }


class MediaLibrary:
    """Resolves ``volume_id`` to a synthetic image path and loads it on demand."""

    def __init__(self, media_dir: Path, volume_paths: Optional[dict[str, Path]] = None) -> None:
        self.media_dir = Path(media_dir)
        self._paths: dict[str, Path] = {}
        if volume_paths:
            self._paths.update({k: Path(v) for k, v in volume_paths.items()})

    def register(self, volume_id: str, path: Path) -> None:
        self._paths[volume_id] = Path(path)

    def path_for(self, volume_id: str) -> Path:
        if volume_id in self._paths:
            return self._paths[volume_id]
        for suffix in (".tape", ".img", ".dat"):
            candidate = self.media_dir / f"{volume_id}{suffix}"
            if candidate.exists():
                self._paths[volume_id] = candidate
                return candidate
        raise NoSuchVolume("no synthetic image for volume", volume_id=volume_id)

    def known_volumes(self) -> list[str]:
        names = {p.name.rsplit(".", 1)[0] for p in self.media_dir.glob("*.tape")}
        names.update(self._paths)
        return sorted(names)

    def load(self, volume_id: str, *, strict_footer: bool = False) -> Volume:
        path = self.path_for(volume_id)
        return media.read_volume(path, strict_footer=strict_footer)

    def image_digest(self, volume_id: str) -> str:
        """SHA256 of the raw image, used to detect media changes across resumes."""
        import hashlib

        path = self.path_for(volume_id)
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
        except OSError as exc:
            raise MediaError(f"cannot hash volume image: {exc}", path=str(path)) from exc
        return h.hexdigest()


class DeviceManager:
    def __init__(
        self,
        library: MediaLibrary,
        drive_ids: Iterable[str] = ("D0", "D1"),
        *,
        log: Optional[CallLog] = None,
    ) -> None:
        ids = list(drive_ids)
        if not ids:
            raise ValueError("at least one drive is required")
        self.library = library
        self.drives: dict[str, Drive] = {d: Drive(d) for d in ids}
        self.log = log or CallLog()
        self._mounted: dict[str, Drive] = {}

    # -- introspection ---------------------------------------------------
    def drive(self, drive_id: str) -> Drive:
        if drive_id not in self.drives:
            raise NoSuchDrive("unknown drive", drive_id=drive_id)
        return self.drives[drive_id]

    def state(self) -> dict:
        return {
            "drives": [self.drives[d].as_dict() for d in sorted(self.drives)],
            "mounted_volumes": sorted(self._mounted),
        }

    def mounted_drives(self, volume_id: str) -> list[Drive]:
        return [d for d in self.drives.values() if d.volume_id == volume_id and d.mounted]

    # -- operations ------------------------------------------------------
    def mount(self, drive_id: str, volume_id: str, *, strict_footer: bool = False) -> Drive:
        drive = self.drive(drive_id)
        before = drive.position
        if drive.occupied:
            self.log.record(
                "mount", drive=drive_id, volume=volume_id, before=before, after=before,
                ok=False, error="drive_occupied", mounted=drive.volume_id,
            )
            raise DriveOccupied(
                "drive already holds a volume", drive=drive_id, mounted=drive.volume_id,
            )
        if volume_id in self._mounted:
            owner = self._mounted[volume_id].drive_id
            self.log.record(
                "mount", drive=drive_id, volume=volume_id, before=before, after=before,
                ok=False, error="volume_in_use", mounted_by=owner,
            )
            raise VolumeInUse(
                "volume is already mounted on another drive",
                volume_id=volume_id, mounted_by=owner,
            )
        try:
            volume = self.library.load(volume_id, strict_footer=strict_footer)
        except NoSuchVolume as exc:
            self.log.record(
                "mount", drive=drive_id, volume=volume_id, before=before, after=before,
                ok=False, error=exc.code,
            )
            self.log.record("error", drive=drive_id, volume=volume_id, error=exc.code,
                            message=exc.message)
            raise
        except MediaError as exc:
            drive.volume_id = volume_id
            drive.volume = None
            drive.position = 0
            drive.load_error = exc.code
            self.log.record(
                "mount", drive=drive_id, volume=volume_id, before=before, after=0,
                ok=False, error=exc.code,
            )
            self.log.record("error", drive=drive_id, volume=volume_id, error=exc.code,
                            message=exc.message)
            raise
        drive.volume_id = volume_id
        drive.volume = volume
        drive.position = 0
        drive.load_error = None
        self._mounted[volume_id] = drive
        self.log.record(
            "mount", drive=drive_id, volume=volume_id, before=before, after=0, ok=True,
            block_count=volume.block_count,
        )
        return drive

    def unmount(self, drive_id: str) -> None:
        drive = self.drive(drive_id)
        if not drive.occupied:
            self.log.record("unmount", drive=drive_id, before=drive.position,
                            after=drive.position, ok=False, error="drive_empty")
            raise DriveEmpty("drive has no volume", drive=drive_id)
        volume_id = drive.volume_id
        pos = drive.position
        self._mounted.pop(volume_id, None)  # type: ignore[arg-type]
        drive.volume_id = None
        drive.volume = None
        drive.position = 0
        drive.load_error = None
        self.log.record("unmount", drive=drive_id, volume=volume_id, before=pos, after=0, ok=True)

    def rewind(self, drive_id: str) -> int:
        drive = self._mounted_drive(drive_id, "rewind")
        before = drive.position
        drive.position = 0
        self.log.record("rewind", drive=drive_id, volume=drive.volume_id,
                        before=before, after=0, ok=True)
        return 0

    def skip(self, drive_id: str, count: int) -> int:
        drive = self._mounted_drive(drive_id, "skip")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            self.log.record("skip", drive=drive_id, volume=drive.volume_id,
                            requested=count, before=drive.position, after=drive.position,
                            actual=0, ok=False, error="bad_skip_count")
            raise BlockOutOfRange("skip count must be a non-negative integer", requested=count)
        assert drive.volume is not None
        before = drive.position
        target = before + count
        if target > drive.volume.block_count:
            actual = drive.volume.block_count - before
            drive.position = drive.volume.block_count
            self.log.record(
                "skip", drive=drive_id, volume=drive.volume_id, requested=count,
                before=before, after=drive.position, actual=actual, ok=False,
                error="end_of_tape",
            )
            raise EndOfTape(
                "skip runs past end of volume", drive=drive_id,
                requested=count, available=actual,
            )
        drive.position = target
        self.log.record(
            "skip", drive=drive_id, volume=drive.volume_id, requested=count,
            before=before, after=target, actual=count, ok=True,
        )
        return target

    def read(self, drive_id: str) -> BlockRead:
        drive = self._mounted_drive(drive_id, "read")
        assert drive.volume is not None
        before = drive.position
        if before >= drive.volume.block_count:
            self.log.record(
                "read", drive=drive_id, volume=drive.volume_id, block=before,
                before=before, after=before, ok=False, error="end_of_tape",
            )
            raise EndOfTape("read at end of volume", drive=drive_id, block=before)
        block = drive.volume.blocks[before]
        drive.position = before + 1
        if block.kind == "data":
            result = BlockRead("data", bytes(block.payload), drive.volume_id, before, drive_id)
            self.log.record(
                "read", drive=drive_id, volume=drive.volume_id, block=before, kind="data",
                before=before, after=drive.position, bytes_returned=len(block.payload), ok=True,
            )
        else:
            result = BlockRead("filemark", b"", drive.volume_id, before, drive_id)
            self.log.record(
                "read", drive=drive_id, volume=drive.volume_id, block=before, kind="filemark",
                before=before, after=drive.position, bytes_returned=0, ok=True,
            )
        return result

    def seek_to(self, drive_id: str, block_index: int) -> int:
        """Convenience: rewind and skip forward to ``block_index``.

        Deliberately implemented *only* with rewind+skip -- there is no random
        seek primitive.  Returns the logical block count traversed by skip.
        """
        drive = self._mounted_drive(drive_id, "seek")
        if block_index < 0:
            raise BlockOutOfRange("target block must be non-negative", target=block_index)
        assert drive.volume is not None
        if block_index > drive.volume.block_count:
            raise BlockOutOfRange(
                "target block past end of volume",
                target=block_index, block_count=drive.volume.block_count,
            )
        self.rewind(drive_id)
        if block_index:
            self.skip(drive_id, block_index)
        return drive.position

    def _mounted_drive(self, drive_id: str, op: str) -> Drive:
        drive = self.drive(drive_id)
        if not drive.occupied:
            self.log.record(op, drive=drive_id, ok=False, error="drive_empty")
            raise DriveEmpty("drive has no volume", drive=drive_id)
        if not drive.mounted:
            self.log.record(op, drive=drive_id, volume=drive.volume_id,
                            ok=False, error="volume_not_mounted")
            raise MediaError("volume image is unreadable and not mounted",
                             volume_id=drive.volume_id)
        return drive

    # -- sequential client helper ---------------------------------------
    def scan(self, volume_id: str, drive_id: Optional[str] = None) -> dict:
        """Independent sequential read of an entire volume (no restore logic)."""
        drive_id = drive_id or sorted(self.drives)[0]
        drive = self.drive(drive_id)
        if drive.occupied:
            self.unmount(drive_id)
        self.mount(drive_id, volume_id)
        self.rewind(drive_id)
        blocks = []
        files = 0
        while True:
            try:
                got = self.read(drive_id)
            except EndOfTape:
                break
            if got.kind == "filemark":
                files += 1
                blocks.append({"kind": "filemark", "block_index": got.block_index})
            else:
                blocks.append({
                    "kind": "data", "block_index": got.block_index,
                    "bytes": got.bytes_returned,
                })
        return {
            "drive_id": drive_id,
            "volume_id": volume_id,
            "blocks": blocks,
            "filemarks": files,
            "position": drive.position,
            "log": self.log.summary(),
        }

    def save_state(self) -> dict:
        return {
            "provenance": PROVENANCE,
            "drives": [self.drives[d].as_dict() for d in sorted(self.drives)],
            "log_summary": self.log.summary(),
            "device_calls": list(self.log.entries),
        }

    def restore_positions(self, saved: dict) -> dict:
        """Rebuild real head positions from saved state, verifying skips.

        Used on resume: mount each recorded volume, rewind, skip to the saved
        block and assert the resulting position.  Any mismatch is reported.
        """
        checks = []
        for rec in saved.get("drives", []):
            did = rec.get("drive_id")
            vid = rec.get("mounted_volume")
            pos = rec.get("position", 0)
            if not did or vid is None or did not in self.drives:
                continue
            try:
                if self.drives[did].occupied:
                    self.unmount(did)
                self.mount(did, vid)
                self.seek_to(did, pos)
                actual = self.drives[did].position
                checks.append({
                    "drive_id": did, "volume_id": vid,
                    "expected": pos, "actual": actual, "ok": actual == pos,
                })
            except (DeviceError, EndOfTape, BlockOutOfRange, MediaError) as exc:
                checks.append({
                    "drive_id": did, "volume_id": vid, "expected": pos,
                    "actual": None, "ok": False, "error": exc.code,
                    "message": exc.message,
                })
        return {"checks": checks, "ok": all(c["ok"] for c in checks)}
