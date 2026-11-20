"""Independent room-state service.

This module knows about physical rooms, their fixed attributes and maintenance.
It never looks at bookings: the front desk can query and maintain the real
hotel state even when the fulfilment vault is empty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .db import Database, utcnow
from .errors import (
    AlreadyExists,
    NotFound,
    RecordRevoked,
    RevisionConflict,
    StaleRevision,
    ValidationError,
)
from .identity import canonical_json, digest


# ---------------------------------------------------------------------------
# Interval helpers


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union left-closed/right-open integer intervals into sorted disjoint runs."""

    ordered = sorted((int(s), int(e)) for s, e in intervals if int(s) < int(e))
    merged: list[list[int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def subtract_intervals(
    window: tuple[int, int], blocked: Iterable[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Return the parts of *window* not covered by *blocked*."""

    wstart, wend = window
    if wstart >= wend:
        return []
    free: list[tuple[int, int]] = []
    cursor = wstart
    for start, end in merge_intervals(blocked):
        if end <= cursor:
            continue
        if start >= wend:
            break
        if start > cursor:
            free.append((cursor, min(start, wend)))
        cursor = max(cursor, end)
        if cursor >= wend:
            break
    if cursor < wend:
        free.append((cursor, wend))
    return free


def covers(blocked: Iterable[tuple[int, int]], start: int, end: int) -> bool:
    """True when [start, end) is fully covered by the union of *blocked*."""

    if start >= end:
        return False
    cursor = start
    for bstart, bend in merge_intervals(blocked):
        if bend <= cursor:
            continue
        if bstart > cursor:
            return False
        cursor = max(cursor, bend)
        if cursor >= end:
            return True
    return cursor >= end


def intersects(blocked: Iterable[tuple[int, int]], start: int, end: int) -> bool:
    """True when any blocked interval intersects [start, end)."""

    if start >= end:
        return False
    for bstart, bend in merge_intervals(blocked):
        if bstart >= end:
            return False
        if bend > start:
            return True
    return False


# ---------------------------------------------------------------------------
# Immutable state product


@dataclass(frozen=True)
class StateProduct:
    """A committed, immutable view of room availability at one version.

    The product is a value object: callers cannot mutate the database through
    it, and product digests are stable forever.
    """

    version: int
    horizon: tuple[int, int]
    rooms: Mapping[str, Mapping[str, Any]]
    digest: str
    product_json: str
    created_at: str

    # -- lookups ---------------------------------------------------------

    def room_ids(self) -> list[str]:
        return sorted(self.rooms)

    def has_room(self, room_id: str) -> bool:
        return room_id in self.rooms

    def room(self, room_id: str) -> Mapping[str, Any]:
        try:
            return self.rooms[room_id]
        except KeyError as exc:
            raise NotFound(f"room {room_id!r} not registered at v{self.version}") from exc

    def level(self, room_id: str) -> int:
        return int(self.room(room_id)["level"])

    def capacity(self, room_id: str) -> int:
        return int(self.room(room_id)["capacity"])

    def cleaning_days(self, room_id: str) -> int:
        return int(self.room(room_id)["cleaning_days"])

    def blocked_intervals(self, room_id: str) -> list[tuple[int, int]]:
        return [(int(s), int(e)) for s, e in self.room(room_id)["blocked"]]

    def available_intervals(
        self,
        room_id: str,
        start: int | None = None,
        end: int | None = None,
    ) -> list[tuple[int, int]]:
        """Available day runs for *room_id*.

        With no window the product horizon is used.  Maintenance is a finite
        set of intervals, so any explicitly requested window is answered
        directly; the answer never depends on state versions discovered later.
        """

        room = self.room(room_id)
        hstart, hend = self.horizon
        qstart = hstart if start is None else max(int(start), hstart)
        qend = hend if end is None else min(int(end), hend)
        if qstart >= qend:
            return []
        return subtract_intervals((qstart, qend), room["blocked"])

    def is_available(self, room_id: str, start: int, end: int) -> bool:
        """True when no blocked maintenance intersects [start, end)."""

        return not intersects(self.blocked_intervals(room_id), int(start), int(end))

    def maintenance_sources(self, room_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.room(room_id)["maintenance"]]

    def to_dict(self) -> dict[str, Any]:
        # Re-parse so callers can never mutate the stored structure in place.
        return json.loads(self.product_json)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"StateProduct(version={self.version}, digest={self.digest[:12]}, "
            f"rooms={len(self.rooms)})"
        )


# ---------------------------------------------------------------------------
# Service


_MAINT_SQL = """
SELECT maintenance_id, revision, op, action, room_id, start_day, end_day,
       content_hash, committed_version, created_at
FROM maintenance_events
WHERE committed_version <= ?
ORDER BY maintenance_id, revision
"""

_ROOM_SQL = """
SELECT room_id, level, capacity, cleaning_days, registered_version
FROM rooms
WHERE registered_version <= ?
ORDER BY room_id
"""


class RoomStateService:
    """Register rooms and versioned maintenance; publish immutable products."""

    def __init__(
        self,
        db: Database,
        horizon: tuple[int, int] = (0, 30),
    ) -> None:
        hstart, hend = horizon
        if int(hstart) >= int(hend):
            raise ValidationError("horizon must be a non-empty [start, end) window")
        self.db = db
        self.horizon = (int(hstart), int(hend))

    # -- versions --------------------------------------------------------

    def current_version(self) -> int:
        return int(self.db.scalar("SELECT MAX(version) FROM state_versions", default=0))

    def versions(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT version, kind, detail, created_at FROM state_versions"
            " ORDER BY version"
        )
        return [dict(row) for row in rows]

    def _commit_version(self, kind: str, detail: Mapping[str, Any]) -> int:
        nxt = self.current_version() + 1
        self.db.execute(
            "INSERT INTO state_versions(version, kind, detail, created_at)"
            " VALUES (?, ?, ?, ?)",
            (nxt, kind, canonical_json(detail), utcnow()),
        )
        return nxt

    # -- rooms -----------------------------------------------------------

    def register_room(
        self,
        room_id: str,
        level: int,
        capacity: int,
        cleaning_days: int,
    ) -> int:
        """Register a physical room.

        Identical re-registration is a no-op and returns the original version;
        a conflicting registration is rejected because room attributes are
        fixed for the life of the room.
        """

        if not isinstance(room_id, str) or not room_id.strip():
            raise ValidationError("room_id must be a non-empty string")
        level = int(level)
        capacity = int(capacity)
        cleaning_days = int(cleaning_days)
        if level < 1:
            raise ValidationError("level must be >= 1")
        if capacity < 1:
            raise ValidationError("capacity must be >= 1")
        if cleaning_days < 0:
            raise ValidationError("cleaning_days must be >= 0")

        with self.db.tx():
            row = self.db.one("SELECT * FROM rooms WHERE room_id = ?", (room_id,))
            if row is not None:
                if (
                    int(row["level"]) == level
                    and int(row["capacity"]) == capacity
                    and int(row["cleaning_days"]) == cleaning_days
                ):
                    return int(row["registered_version"])
                raise AlreadyExists(
                    f"room {room_id!r} already registered with different attributes"
                )
            version = self._commit_version(
                "room_registered",
                {
                    "room_id": room_id,
                    "level": level,
                    "capacity": capacity,
                    "cleaning_days": cleaning_days,
                },
            )
            self.db.execute(
                "INSERT INTO rooms(room_id, level, capacity, cleaning_days,"
                " registered_version) VALUES (?, ?, ?, ?, ?)",
                (room_id, level, capacity, cleaning_days, version),
            )
            return version

    def room(self, room_id: str) -> dict[str, Any]:
        row = self.db.one(
            "SELECT room_id, level, capacity, cleaning_days, registered_version"
            " FROM rooms WHERE room_id = ?",
            (room_id,),
        )
        if row is None:
            raise NotFound(f"room {room_id!r} is not registered")
        return dict(row)

    def rooms(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT room_id, level, capacity, cleaning_days, registered_version"
            " FROM rooms ORDER BY room_id"
        )
        return [dict(row) for row in rows]

    # -- maintenance -----------------------------------------------------

    @staticmethod
    def _maint_content(
        maintenance_id: str,
        revision: int,
        op: str,
        room_id: str,
        start: int | None,
        end: int | None,
    ) -> dict[str, Any]:
        return {
            "kind": "maintenance",
            "maintenance_id": maintenance_id,
            "revision": int(revision),
            "op": op,
            "room_id": room_id,
            "start": None if start is None else int(start),
            "end": None if end is None else int(end),
        }

    def set_maintenance(
        self,
        maintenance_id: str,
        revision: int,
        op: str,
        room_id: str,
        start: int | None = None,
        end: int | None = None,
    ) -> dict[str, Any]:
        """Add, replace or revoke one maintenance record.

        Returns a receipt with the committed state version.  Idempotent
        resubmission of an already committed ``(maintenance_id, revision)``
        with identical content performs no write and returns the original
        version.  A different payload at the same revision is rejected, as is
        any attempt to revive a revoked record.
        """

        if not isinstance(maintenance_id, str) or not maintenance_id.strip():
            raise ValidationError("maintenance_id must be a non-empty string")
        revision = int(revision)
        if revision < 1:
            raise ValidationError("revision must be a positive integer")
        op = str(op).lower()
        if op not in {"add", "replace", "revoke"}:
            raise ValidationError("op must be 'add', 'replace' or 'revoke'")
        if not isinstance(room_id, str) or not room_id.strip():
            raise ValidationError("room_id must be a non-empty string")

        if op == "revoke":
            if start is not None or end is not None:
                raise ValidationError("revoke must not carry an interval")
        else:
            if start is None or end is None:
                raise ValidationError(f"{op} requires start and end")
            start, end = int(start), int(end)
            if start >= end:
                raise ValidationError("maintenance must satisfy start < end")

        content = self._maint_content(
            maintenance_id, revision, op, room_id,
            None if op == "revoke" else start,
            None if op == "revoke" else end,
        )
        content_hash = digest(content)

        with self.db.tx():
            if self.db.one("SELECT 1 FROM rooms WHERE room_id = ?", (room_id,)) is None:
                raise NotFound(f"room {room_id!r} is not registered")

            existing = self.db.one(
                "SELECT * FROM maintenance_events WHERE maintenance_id = ?"
                " AND revision = ?",
                (maintenance_id, revision),
            )
            head = self.db.one(
                "SELECT * FROM maintenance_head WHERE maintenance_id = ?",
                (maintenance_id,),
            )

            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise RevisionConflict(
                        f"maintenance {maintenance_id!r} revision {revision} "
                        "already committed with different content"
                    )
                return self._maint_receipt(existing, replayed=True)

            if head is None:
                if op != "add":
                    raise NotFound(
                        f"maintenance {maintenance_id!r} has no revision to {op}"
                    )
                if revision != 1:
                    raise ValidationError("a new maintenance record must start at revision 1")
            else:
                head_revision = int(head["revision"])
                if revision < head_revision:
                    raise StaleRevision(
                        f"maintenance {maintenance_id!r} revision {revision} "
                        f"is older than head revision {head_revision}"
                    )
                if revision == head_revision:
                    # Unreachable when existing is None, but keeps the
                    # invariant explicit if storage changes later.
                    raise RevisionConflict(
                        f"revision {revision} is already the head for "
                        f"{maintenance_id!r}"
                    )
                if head["action"] == "revoke":
                    raise RecordRevoked(
                        f"maintenance {maintenance_id!r} is revoked; it cannot be revived"
                    )
                if op == "add":
                    raise AlreadyExists(
                        f"maintenance {maintenance_id!r} already exists; use 'replace'"
                    )
                if head["room_id"] != room_id:
                    raise ValidationError(
                        f"{op} must keep the maintenance record on the same room"
                    )

            action = "revoke" if op == "revoke" else "set"
            version = self._commit_version(
                f"maintenance_{op}", content
            )
            self.db.execute(
                "INSERT INTO maintenance_events(maintenance_id, revision, op,"
                " action, room_id, start_day, end_day, content_hash,"
                " committed_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    maintenance_id,
                    revision,
                    op,
                    action,
                    room_id,
                    None if action == "revoke" else start,
                    None if action == "revoke" else end,
                    content_hash,
                    version,
                    utcnow(),
                ),
            )
            self.db.execute(
                "INSERT INTO maintenance_head(maintenance_id, revision, op, action,"
                " room_id, start_day, end_day, content_hash, committed_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(maintenance_id) DO UPDATE SET"
                " revision=excluded.revision, op=excluded.op,"
                " action=excluded.action, room_id=excluded.room_id,"
                " start_day=excluded.start_day, end_day=excluded.end_day,"
                " content_hash=excluded.content_hash,"
                " committed_version=excluded.committed_version",
                (
                    maintenance_id,
                    revision,
                    op,
                    action,
                    room_id,
                    None if action == "revoke" else start,
                    None if action == "revoke" else end,
                    content_hash,
                    version,
                ),
            )
            row = self.db.one(
                "SELECT * FROM maintenance_events WHERE maintenance_id = ?"
                " AND revision = ?",
                (maintenance_id, revision),
            )
            assert row is not None
            return self._maint_receipt(row, replayed=False)

    def _maint_receipt(self, row: Any, replayed: bool) -> dict[str, Any]:
        return {
            "maintenance_id": row["maintenance_id"],
            "revision": int(row["revision"]),
            "op": row["op"],
            "action": row["action"],
            "room_id": row["room_id"],
            "start": row["start_day"],
            "end": row["end_day"],
            "committed_version": int(row["committed_version"]),
            "replayed": replayed,
        }

    # Convenience wrappers -------------------------------------------------

    def add_maintenance(
        self, maintenance_id: str, room_id: str, start: int, end: int,
        revision: int = 1,
    ) -> dict[str, Any]:
        return self.set_maintenance(maintenance_id, revision, "add", room_id, start, end)

    def replace_maintenance(
        self, maintenance_id: str, revision: int, room_id: str, start: int, end: int
    ) -> dict[str, Any]:
        return self.set_maintenance(
            maintenance_id, revision, "replace", room_id, start, end
        )

    def revoke_maintenance(
        self, maintenance_id: str, revision: int, room_id: str | None = None
    ) -> dict[str, Any]:
        if room_id is None:
            head = self.db.one(
                "SELECT room_id FROM maintenance_head WHERE maintenance_id = ?",
                (maintenance_id,),
            )
            if head is None:
                raise NotFound(f"maintenance {maintenance_id!r} does not exist")
            room_id = head["room_id"]
        return self.set_maintenance(maintenance_id, revision, "revoke", room_id)

    def maintenance_history(self, maintenance_id: str) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM maintenance_events WHERE maintenance_id = ?"
            " ORDER BY revision",
            (maintenance_id,),
        )
        if not rows:
            raise NotFound(f"maintenance {maintenance_id!r} does not exist")
        return [self._maint_receipt(row, replayed=False) for row in rows]

    def maintenance_comments(self) -> list[dict[str, Any]]:
        """Current head per maintenance record (including revocations)."""

        rows = self.db.all("SELECT * FROM maintenance_head ORDER BY maintenance_id")
        return [
            {
                "maintenance_id": row["maintenance_id"],
                "revision": int(row["revision"]),
                "op": row["op"],
                "action": row["action"],
                "room_id": row["room_id"],
                "start": row["start_day"],
                "end": row["end_day"],
                "committed_version": int(row["committed_version"]),
            }
            for row in rows
        ]

    # -- products --------------------------------------------------------

    def product(self, version: int | None = None) -> StateProduct:
        """Return the immutable product for a committed version.

        The product is persisted the first time it is requested.  Later state
        changes never touch it.
        """

        if version is None:
            version = self.current_version()
        version = int(version)
        if version < 0 or version > self.current_version():
            raise NotFound(f"state version {version} does not exist")

        row = self.db.one(
            "SELECT * FROM state_products WHERE version = ?"
            " AND horizon_start = ? AND horizon_end = ?",
            (version, self.horizon[0], self.horizon[1]),
        )
        if row is not None:
            return self._product_from_row(row)

        content = self._build_product(version)
        payload = canonical_json(content)
        product_digest = digest(content)
        now = utcnow()
        with self.db.tx():
            row = self.db.one(
                "SELECT * FROM state_products WHERE version = ?"
                " AND horizon_start = ? AND horizon_end = ?",
                (version, self.horizon[0], self.horizon[1]),
            )
            if row is None:
                self.db.execute(
                    "INSERT OR IGNORE INTO state_products(version, horizon_start,"
                    " horizon_end, digest, product_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        version,
                        self.horizon[0],
                        self.horizon[1],
                        product_digest,
                        payload,
                        now,
                    ),
                )
                row = self.db.one(
                    "SELECT * FROM state_products WHERE version = ?"
                    " AND horizon_start = ? AND horizon_end = ?",
                    (version, self.horizon[0], self.horizon[1]),
                )
        assert row is not None
        return self._product_from_row(row)

    def product_by_digest(self, product_digest: str) -> StateProduct:
        """Return the most recently published product with this content digest."""

        row = self.db.one(
            "SELECT * FROM state_products WHERE digest = ?"
            " ORDER BY version DESC, horizon_start DESC LIMIT 1",
            (product_digest,),
        )
        if row is None:
            raise NotFound(f"state product {product_digest!r} does not exist")
        return self._product_from_row(row)

    def products(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM state_products ORDER BY version, horizon_start"
        )
        return [
            {
                "version": int(row["version"]),
                "horizon": [int(row["horizon_start"]), int(row["horizon_end"])],
                "digest": row["digest"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _product_from_row(self, row: Any) -> StateProduct:
        content = json.loads(row["product_json"])
        rooms = content["rooms"]
        return StateProduct(
            version=int(row["version"]),
            horizon=(int(row["horizon_start"]), int(row["horizon_end"])),
            rooms={rid: rooms[rid] for rid in sorted(rooms)},
            digest=row["digest"],
            product_json=row["product_json"],
            created_at=row["created_at"],
        )

    def _build_product(self, version: int) -> dict[str, Any]:
        room_rows = self.db.all(_ROOM_SQL, (version,))
        events = self.db.all(_MAINT_SQL, (version,))

        # Latest committed revision per maintenance record, as of *version*.
        latest: dict[str, dict[str, Any]] = {}
        for event in events:
            mid = event["maintenance_id"]
            prev = latest.get(mid)
            if prev is None or int(event["revision"]) > int(prev["revision"]):
                latest[mid] = dict(event)

        by_room: dict[str, list[dict[str, Any]]] = {}
        for event in latest.values():
            if event["action"] != "set":
                continue
            if event["start_day"] is None or event["end_day"] is None:
                continue
            by_room.setdefault(event["room_id"], []).append(event)

        rooms: dict[str, Any] = {}
        for room in room_rows:
            rid = room["room_id"]
            sources = sorted(
                by_room.get(rid, []),
                key=lambda e: (int(e["start_day"]), int(e["end_day"]),
                               e["maintenance_id"], int(e["revision"])),
            )
            blocked = merge_intervals(
                (int(e["start_day"]), int(e["end_day"])) for e in sources
            )
            rooms[rid] = {
                "level": int(room["level"]),
                "capacity": int(room["capacity"]),
                "cleaning_days": int(room["cleaning_days"]),
                "registered_version": int(room["registered_version"]),
                "blocked": [[s, e] for s, e in blocked],
                "maintenance": [
                    {
                        "maintenance_id": e["maintenance_id"],
                        "revision": int(e["revision"]),
                        "start": int(e["start_day"]),
                        "end": int(e["end_day"]),
                    }
                    for e in sources
                ],
            }

        return {
            "kind": "room_state_product",
            "version": version,
            "horizon": [self.horizon[0], self.horizon[1]],
            "rooms": rooms,
        }
