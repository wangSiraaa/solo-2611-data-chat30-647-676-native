"""Leased, metered scratch space for in-flight restore data.

Every byte of fragment data the restorer holds on the work disk is leased here
first.  Leases are tied to a file target and released when the target is
committed (published) or aborted.  Accounting is real, not advisory:

* the bytes live in files under ``work_dir`` (never in an untracked Python
  cache),
* reading ``ledger.snapshot()`` reports current / peak usage at any time,
* an over-quota request raises :class:`QuotaExceeded` *before* writing.

The final published outputs live in a different directory and are never counted.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import QuotaExceeded, StagingError


@dataclass
class Lease:
    lease_id: str
    owner: str
    path: Path
    size: int


class ScratchLedger:
    def __init__(self, work_dir: Path, quota_bytes: int, *, initial_peak: int = 0) -> None:
        if quota_bytes < 0:
            raise StagingError("quota must be non-negative", quota_bytes=quota_bytes)
        self.work_dir = Path(work_dir)
        self.quota = int(quota_bytes)
        # keep leaked/leftover scratch visible instead of silently deleting it
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.root = self.work_dir / "scratch"
        self.root.mkdir(parents=True, exist_ok=True)
        self._leases: dict[str, Lease] = {}
        self._by_key: dict[str, str] = {}
        self._current = 0
        self._peak = max(0, int(initial_peak))
        self._events: list[dict] = []
        self._lease_seq = 0

    # -- introspection ---------------------------------------------------
    @property
    def current(self) -> int:
        return self._current

    @property
    def peak(self) -> int:
        return self._peak

    @property
    def available(self) -> int:
        return self.quota - self._current

    def leases(self) -> list[Lease]:
        return sorted(self._leases.values(), key=lambda l: l.lease_id)

    def owner_bytes(self, owner: str) -> int:
        return sum(l.size for l in self._leases.values() if l.owner == owner)

    def snapshot(self) -> dict:
        return {
            "quota_bytes": self.quota,
            "current_bytes": self._current,
            "peak_bytes": self._peak,
            "available_bytes": self.available,
            "lease_count": len(self._leases),
            "owners": {
                owner: self.owner_bytes(owner)
                for owner in sorted({l.owner for l in self._leases.values()})
            },
        }

    def events(self) -> list[dict]:
        return list(self._events)

    # -- leasing ---------------------------------------------------------
    def reserve(self, owner: str, size: int, *, label: str = "") -> str:
        """Reserve ``size`` bytes without writing data yet."""
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise StagingError("lease size must be a non-negative integer", size=size)
        if size > self.available:
            self._events.append({
                "op": "reserve_denied", "owner": owner, "requested": size,
                "current": self._current, "quota": self.quota, "label": label,
            })
            raise QuotaExceeded(
                "scratch quota would be exceeded",
                requested=size, available=self.available, quota=self.quota,
                current=self._current, owner=owner, label=label,
            )
        self._lease_seq += 1
        lease_id = f"L{self._lease_seq:05d}"
        path = self.root / f"{lease_id}.bin"
        lease = Lease(lease_id=lease_id, owner=owner, path=path, size=size)
        self._leases[lease_id] = lease
        self._current += size
        self._peak = max(self._peak, self._current)
        self._events.append({
            "op": "reserve", "lease": lease_id, "owner": owner, "size": size,
            "current": self._current, "peak": self._peak, "label": label,
        })
        return lease_id

    def reserve_named(self, owner: str, size: int, key: str, *,
                      state_dir: Optional[Path] = None, label: str = "") -> str:
        """Reserve a lease identified by ``key`` (re-adoptable across resumes).

        On resume the caller passes the same ``key`` and the previous lease's
        file name so the *already written bytes* on the work disk can be
        re-adopted and re-verified instead of blindly discarded.
        """
        if key in self._by_key:
            raise StagingError("lease key already reserved", key=key)
        lease_id = self.reserve(owner, size, label=label or key)
        self._by_key[key] = lease_id
        self._events.append({"op": "key", "lease": lease_id, "key": key})
        return lease_id

    def adopt(self, owner: str, size: int, key: str, lease_id: str) -> bool:
        """Adopt an existing scratch file into the ledger, re-counting its bytes.

        Returns True if the file existed with the expected size.  If it is
        missing or the wrong size, nothing is adopted and False is returned.
        """
        path = self.root / f"{lease_id}.bin"
        if not path.exists() or path.stat().st_size != size:
            return False
        if lease_id in self._leases:
            return True
        if size > self.available:
            self._events.append({
                "op": "adopt_denied", "owner": owner, "requested": size,
                "current": self._current, "quota": self.quota, "key": key,
            })
            raise QuotaExceeded(
                "cannot re-adopt stored fragments within quota",
                requested=size, available=self.available, quota=self.quota, owner=owner,
            )
        lease = Lease(lease_id=lease_id, owner=owner, path=path, size=size)
        self._leases[lease_id] = lease
        self._by_key[key] = lease_id
        self._current += size
        self._peak = max(self._peak, self._current)
        # keep generated ids monotonic past any adopted id
        if lease_id.startswith("L") and lease_id[1:].isdigit():
            self._lease_seq = max(self._lease_seq, int(lease_id[1:]))
        self._events.append({
            "op": "adopt", "lease": lease_id, "owner": owner, "size": size, "key": key,
            "current": self._current, "peak": self._peak,
        })
        return True

    def cleanup_orphans(self) -> list[str]:
        """Delete scratch ``*.bin`` files no lease currently owns."""
        owned = {l.path.name for l in self._leases.values()}
        removed = []
        for p in sorted(self.root.glob("*.bin")):
            if p.name not in owned:
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError:
                    pass
        return removed

    def lease_for_key(self, key: str) -> Optional[str]:
        return self._by_key.get(key)

    def key_of(self, lease_id: str) -> Optional[str]:
        for k, v in self._by_key.items():
            if v == lease_id:
                return k
        return None

    def path(self, lease_id: str) -> Path:
        if lease_id not in self._leases:
            raise StagingError("unknown lease", lease_id=lease_id)
        return self._leases[lease_id].path

    def write(self, lease_id: str, data: bytes, *, mode: str = "ab") -> int:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise StagingError("unknown lease", lease_id=lease_id)
        if mode == "ab" and lease.path.exists():
            written = lease.path.stat().st_size
            if written + len(data) > lease.size:
                raise StagingError(
                    "write would exceed reserved lease size",
                    lease=lease_id, reserved=lease.size, written=written,
                    additional=len(data),
                )
        elif mode == "wb" and len(data) > lease.size:
            raise StagingError(
                "write would exceed reserved lease size",
                lease=lease_id, reserved=lease.size, additional=len(data),
            )
        with open(lease.path, mode) as fh:
            fh.write(data)
        return len(data)

    def actual_size(self, lease_id: str) -> int:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise StagingError("unknown lease", lease_id=lease_id)
        return lease.path.stat().st_size if lease.path.exists() else 0

    def write_at(self, lease_id: str, offset: int, data: bytes) -> int:
        """Write at an absolute offset inside a reserved lease."""
        lease = self._leases.get(lease_id)
        if lease is None:
            raise StagingError("unknown lease", lease_id=lease_id)
        if offset < 0 or offset + len(data) > lease.size:
            raise StagingError(
                "offset write outside reserved lease",
                lease=lease_id, offset=offset, length=len(data), reserved=lease.size,
            )
        with open(lease.path, "r+b") as fh:
            fh.seek(offset)
            fh.write(data)
        return len(data)

    def preallocate(self, lease_id: str) -> Path:
        """Create the backing file at full reserved size (counts against quota)."""
        lease = self._leases.get(lease_id)
        if lease is None:
            raise StagingError("unknown lease", lease_id=lease_id)
        with open(lease.path, "wb") as fh:
            fh.truncate(lease.size)
        return lease.path

    def read_region(self, lease_id: str, offset: int, length: int) -> bytes:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise StagingError("unknown lease", lease_id=lease_id)
        with open(lease.path, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    def hash_region(self, lease_id: str, offset: int, length: int) -> str:
        import hashlib

        h = hashlib.sha256()
        lease = self._leases.get(lease_id)
        if lease is None:
            raise StagingError("unknown lease", lease_id=lease_id)
        remaining = length
        with open(lease.path, "rb") as fh:
            fh.seek(offset)
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
        if remaining != 0:
            raise StagingError(
                "short read while hashing region", lease=lease_id, missing=remaining,
            )
        return h.hexdigest()

    def hash_file(self, lease_id: str) -> str:
        import hashlib

        lease = self._leases.get(lease_id)
        if lease is None:
            raise StagingError("unknown lease", lease_id=lease_id)
        h = hashlib.sha256()
        with open(lease.path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def release(self, lease_id: str, *, reason: str = "released",
                remove_file: bool = True) -> None:
        lease = self._leases.pop(lease_id, None)
        if lease is None:
            raise StagingError("unknown or already released lease", lease_id=lease_id)
        for k, v in list(self._by_key.items()):
            if v == lease_id:
                self._by_key.pop(k, None)
        if remove_file:
            try:
                lease.path.unlink()
            except FileNotFoundError:
                pass
        self._current -= lease.size
        self._events.append({
            "op": "release", "lease": lease_id, "owner": lease.owner,
            "size": lease.size, "reason": reason, "removed": remove_file,
            "current": self._current, "peak": self._peak,
        })

    def release_owner(self, owner: str, *, reason: str = "owner_released") -> int:
        freed = 0
        for lid in [l.lease_id for l in self._leases.values() if l.owner == owner]:
            freed += self._leases[lid].size
            self.release(lid, reason=reason)
        return freed

    def clear(self, *, remove_dir: bool = False) -> None:
        for lid in list(self._leases):
            try:
                self.release(lid, reason="cleared")
            except StagingError:
                pass
        if remove_dir:
            shutil.rmtree(self.root, ignore_errors=True)
            try:
                self.root.rmdir()
            except OSError:
                pass


class Workspace:
    """Groups the directories the restorer is allowed to touch."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.work_dir = self.root / "work"
        self.state_dir = self.root / "state"
        self.output_dir = self.root / "out"
        for d in (self.work_dir, self.state_dir, self.output_dir):
            d.mkdir(parents=True, exist_ok=True)
