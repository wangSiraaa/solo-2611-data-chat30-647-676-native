"""SQLite persistence layer.

All state lives in a single SQLite database (WAL mode) — never only in
memory. Mutations that must be atomic run inside ``BEGIN IMMEDIATE``
transactions so concurrent clients serialize on the database write lock:
the loser of a race re-reads state inside its transaction and therefore
sees the winner's committed reservations.

Sealed boxes are immutable *by database enforcement*: triggers abort any
UPDATE/DELETE on ``sealed_boxes`` / ``sealed_box_items``.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .errors import (NotFoundError, ReservationConflictError,
                     VersionConflictError)
from .models import (Item, PlanStatus, SealedBox, SealedItem, Shift,
                     TransportAssignment, TransportPlan)

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS items_current (
    unit_id   TEXT PRIMARY KEY,
    length    INTEGER NOT NULL,
    width     INTEGER NOT NULL,
    weight    INTEGER NOT NULL,
    zone      TEXT NOT NULL,
    version   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS item_history (
    unit_id   TEXT NOT NULL,
    version   INTEGER NOT NULL,
    length    INTEGER NOT NULL,
    width     INTEGER NOT NULL,
    weight    INTEGER NOT NULL,
    zone      TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    PRIMARY KEY (unit_id, version)
);

CREATE TABLE IF NOT EXISTS box_types (
    box_type_id      TEXT PRIMARY KEY,
    inner_length     INTEGER NOT NULL,
    inner_width      INTEGER NOT NULL,
    empty_weight     INTEGER NOT NULL,
    max_gross_weight INTEGER NOT NULL,
    allowed_zones    TEXT NOT NULL  -- JSON array
);

CREATE TABLE IF NOT EXISTS packing_plans (
    plan_id        TEXT PRIMARY KEY,
    normalization  TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    item_versions  TEXT NOT NULL,   -- JSON object unit_id -> version
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sealed_boxes (
    sealed_box_id TEXT PRIMARY KEY,
    plan_id       TEXT NOT NULL,
    box_type_id   TEXT NOT NULL,
    zone          TEXT NOT NULL,
    gross_weight  INTEGER NOT NULL,
    empty_weight  INTEGER NOT NULL,
    canonical_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sealed_box_items (
    sealed_box_id TEXT NOT NULL,
    unit_id       TEXT NOT NULL,
    x INTEGER NOT NULL, y INTEGER NOT NULL, rotated INTEGER NOT NULL,
    length INTEGER NOT NULL, width INTEGER NOT NULL,
    weight INTEGER NOT NULL, zone TEXT NOT NULL,
    PRIMARY KEY (sealed_box_id, unit_id)
);

CREATE TABLE IF NOT EXISTS shifts (
    shift_id         TEXT PRIMARY KEY,
    allowed_zones    TEXT NOT NULL,  -- JSON array
    max_gross_weight INTEGER NOT NULL,
    max_boxes        INTEGER NOT NULL,
    fixed_fee        INTEGER NOT NULL,
    stop_fee         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transport_plans (
    plan_id        TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    version        INTEGER NOT NULL,
    total_cost     INTEGER NOT NULL,
    vehicle_count  INTEGER NOT NULL,
    assignments    TEXT NOT NULL,    -- JSON snapshot
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transport_plan_boxes (
    plan_id       TEXT NOT NULL,
    sealed_box_id TEXT NOT NULL,
    store_id      TEXT NOT NULL,
    shift_id      TEXT NOT NULL,
    gross_weight  INTEGER NOT NULL,
    PRIMARY KEY (plan_id, sealed_box_id)
);

-- Active reservations. Rows exist only while a plan is CONFIRMED or
-- DEPARTED (departed reservations are kept forever as historical fact).
-- The primary keys make double-booking impossible even under races.
CREATE TABLE IF NOT EXISTS box_reservations (
    sealed_box_id TEXT PRIMARY KEY,
    plan_id       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shift_reservations (
    shift_id TEXT PRIMARY KEY,
    plan_id  TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS sealed_boxes_no_update
BEFORE UPDATE ON sealed_boxes
BEGIN SELECT RAISE(ABORT, 'sealed boxes are immutable'); END;

CREATE TRIGGER IF NOT EXISTS sealed_boxes_no_delete
BEFORE DELETE ON sealed_boxes
BEGIN SELECT RAISE(ABORT, 'sealed boxes are immutable'); END;

CREATE TRIGGER IF NOT EXISTS sealed_box_items_no_update
BEFORE UPDATE ON sealed_box_items
BEGIN SELECT RAISE(ABORT, 'sealed box contents are immutable'); END;

CREATE TRIGGER IF NOT EXISTS sealed_box_items_no_delete
BEFORE DELETE ON sealed_box_items
BEGIN SELECT RAISE(ABORT, 'sealed box contents are immutable'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    """Thin, explicit data-access layer. One DB file per instance."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        con = self._connect()
        try:
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 30000")
        return con

    @contextmanager
    def read(self):
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def write_tx(self):
        """Atomic write transaction; rolls back on any exception."""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Master data
    # ------------------------------------------------------------------

    def upsert_item(self, con, item: Item) -> None:
        con.execute(
            "INSERT INTO items_current(unit_id,length,width,weight,zone,version)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(unit_id) DO UPDATE SET"
            " length=excluded.length, width=excluded.width,"
            " weight=excluded.weight, zone=excluded.zone,"
            " version=excluded.version",
            (item.unit_id, item.length, item.width, item.weight,
             item.zone, item.version))
        con.execute(
            "INSERT OR REPLACE INTO item_history"
            "(unit_id,version,length,width,weight,zone,changed_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (item.unit_id, item.version, item.length, item.width,
             item.weight, item.zone, _now()))

    def get_item(self, con, unit_id: str) -> Item | None:
        row = con.execute(
            "SELECT * FROM items_current WHERE unit_id=?",
            (unit_id,)).fetchone()
        return _row_to_item(row) if row else None

    def all_items(self, con) -> list[Item]:
        return [_row_to_item(r) for r in con.execute(
            "SELECT * FROM items_current ORDER BY unit_id")]

    def upsert_box_type(self, con, box_type_id, inner_length, inner_width,
                        empty_weight, max_gross_weight, allowed_zones) -> None:
        con.execute(
            "INSERT INTO box_types VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(box_type_id) DO UPDATE SET"
            " inner_length=excluded.inner_length,"
            " inner_width=excluded.inner_width,"
            " empty_weight=excluded.empty_weight,"
            " max_gross_weight=excluded.max_gross_weight,"
            " allowed_zones=excluded.allowed_zones",
            (box_type_id, inner_length, inner_width, empty_weight,
             max_gross_weight, json.dumps(sorted(allowed_zones))))

    def all_box_types(self, con) -> list[sqlite3.Row]:
        return list(con.execute("SELECT * FROM box_types ORDER BY box_type_id"))

    def get_box_type(self, con, box_type_id: str) -> sqlite3.Row | None:
        return con.execute("SELECT * FROM box_types WHERE box_type_id=?",
                           (box_type_id,)).fetchone()

    def upsert_shift(self, con, shift: Shift) -> None:
        con.execute(
            "INSERT INTO shifts VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(shift_id) DO UPDATE SET"
            " allowed_zones=excluded.allowed_zones,"
            " max_gross_weight=excluded.max_gross_weight,"
            " max_boxes=excluded.max_boxes,"
            " fixed_fee=excluded.fixed_fee, stop_fee=excluded.stop_fee",
            (shift.shift_id, json.dumps(sorted(shift.allowed_zones)),
             shift.max_gross_weight, shift.max_boxes,
             shift.fixed_fee, shift.stop_fee))

    def all_shifts(self, con) -> list[Shift]:
        return [_row_to_shift(r) for r in
                con.execute("SELECT * FROM shifts ORDER BY shift_id")]

    def get_shift(self, con, shift_id: str) -> Shift | None:
        row = con.execute("SELECT * FROM shifts WHERE shift_id=?",
                          (shift_id,)).fetchone()
        return _row_to_shift(row) if row else None

    # ------------------------------------------------------------------
    # Packing plans / sealed boxes
    # ------------------------------------------------------------------

    def save_packing_plan(self, con, plan_id: str, normalization: str,
                          canonical_json: str,
                          item_versions: dict[str, int]) -> None:
        con.execute(
            "INSERT OR IGNORE INTO packing_plans VALUES(?,?,?,?,?)",
            (plan_id, normalization, canonical_json,
             json.dumps(item_versions, sort_keys=True), _now()))

    def insert_sealed_box(self, con, box: SealedBox, canonical_json: str,
                          item_versions: dict[str, int]) -> bool:
        """Insert one sealed box; returns False if it already existed."""
        cur = con.execute(
            "INSERT OR IGNORE INTO sealed_boxes VALUES(?,?,?,?,?,?,?,?)",
            (box.sealed_box_id, box.plan_id, box.box_type_id, box.zone,
             box.gross_weight, box.empty_weight, canonical_json,
             box.created_at))
        if cur.rowcount == 0:
            return False
        for it in box.items:
            con.execute(
                "INSERT INTO sealed_box_items VALUES(?,?,?,?,?,?,?,?,?)",
                (box.sealed_box_id, it.unit_id, it.x, it.y, it.rotated,
                 it.length, it.width, it.weight, it.zone))
        return True

    def get_sealed_box(self, con, sealed_box_id: str) -> SealedBox | None:
        row = con.execute("SELECT * FROM sealed_boxes WHERE sealed_box_id=?",
                          (sealed_box_id,)).fetchone()
        if row is None:
            return None
        items = [SealedItem(unit_id=r["unit_id"], x=r["x"], y=r["y"],
                            rotated=r["rotated"], length=r["length"],
                            width=r["width"], weight=r["weight"],
                            zone=r["zone"])
                 for r in con.execute(
                     "SELECT * FROM sealed_box_items WHERE sealed_box_id=?"
                     " ORDER BY unit_id", (sealed_box_id,))]
        return SealedBox(sealed_box_id=row["sealed_box_id"],
                         plan_id=row["plan_id"],
                         box_type_id=row["box_type_id"], zone=row["zone"],
                         gross_weight=row["gross_weight"],
                         empty_weight=row["empty_weight"],
                         items=tuple(items), created_at=row["created_at"])

    def all_sealed_boxes(self, con) -> list[SealedBox]:
        ids = [r["sealed_box_id"] for r in con.execute(
            "SELECT sealed_box_id FROM sealed_boxes ORDER BY sealed_box_id")]
        return [self.get_sealed_box(con, sid) for sid in ids]

    # ------------------------------------------------------------------
    # Transport plans
    # ------------------------------------------------------------------

    def insert_transport_plan(self, con, plan: TransportPlan) -> None:
        con.execute(
            "INSERT INTO transport_plans VALUES(?,?,?,?,?,?,?,?)",
            (plan.plan_id, plan.status.value, plan.version, plan.total_cost,
             plan.vehicle_count,
             json.dumps([a.__dict__ for a in plan.assignments],
                        sort_keys=True),
             _now(), _now()))
        for a in plan.assignments:
            con.execute(
                "INSERT INTO transport_plan_boxes VALUES(?,?,?,?,?)",
                (plan.plan_id, a.sealed_box_id, a.store_id, a.shift_id,
                 a.gross_weight))

    def get_transport_plan(self, con, plan_id: str) -> TransportPlan | None:
        row = con.execute("SELECT * FROM transport_plans WHERE plan_id=?",
                          (plan_id,)).fetchone()
        if row is None:
            return None
        assignments = [TransportAssignment(
            sealed_box_id=r["sealed_box_id"], store_id=r["store_id"],
            shift_id=r["shift_id"], gross_weight=r["gross_weight"])
            for r in con.execute(
                "SELECT * FROM transport_plan_boxes WHERE plan_id=?"
                " ORDER BY sealed_box_id", (plan_id,))]
        return TransportPlan(plan_id=row["plan_id"],
                             status=PlanStatus(row["status"]),
                             version=row["version"],
                             assignments=tuple(assignments),
                             total_cost=row["total_cost"],
                             vehicle_count=row["vehicle_count"])

    def all_transport_plans(self, con) -> list[TransportPlan]:
        ids = [r["plan_id"] for r in con.execute(
            "SELECT plan_id FROM transport_plans ORDER BY created_at, plan_id")]
        return [self.get_transport_plan(con, pid) for pid in ids]

    def update_plan_status(self, con, plan_id: str, status: PlanStatus,
                           expected_version: int) -> None:
        """Version-checked status transition (optimistic concurrency)."""
        cur = con.execute(
            "UPDATE transport_plans SET status=?, version=version+1,"
            " updated_at=? WHERE plan_id=? AND version=?",
            (status.value, _now(), plan_id, expected_version))
        if cur.rowcount == 0:
            row = con.execute("SELECT version FROM transport_plans"
                              " WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"transport plan {plan_id} not found")
            raise VersionConflictError(
                f"plan {plan_id}: expected version {expected_version},"
                f" current {row['version']}")

    # ------------------------------------------------------------------
    # Reservations
    # ------------------------------------------------------------------

    def conflicting_reservations(self, con, sealed_box_ids, shift_ids,
                                 exclude_plan_id=None):
        """Active reservations overlapping the given boxes/shifts."""
        box_hits, shift_hits = set(), set()
        if sealed_box_ids:
            q = ("SELECT sealed_box_id, plan_id FROM box_reservations"
                 f" WHERE sealed_box_id IN ({_ph(sealed_box_ids)})")
            for r in con.execute(q, tuple(sealed_box_ids)):
                if r["plan_id"] != exclude_plan_id:
                    box_hits.add(r["sealed_box_id"])
        if shift_ids:
            q = ("SELECT shift_id, plan_id FROM shift_reservations"
                 f" WHERE shift_id IN ({_ph(shift_ids)})")
            for r in con.execute(q, tuple(shift_ids)):
                if r["plan_id"] != exclude_plan_id:
                    shift_hits.add(r["shift_id"])
        return sorted(box_hits), sorted(shift_hits)

    def insert_reservations(self, con, plan_id: str, sealed_box_ids,
                            shift_ids) -> None:
        try:
            for sid in sealed_box_ids:
                con.execute("INSERT INTO box_reservations VALUES(?,?)",
                            (sid, plan_id))
            for sh in shift_ids:
                con.execute("INSERT INTO shift_reservations VALUES(?,?)",
                            (sh, plan_id))
        except sqlite3.IntegrityError as e:
            raise ReservationConflictError(
                f"reservation race lost for plan {plan_id}: {e}") from e

    def release_reservations(self, con, plan_id: str) -> None:
        con.execute("DELETE FROM box_reservations WHERE plan_id=?",
                    (plan_id,))
        con.execute("DELETE FROM shift_reservations WHERE plan_id=?",
                    (plan_id,))

    # ------------------------------------------------------------------
    # Traceability
    # ------------------------------------------------------------------

    def trace_unit(self, con, unit_id: str) -> list[dict]:
        """Every sealed box containing the unit, with transport history."""
        out = []
        boxes = con.execute(
            "SELECT * FROM sealed_box_items WHERE unit_id=?"
            " ORDER BY sealed_box_id", (unit_id,)).fetchall()
        for b in boxes:
            box = self.get_sealed_box(con, b["sealed_box_id"])
            legs = con.execute(
                "SELECT tpb.plan_id, tpb.store_id, tpb.shift_id,"
                " tp.status, tp.version, tp.total_cost"
                " FROM transport_plan_boxes tpb"
                " JOIN transport_plans tp ON tp.plan_id = tpb.plan_id"
                " WHERE tpb.sealed_box_id=?"
                " ORDER BY tp.created_at, tpb.plan_id",
                (b["sealed_box_id"],)).fetchall()
            out.append({
                "unit_id": unit_id,
                "sealed_box_id": b["sealed_box_id"],
                "box_type_id": box.box_type_id,
                "zone": box.zone,
                "gross_weight": box.gross_weight,
                "placement": {"x": b["x"], "y": b["y"],
                              "rotated": b["rotated"]},
                "item_snapshot": {"length": b["length"], "width": b["width"],
                                  "weight": b["weight"]},
                "transport": [dict(plan_id=l["plan_id"],
                                   store_id=l["store_id"],
                                   shift_id=l["shift_id"],
                                   status=l["status"],
                                   total_cost=l["total_cost"])
                              for l in legs],
            })
        return out


def _ph(seq) -> str:
    return ",".join("?" for _ in seq)


def _row_to_item(row) -> Item:
    return Item(unit_id=row["unit_id"], length=row["length"],
                width=row["width"], weight=row["weight"],
                zone=row["zone"], version=row["version"])


def _row_to_shift(row) -> Shift:
    return Shift(shift_id=row["shift_id"],
                 allowed_zones=frozenset(json.loads(row["allowed_zones"])),
                 max_gross_weight=row["max_gross_weight"],
                 max_boxes=row["max_boxes"],
                 fixed_fee=row["fixed_fee"], stop_fee=row["stop_fee"])


def new_plan_id() -> str:
    return "TP-" + uuid.uuid4().hex[:16]
