"""SQLite plumbing shared by the room-state and fulfilment modules.

The two modules deliberately only share this small database helper.  Callers
may give them the same :class:`Database` (and therefore transactions) or reopen
the same file independently; both styles are supported.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


SCHEMA = """
CREATE TABLE IF NOT EXISTS state_versions (
    version      INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    detail       TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    room_id            TEXT PRIMARY KEY,
    level              INTEGER NOT NULL,
    capacity           INTEGER NOT NULL,
    cleaning_days      INTEGER NOT NULL CHECK (cleaning_days >= 0),
    registered_version INTEGER NOT NULL,
    FOREIGN KEY (registered_version) REFERENCES state_versions(version)
);

CREATE TABLE IF NOT EXISTS maintenance_events (
    maintenance_id  TEXT NOT NULL,
    revision        INTEGER NOT NULL CHECK (revision > 0),
    op              TEXT NOT NULL,          -- add / replace / revoke
    action          TEXT NOT NULL,          -- set / revoke
    room_id         TEXT NOT NULL,
    start_day       INTEGER,
    end_day         INTEGER,
    content_hash    TEXT NOT NULL,
    committed_version INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (maintenance_id, revision),
    FOREIGN KEY (room_id) REFERENCES rooms(room_id),
    FOREIGN KEY (committed_version) REFERENCES state_versions(version)
);
CREATE INDEX IF NOT EXISTS ix_maint_version
    ON maintenance_events(committed_version);

CREATE TABLE IF NOT EXISTS maintenance_head (
    maintenance_id  TEXT PRIMARY KEY,
    revision        INTEGER NOT NULL,
    op              TEXT NOT NULL,
    action          TEXT NOT NULL,
    room_id         TEXT NOT NULL,
    start_day       INTEGER,
    end_day         INTEGER,
    content_hash    TEXT NOT NULL,
    committed_version INTEGER NOT NULL,
    FOREIGN KEY (room_id) REFERENCES rooms(room_id)
);

CREATE TABLE IF NOT EXISTS booking_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id  TEXT NOT NULL,
    revision    INTEGER NOT NULL CHECK (revision > 0),
    action      TEXT NOT NULL,              -- active / cancelled
    arrival     INTEGER NOT NULL,
    departure   INTEGER NOT NULL,
    party_size  INTEGER NOT NULL,
    min_level   INTEGER NOT NULL,
    max_level   INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (booking_id, revision)
);
CREATE INDEX IF NOT EXISTS ix_booking_events_seq ON booking_events(seq);

CREATE TABLE IF NOT EXISTS booking_head (
    booking_id  TEXT PRIMARY KEY,
    revision    INTEGER NOT NULL,
    action      TEXT NOT NULL,
    arrival     INTEGER NOT NULL,
    departure   INTEGER NOT NULL,
    party_size  INTEGER NOT NULL,
    min_level   INTEGER NOT NULL,
    max_level   INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    label               TEXT,
    state_version       INTEGER NOT NULL,
    product_digest      TEXT NOT NULL,
    product_json        TEXT NOT NULL,
    slice_digest        TEXT NOT NULL,
    slice_json          TEXT NOT NULL,
    objective_json      TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_assignments (
    plan_id        INTEGER NOT NULL,
    booking_id     TEXT NOT NULL,
    room_id        TEXT NOT NULL,
    arrival        INTEGER NOT NULL,
    departure      INTEGER NOT NULL,
    party_size     INTEGER NOT NULL,
    min_level      INTEGER NOT NULL,
    max_level      INTEGER NOT NULL,
    room_level     INTEGER NOT NULL,
    upgrade_delta  INTEGER NOT NULL,
    PRIMARY KEY (plan_id, booking_id),
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
);

CREATE TABLE IF NOT EXISTS plan_unsatisfied (
    plan_id     INTEGER NOT NULL,
    booking_id  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (plan_id, booking_id),
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
);

-- Published plans are historical evidence; never update them in place.
CREATE TRIGGER IF NOT EXISTS plans_no_update
BEFORE UPDATE ON plans
BEGIN SELECT RAISE(ABORT, 'plans are immutable'); END;

CREATE TRIGGER IF NOT EXISTS plans_no_delete
BEFORE DELETE ON plans
BEGIN SELECT RAISE(ABORT, 'plans are immutable'); END;

CREATE TRIGGER IF NOT EXISTS plan_assignments_no_update
BEFORE UPDATE ON plan_assignments
BEGIN SELECT RAISE(ABORT, 'plan assignments are immutable'); END;

CREATE TRIGGER IF NOT EXISTS plan_assignments_no_delete
BEFORE DELETE ON plan_assignments
BEGIN SELECT RAISE(ABORT, 'plan assignments are immutable'); END;

CREATE TRIGGER IF NOT EXISTS plan_unsatisfied_no_update
BEFORE UPDATE ON plan_unsatisfied
BEGIN SELECT RAISE(ABORT, 'unsatisfied records are immutable'); END;

CREATE TRIGGER IF NOT EXISTS plan_unsatisfied_no_delete
BEFORE DELETE ON plan_unsatisfied
BEGIN SELECT RAISE(ABORT, 'unsatisfied records are immutable'); END;

-- A state product is the rooms/availability snapshot at one committed
-- version.  Identical content at a later version is still a distinct
-- historical product, hence the composite primary key.
CREATE TABLE IF NOT EXISTS state_products (
    version        INTEGER NOT NULL,
    horizon_start  INTEGER NOT NULL,
    horizon_end    INTEGER NOT NULL,
    digest         TEXT NOT NULL,
    product_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (version, horizon_start, horizon_end),
    FOREIGN KEY (version) REFERENCES state_versions(version)
);
CREATE INDEX IF NOT EXISTS ix_products_digest ON state_products(digest);
CREATE TRIGGER IF NOT EXISTS state_products_no_update
BEFORE UPDATE ON state_products
BEGIN SELECT RAISE(ABORT, 'state products are immutable'); END;
CREATE TRIGGER IF NOT EXISTS state_products_no_delete
BEFORE DELETE ON state_products
BEGIN SELECT RAISE(ABORT, 'state products are immutable'); END;

CREATE TRIGGER IF NOT EXISTS maintenance_events_no_update
BEFORE UPDATE ON maintenance_events
BEGIN SELECT RAISE(ABORT, 'maintenance history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS maintenance_events_no_delete
BEFORE DELETE ON maintenance_events
BEGIN SELECT RAISE(ABORT, 'maintenance history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS booking_events_no_update
BEFORE UPDATE ON booking_events
BEGIN SELECT RAISE(ABORT, 'booking history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS booking_events_no_delete
BEFORE DELETE ON booking_events
BEGIN SELECT RAISE(ABORT, 'booking history is immutable'); END;
"""


def utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Database:
    """A tiny SQLite wrapper with one re-entrant lock and explicit transactions."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.path,
            isolation_level=None,  # explicit BEGIN/COMMIT below
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            row = self.conn.execute(
                "SELECT version FROM state_versions WHERE version = 0"
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO state_versions(version, kind, detail, created_at)"
                    " VALUES (0, 'baseline', 'empty hotel', ?)",
                    (utcnow(),),
                )

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Serialise writers and make a group of statements atomic."""

        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(
        self, sql: str, params: Iterable[Sequence[Any]]
    ) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executemany(sql, params)

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        if row is None or row[0] is None:
            return default
        return row[0]

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
