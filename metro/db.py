"""SQLite persistence for the metro explainable-flow warehouse.

The schema is deliberately batch-scoped: every analysis batch points at one
immutable timetable snapshot and stores its own pairing, candidate plans,
interval bounds and witness assignments.  A timetable correction therefore
creates a new snapshot and new analysis batches, while old batches remain
queryable against exactly the data that produced them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stations (
    station      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    entry_walk   INTEGER NOT NULL CHECK (entry_walk >= 0),
    exit_walk    INTEGER NOT NULL CHECK (exit_walk >= 0),
    min_transfer INTEGER NOT NULL CHECK (min_transfer >= 0)
);

CREATE TABLE IF NOT EXISTS timetable_snapshots (
    snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    base_snapshot_id INTEGER REFERENCES timetable_snapshots(snapshot_id),
    note             TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshot_stops (
    snapshot_id INTEGER NOT NULL REFERENCES timetable_snapshots(snapshot_id) ON DELETE CASCADE,
    train_id    TEXT NOT NULL,
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    station     TEXT NOT NULL REFERENCES stations(station),
    arr_ts      INTEGER NOT NULL,
    dep_ts      INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, train_id, seq),
    CHECK (arr_ts <= dep_ts)
);
CREATE INDEX IF NOT EXISTS idx_stops_station
    ON snapshot_stops(snapshot_id, station, arr_ts);
CREATE INDEX IF NOT EXISTS idx_stops_train
    ON snapshot_stops(snapshot_id, train_id, seq);

CREATE TABLE IF NOT EXISTS gate_imports (
    import_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    raw_rows       INTEGER NOT NULL,
    unique_events  INTEGER NOT NULL,
    duplicate_rows INTEGER NOT NULL,
    status         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_events (
    import_id INTEGER NOT NULL REFERENCES gate_imports(import_id) ON DELETE CASCADE,
    card_id   TEXT NOT NULL,
    event_id  TEXT NOT NULL,
    station   TEXT NOT NULL REFERENCES stations(station),
    ts        INTEGER NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('IN', 'OUT')),
    PRIMARY KEY (import_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_card
    ON gate_events(import_id, card_id, ts);

CREATE TABLE IF NOT EXISTS analysis_batches (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL REFERENCES timetable_snapshots(snapshot_id),
    import_id       INTEGER NOT NULL REFERENCES gate_imports(import_id),
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    n_trips         INTEGER NOT NULL DEFAULT 0,
    n_explainable   INTEGER NOT NULL DEFAULT 0,
    n_unexplainable INTEGER NOT NULL DEFAULT 0,
    n_plans         INTEGER NOT NULL DEFAULT 0,
    n_intervals     INTEGER NOT NULL DEFAULT 0,
    stats_json      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_batches_snapshot
    ON analysis_batches(snapshot_id, import_id);

CREATE TABLE IF NOT EXISTS trip_pairs (
    batch_id       INTEGER NOT NULL REFERENCES analysis_batches(batch_id) ON DELETE CASCADE,
    trip_id        TEXT NOT NULL,
    card_id        TEXT NOT NULL,
    entry_event_id TEXT NOT NULL,
    exit_event_id  TEXT NOT NULL,
    entry_station  TEXT NOT NULL,
    entry_ts       INTEGER NOT NULL,
    exit_station   TEXT NOT NULL,
    exit_ts        INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('EXPLAINABLE', 'UNEXPLAINABLE')),
    n_plans        INTEGER NOT NULL,
    n_direct       INTEGER NOT NULL,
    n_transfer     INTEGER NOT NULL,
    baseline_plan_id TEXT,
    diag_json      TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (batch_id, trip_id)
);
CREATE INDEX IF NOT EXISTS idx_trips_status
    ON trip_pairs(batch_id, status);

CREATE TABLE IF NOT EXISTS unclosed_entries (
    batch_id INTEGER NOT NULL REFERENCES analysis_batches(batch_id) ON DELETE CASCADE,
    card_id  TEXT NOT NULL,
    event_id TEXT NOT NULL,
    station  TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    reason   TEXT NOT NULL,
    PRIMARY KEY (batch_id, event_id)
);

CREATE TABLE IF NOT EXISTS orphan_exits (
    batch_id INTEGER NOT NULL REFERENCES analysis_batches(batch_id) ON DELETE CASCADE,
    card_id  TEXT NOT NULL,
    event_id TEXT NOT NULL,
    station  TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    reason   TEXT NOT NULL,
    PRIMARY KEY (batch_id, event_id)
);

CREATE TABLE IF NOT EXISTS candidate_plans (
    batch_id        INTEGER NOT NULL,
    trip_id         TEXT NOT NULL,
    plan_id         TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('DIRECT', 'TRANSFER')),
    train1          TEXT NOT NULL,
    board_seq1      INTEGER NOT NULL,
    alight_seq1     INTEGER NOT NULL,
    board_station1  TEXT NOT NULL,
    alight_station1 TEXT NOT NULL,
    board_dep1      INTEGER NOT NULL,
    alight_arr1     INTEGER NOT NULL,
    train2          TEXT,
    board_seq2      INTEGER,
    alight_seq2     INTEGER,
    board_station2  TEXT,
    alight_station2 TEXT,
    board_dep2      INTEGER,
    alight_arr2     INTEGER,
    transfer_station TEXT,
    transfer_min    INTEGER,
    transfer_slack  INTEGER,
    signature       TEXT NOT NULL,
    sort_key        TEXT NOT NULL,
    PRIMARY KEY (batch_id, trip_id, plan_id),
    FOREIGN KEY (batch_id, trip_id)
        REFERENCES trip_pairs(batch_id, trip_id) ON DELETE CASCADE,
    CHECK (board_seq1 < alight_seq1),
    CHECK (kind = 'DIRECT' OR
           (train2 IS NOT NULL AND board_seq2 < alight_seq2 AND train1 <> train2))
);
CREATE INDEX IF NOT EXISTS idx_plans_trip
    ON candidate_plans(batch_id, trip_id, sort_key);
CREATE INDEX IF NOT EXISTS idx_plans_train
    ON candidate_plans(batch_id, train1);

CREATE TABLE IF NOT EXISTS plan_interval_uses (
    batch_id INTEGER NOT NULL,
    trip_id  TEXT NOT NULL,
    plan_id  TEXT NOT NULL,
    leg_no   INTEGER NOT NULL,
    train_id TEXT NOT NULL,
    from_seq INTEGER NOT NULL,
    to_seq   INTEGER NOT NULL,
    PRIMARY KEY (batch_id, trip_id, plan_id, leg_no, from_seq),
    FOREIGN KEY (batch_id, trip_id, plan_id)
        REFERENCES candidate_plans(batch_id, trip_id, plan_id) ON DELETE CASCADE,
    CHECK (from_seq < to_seq)
);
CREATE INDEX IF NOT EXISTS idx_interval_uses
    ON plan_interval_uses(batch_id, train_id, from_seq);

CREATE TABLE IF NOT EXISTS witness_sets (
    witness_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES analysis_batches(batch_id) ON DELETE CASCADE,
    train_id    TEXT NOT NULL,
    from_seq    INTEGER NOT NULL,
    bound       TEXT NOT NULL CHECK (bound IN ('MIN', 'MAX')),
    n_overrides INTEGER NOT NULL,
    rule        TEXT NOT NULL,
    selection_digest TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    UNIQUE (batch_id, train_id, from_seq, bound)
);

-- Compact witnesses: the bound is replayed from the canonical candidate plans
-- using ``rule``; ``selection_digest`` and ``n_overrides`` pin the exact
-- per-trip choices so any divergence is detectable.

CREATE TABLE IF NOT EXISTS interval_flows (
    batch_id     INTEGER NOT NULL REFERENCES analysis_batches(batch_id) ON DELETE CASCADE,
    train_id     TEXT NOT NULL,
    from_seq     INTEGER NOT NULL,
    to_seq       INTEGER NOT NULL,
    station_from TEXT NOT NULL,
    station_to   TEXT NOT NULL,
    min_count    INTEGER NOT NULL,
    max_count    INTEGER NOT NULL,
    min_witness_id INTEGER NOT NULL,
    max_witness_id INTEGER NOT NULL,
    PRIMARY KEY (batch_id, train_id, from_seq)
);
CREATE INDEX IF NOT EXISTS idx_flows_bounds
    ON interval_flows(batch_id, max_count, min_count);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def chunks(iterable: Iterable, size: int = 500):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
