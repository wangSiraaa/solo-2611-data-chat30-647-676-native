"""Calibration service: reference snapshots, immutable published products.

Persistence is SQLite.  Key invariants:

* A *reference identity* is ``reference_id``.  Re-measuring the same
  ``reference_id`` creates a new *measurement* with its own ``measurement_id``;
  it is not a re-send/overwrite.
* Publishing snapshots the reference versions and the channel-angle version in
  use.  **Any change that affects the fit soil (adding/removing/correcting a
  reference, retracting a measurement, re-registering channels) yields a new
  product with the next product version; old products are never mutated.**
* Products are self-contained parameter payloads: they contain fitted gains,
  offsets, residuals and reasons, but never reference readings.  A warehouse
  can therefore consume a product without reading this store.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import model

PRODUCT_IMMUTABLE_COLUMNS = (
    "product_id", "product_version", "created_at", "kind", "source_product_id",
    "design_id", "design_fingerprint", "payload_json", "payload_sha256",
    "model_version", "config_json", "status_summary_json", "channel_status_json",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_designs (
    design_id      TEXT PRIMARY KEY,
    parent_design_id TEXT,
    created_at     TEXT NOT NULL,
    channels_json  TEXT NOT NULL,
    channels_sha   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refs (
    reference_id       TEXT PRIMARY KEY,
    I                  REAL NOT NULL,
    Q                  REAL NOT NULL,
    U                  REAL NOT NULL,
    reference_version  INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measurements (
    measurement_id  TEXT PRIMARY KEY,
    reference_id    TEXT NOT NULL REFERENCES refs(reference_id),
    I               REAL NOT NULL,
    Q               REAL NOT NULL,
    U               REAL NOT NULL,
    y_json          TEXT NOT NULL,
    reference_version INTEGER NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    retracted_at    TEXT,
    retraction_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_measurements_ref ON measurements(reference_id);
CREATE INDEX IF NOT EXISTS idx_measurements_status ON measurements(status);
CREATE TABLE IF NOT EXISTS product_cursors (
    kind             TEXT PRIMARY KEY,
    last_version     INTEGER NOT NULL,
    last_product_id  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    product_id              TEXT PRIMARY KEY,
    product_version         INTEGER NOT NULL,
    kind                    TEXT NOT NULL,
    source_product_id       TEXT,
    design_id               TEXT NOT NULL,
    design_fingerprint      TEXT NOT NULL,
    payload_json            TEXT NOT NULL,
    payload_sha256          TEXT NOT NULL,
    content_sha256          TEXT NOT NULL,
    model_version           TEXT NOT NULL,
    config_json             TEXT NOT NULL,
    status_summary_json     TEXT NOT NULL,
    channel_status_json     TEXT NOT NULL,
    created_at              TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_products_kind_version
    ON products(kind, product_version);
CREATE TABLE IF NOT EXISTS product_fit_points (
    product_id        TEXT NOT NULL,
    channel           TEXT NOT NULL,
    position          INTEGER NOT NULL,
    measurement_id    TEXT,
    reference_id      TEXT,
    reference_version INTEGER,
    s                 REAL,
    y                 REAL,
    residual          REAL,
    PRIMARY KEY (product_id, channel, position)
);
CREATE TRIGGER IF NOT EXISTS fit_points_no_update
BEFORE UPDATE ON product_fit_points
BEGIN
    SELECT RAISE(ABORT, 'product fit details are immutable');
END;
CREATE TRIGGER IF NOT EXISTS fit_points_no_delete
BEFORE DELETE ON product_fit_points
BEGIN
    SELECT RAISE(ABORT, 'product fit details are immutable');
END;
CREATE TABLE IF NOT EXISTS events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT,
    action     TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS products_no_update
BEFORE UPDATE ON products
BEGIN
    SELECT RAISE(ABORT, 'published products are immutable');
END;
CREATE TRIGGER IF NOT EXISTS products_no_delete
BEFORE DELETE ON products
BEGIN
    SELECT RAISE(ABORT, 'published products are immutable');
END;
CREATE TRIGGER IF NOT EXISTS design_no_update
BEFORE UPDATE ON calibration_designs
BEGIN
    SELECT RAISE(ABORT, 'calibration designs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS design_no_delete
BEFORE DELETE ON calibration_designs
BEGIN
    SELECT RAISE(ABORT, 'calibration designs are immutable');
END;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class CalibrationStore:
    """Independent calibration service (no observation data lives here)."""

    def __init__(self, db_path: str | Path, config: Mapping[str, Any] | None = None):
        self.db_path = str(db_path)
        self.config = model.merge_config(config)

    # -- plumbing ---------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _event(self, conn: sqlite3.Connection, entity: str, entity_id: str | None,
               action: str, detail: Mapping[str, Any] | None = None) -> None:
        conn.execute(
            "INSERT INTO events(at, entity, entity_id, action, detail_json) "
            "VALUES(?,?,?,?,?)",
            (utcnow(), entity, entity_id, action, model.json_dumps(dict(detail or {}))),
        )

    # -- channel design ---------------------------------------------------

    def register_channels(
        self, channels: Mapping[str, float], parent_design_id: str | None = None
    ) -> str:
        """Register the fixed per-channel angles as an immutable design version."""
        clean = {str(k): model.canonical_theta(v) for k, v in channels.items()}
        digest = model.sha256_of(clean)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT design_id FROM calibration_designs WHERE channels_sha = ? "
                "ORDER BY created_at DESC LIMIT 1", (digest,)
            ).fetchone()
            if row is not None and parent_design_id is None:
                return row["design_id"]
            design_id = _new_id("design")
            conn.execute(
                "INSERT INTO calibration_designs(design_id, parent_design_id, created_at,"
                " channels_json, channels_sha) VALUES(?,?,?,?,?)",
                (design_id, parent_design_id, utcnow(), model.json_dumps(clean), digest),
            )
            self._event(conn, "design", design_id, "register",
                        {"n_channels": len(clean), "channels_sha": digest,
                         "parent_design_id": parent_design_id})
        return design_id

    def get_design(self, design_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM calibration_designs WHERE design_id = ?", (design_id,)
            ).fetchone()
        if row is None:
            raise model.ValidationError(f"unknown design_id {design_id!r}")
        return {"design_id": row["design_id"],
                "parent_design_id": row["parent_design_id"],
                "created_at": row["created_at"],
                "channels": json.loads(row["channels_json"]),
                "channels_sha": row["channels_sha"]}

    def current_design_id(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT design_id FROM calibration_designs ORDER BY created_at DESC, "
                "rowid DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise model.ValidationError("no calibration design registered yet")
        return row["design_id"]

    # -- references & measurements ---------------------------------------

    def register_reference(
        self, reference_id: str, I: float, Q: float, U: float,
        correction_of: str | None = None,
    ) -> dict[str, Any]:
        """Create a reference, or correct an existing one (version bump)."""
        if not isinstance(reference_id, str) or not reference_id:
            raise model.ValidationError("reference_id must be a non-empty string")
        I = model.require_finite(I, "I")
        Q = model.require_finite(Q, "Q")
        U = model.require_finite(U, "U")
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM refs WHERE reference_id = ?", (reference_id,)
            ).fetchone()
            now = utcnow()
            if existing is None:
                if correction_of is not None:
                    raise model.ValidationError(
                        f"cannot correct unknown reference {reference_id!r}")
                version = 1
                conn.execute(
                    "INSERT INTO refs(reference_id, I, Q, U, reference_version,"
                    " created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                    (reference_id, I, Q, U, version, now, now),
                )
                action = "create"
            else:
                version = int(existing["reference_version"]) + 1
                conn.execute(
                    "UPDATE refs SET I=?, Q=?, U=?, reference_version=?, "
                    "updated_at=? WHERE reference_id=?",
                    (I, Q, U, version, now, reference_id),
                )
                action = "correct"
            self._event(conn, "reference", reference_id, action,
                        {"I": I, "Q": Q, "U": U, "reference_version": version})
        return {"reference_id": reference_id, "I": I, "Q": Q, "U": U,
                "reference_version": version}

    def record_measurement(
        self, reference_id: str, I: float, Q: float, U: float,
        y_by_channel: Mapping[str, float], *, correct: bool = False,
        source: str = "api",
    ) -> dict[str, Any]:
        """Register/correct a reference and record one measurement of it.

        * If the reference does not exist it is created with this version.
        * If it exists and ``correct`` is false, the measurement snapshots the
          current reference values.
        * If ``correct`` is true, the reference is version-bumped to the new
          I/Q/U first and the measurement snapshots the new version.

        In every case a fresh ``measurement_id`` is created: the same
        reference can be measured many times without being a re-send.
        """
        if correct or reference_id not in {r["reference_id"]
                                           for r in self.list_references()}:
            self.register_reference(reference_id, I, Q, U,
                                    correction_of=reference_id if correct else None)
        return self.ingest_measurement(reference_id, y_by_channel, source=source)

    def ingest_measurement(
        self, reference_id: str, y_by_channel: Mapping[str, float],
        I: float | None = None, Q: float | None = None, U: float | None = None,
        source: str = "api",
    ) -> dict[str, Any]:
        """Record one independent measurement of a reference across channels.

        Same ``reference_id`` measured again => a new measurement id.  I/Q/U are
        snapshotted from the reference version that was in force; optionally the
        measured I/Q/U may differ from the reference (a mis-calibrated
        reference), which is recorded faithfully.
        """
        with self._tx() as conn:
            ref = conn.execute(
                "SELECT * FROM refs WHERE reference_id = ?", (reference_id,)
            ).fetchone()
            if ref is None:
                raise model.ValidationError(f"unknown reference_id {reference_id!r}")
            ys: dict[str, float] = {}
            for ch, v in y_by_channel.items():
                ys[str(ch)] = model.require_finite(v, f"y_by_channel[{ch!r}]")
            i_val = ref["I"] if I is None else model.require_finite(I, "I")
            q_val = ref["Q"] if Q is None else model.require_finite(Q, "Q")
            u_val = ref["U"] if U is None else model.require_finite(U, "U")
            mid = _new_id("meas")
            conn.execute(
                "INSERT INTO measurements(measurement_id, reference_id, I, Q, U, y_json,"
                " reference_version, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (mid, reference_id, i_val, q_val, u_val, model.json_dumps(ys),
                 int(ref["reference_version"]), "active", utcnow()),
            )
            self._event(conn, "measurement", mid, "ingest",
                        {"reference_id": reference_id, "source": source,
                         "reference_version": int(ref["reference_version"]),
                         "n_channels": len(ys)})
        return {"measurement_id": mid, "reference_id": reference_id,
                "I": i_val, "Q": q_val, "U": u_val,
                "reference_version": int(ref["reference_version"]),
                "y_by_channel": ys, "status": "active"}

    def retract_measurement(self, measurement_id: str, reason: str) -> dict[str, Any]:
        """Truthfully retract one measurement: it is excluded from future fits."""
        if not isinstance(reason, str) or not reason:
            raise model.ValidationError("retraction requires a reason")
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM measurements WHERE measurement_id = ?", (measurement_id,)
            ).fetchone()
            if row is None:
                raise model.ValidationError(f"unknown measurement_id {measurement_id!r}")
            if row["status"] == "retracted":
                raise model.ValidationError(
                    f"measurement {measurement_id!r} is already retracted")
            conn.execute(
                "UPDATE measurements SET status='retracted', retracted_at=?, "
                "retraction_reason=? WHERE measurement_id=?",
                (utcnow(), reason, measurement_id),
            )
            self._event(conn, "measurement", measurement_id, "retract",
                        {"reference_id": row["reference_id"], "reason": reason})
        return {"measurement_id": measurement_id, "status": "retracted",
                "reason": reason}

    def retract_reference(self, reference_id: str, reason: str) -> dict[str, Any]:
        """Retract a whole reference identity (all of its active measurements).

        The measurement rows are retained with status 'retracted' so history is
        truthful; future products exclude them.  Already-published products are
        immutable and keep the data they were fitted with.
        """
        if not isinstance(reason, str) or not reason:
            raise model.ValidationError("retraction requires a reason")
        with self._tx() as conn:
            ref = conn.execute(
                "SELECT * FROM refs WHERE reference_id = ?", (reference_id,)
            ).fetchone()
            if ref is None:
                raise model.ValidationError(f"unknown reference_id {reference_id!r}")
            rows = conn.execute(
                "SELECT measurement_id FROM measurements WHERE reference_id = ? "
                "AND status = 'active'", (reference_id,)).fetchall()
            ids = [r["measurement_id"] for r in rows]
            now = utcnow()
            for mid in ids:
                conn.execute(
                    "UPDATE measurements SET status='retracted', retracted_at=?, "
                    "retraction_reason=? WHERE measurement_id=?",
                    (now, reason, mid))
            self._event(conn, "reference", reference_id, "retract",
                        {"reason": reason, "measurement_ids": ids})
        return {"reference_id": reference_id, "retracted_measurement_ids": ids,
                "status": "retracted", "reason": reason}

    def list_measurements(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM measurements"
        if active_only:
            sql += " WHERE status = 'active'"
        sql += " ORDER BY created_at, rowid"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        out = []
        for r in rows:
            out.append({
                "measurement_id": r["measurement_id"],
                "reference_id": r["reference_id"],
                "I": r["I"], "Q": r["Q"], "U": r["U"],
                "reference_version": r["reference_version"],
                "y_by_channel": json.loads(r["y_json"]),
                "status": r["status"],
                "created_at": r["created_at"],
                "retracted_at": r["retracted_at"],
                "retraction_reason": r["retraction_reason"],
            })
        return out

    def list_references(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM refs ORDER BY reference_id"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- publishing -------------------------------------------------------

    def _next_version(self, conn: sqlite3.Connection, kind: str) -> int:
        row = conn.execute(
            "SELECT last_version FROM product_cursors WHERE kind = ?", (kind,)
        ).fetchone()
        nxt = 1 if row is None else int(row["last_version"]) + 1
        return nxt

    def _commit_product(
        self, conn: sqlite3.Connection, *, kind: str, payload: dict[str, Any],
        version: int, product_id: str, design_id: str, fingerprint: str,
        created_at: str,
    ) -> None:
        status_summary = payload["status_summary"]
        channel_status = {
            k: {"status": v["status"], "reason": v["reason"]}
            for k, v in payload["channels"].items()
        }
        conn.execute(
            "INSERT INTO products(product_id, product_version, kind, source_product_id,"
            " design_id, design_fingerprint, payload_json, payload_sha256,"
            " content_sha256, model_version,"
            " config_json, status_summary_json, channel_status_json, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (product_id, version, kind, payload.get("source_product_id"), design_id,
             fingerprint, model.json_dumps(payload), model.sha256_of(payload),
             model.sha256_of(payload),
             payload["model_version"], model.json_dumps(payload["config"]),
             model.json_dumps(status_summary), model.json_dumps(channel_status),
             created_at),
        )
        conn.execute(
            "INSERT INTO product_cursors(kind, last_version, last_product_id)"
            " VALUES(?,?,?) ON CONFLICT(kind) DO UPDATE SET"
            " last_version=excluded.last_version, last_product_id=excluded.last_product_id",
            (kind, version, product_id),
        )

    def publish(self, design_id: str | None = None) -> dict[str, Any]:
        """Fit every channel from the current reference snapshot and publish.

        Each publish produces a *new* product (next version for the 'internal'
        kind).  The payload is derived only from the snapshot taken here.
        """
        design_id = design_id or self.current_design_id()
        design = self.get_design(design_id)
        with self._tx() as conn:
            meas_rows = conn.execute(
                "SELECT * FROM measurements WHERE status='active' "
                "ORDER BY created_at, rowid"
            ).fetchall()
            snapshot = []
            for r in meas_rows:
                snapshot.append({
                    "measurement_id": r["measurement_id"],
                    "reference_id": r["reference_id"],
                    "reference_version": r["reference_version"],
                    "I": r["I"], "Q": r["Q"], "U": r["U"],
                    "y_by_channel": json.loads(r["y_json"]),
                })
            fingerprint = model.sha256_of({
                "channels_sha": design["channels_sha"],
                "measurements": snapshot,
                "config": self.config,
            })
            fits = model.fit_product_channels(snapshot, design["channels"], self.config)
            version = self._next_version(conn, "internal")
            product_id = _new_id("prod")
            now = utcnow()
            payload = model.build_payload(
                product_id=product_id, created_at=now, product_version=version,
                kind="internal", source_product_id=None, design_id=design_id,
                design_fingerprint=fingerprint, config=self.config, channels=fits,
            )
            self._commit_product(
                conn, kind="internal", payload=payload, version=version,
                product_id=product_id, design_id=design_id, fingerprint=fingerprint,
                created_at=now,
            )
            # Retain every fitted/rejected point so the product's provenance is
            # fully auditable (s, y, residual, reference version).
            for ch, fit in fits.items():
                for pos, pt in enumerate(fit.get("points", [])):
                    conn.execute(
                        "INSERT INTO product_fit_points(product_id, channel, position,"
                        " measurement_id, reference_id, reference_version, s, y, residual)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (product_id, ch, pos, pt.get("measurement_id"),
                         pt.get("reference_id"), pt.get("reference_version"),
                         pt["s"], pt["y"], pt["residual"]),
                    )
            self._event(conn, "product", product_id, "publish",
                        {"version": version, "n_measurements": len(snapshot),
                         "status_summary": payload["status_summary"],
                         "design_id": design_id})
        return self.get_product(product_id)

    def import_product(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Persist an external same-format product under its own 'external' kind.

        External products never mix with internally published versions; the
        warehouse consumes the content of the product, not this store.
        """
        normalized = model.validate_payload(payload)
        # Content identity: the external product's substantive content, excluding
        # fields this store rewrites locally (product_id/version/created_at/kind).
        content = dict(normalized)
        for key in ("product_id", "product_version", "created_at", "kind",
                    "source_product_id", "status_summary"):
            content.pop(key, None)
        content_digest = model.sha256_of(content)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT product_id, product_version FROM products "
                "WHERE kind='external' AND content_sha256=?", (content_digest,)
            ).fetchone()
            if existing is not None:
                return {"product_id": existing["product_id"],
                        "product_version": existing["product_version"],
                        "deduplicated": True, "kind": "external"}
            version = self._next_version(conn, "external")
            product_id = _new_id("ext")
            now = utcnow()
            normalized = dict(normalized)
            normalized["product_id"] = product_id
            normalized["product_version"] = version
            normalized["created_at"] = now
            normalized["kind"] = "external"
            normalized["source_product_id"] = payload.get("product_id")
            status_summary = {
                "calibrated": sum(1 for c in normalized["channels"].values()
                                  if c["status"] == "calibrated"),
                "uncalibratable": sum(1 for c in normalized["channels"].values()
                                      if c["status"] != "calibrated"),
            }
            normalized["status_summary"] = status_summary
            channel_status = {
                k: {"status": v["status"], "reason": v["reason"]}
                for k, v in normalized["channels"].items()
            }
            stored_text = model.json_dumps(normalized)
            conn.execute(
                "INSERT INTO products(product_id, product_version, kind, source_product_id,"
                " design_id, design_fingerprint, payload_json, payload_sha256,"
                " content_sha256, model_version,"
                " config_json, status_summary_json, channel_status_json, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (product_id, version, "external", payload.get("product_id"),
                 normalized.get("design_id"), normalized.get("design_fingerprint"),
                 stored_text, model.sha256_of(normalized), content_digest,
                 normalized["model_version"],
                 model.json_dumps(normalized["config"]), model.json_dumps(status_summary),
                 model.json_dumps(channel_status), now),
            )
            conn.execute(
                "INSERT INTO product_cursors(kind, last_version, last_product_id)"
                " VALUES('external',?,?) ON CONFLICT(kind) DO UPDATE SET"
                " last_version=excluded.last_version, last_product_id=excluded.last_product_id",
                (version, product_id),
            )
            self._event(conn, "product", product_id, "import",
                        {"version": version, "source_product_id": payload.get("product_id"),
                         "status_summary": status_summary})
        return self.get_product(product_id, kind="external")

    # -- reads ------------------------------------------------------------

    def get_product(self, product_id: str, *, kind: str | None = None
                    ) -> dict[str, Any]:
        with self._connect() as conn:
            if kind is None:
                row = conn.execute(
                    "SELECT * FROM products WHERE product_id = ?",
                    (product_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM products WHERE product_id = ? AND kind = ?",
                    (product_id, kind)).fetchone()
        if row is None:
            raise model.ValidationError(f"unknown product_id {product_id!r}")
        return self._product_row(row)

    def latest_product(self, kind: str = "internal") -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM products WHERE kind = ? ORDER BY product_version DESC "
                "LIMIT 1", (kind,)).fetchone()
        return None if row is None else self._product_row(row)

    def _product_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "product_id": row["product_id"],
            "product_version": row["product_version"],
            "kind": row["kind"],
            "source_product_id": row["source_product_id"],
            "design_id": row["design_id"],
            "design_fingerprint": row["design_fingerprint"],
            "payload_sha256": row["payload_sha256"],
            "created_at": row["created_at"],
            "status_summary": json.loads(row["status_summary_json"]),
            "channel_status": json.loads(row["channel_status_json"]),
            "payload": json.loads(row["payload_json"]),
        }

    def product_payload(self, product_id: str) -> dict[str, Any]:
        return self.get_product(product_id)["payload"]

    def list_products(self, kind: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT product_id, product_version, kind, source_product_id, design_id,"
               " payload_sha256, created_at, status_summary_json FROM products")
        args: tuple[Any, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            args = (kind,)
        sql += " ORDER BY kind, product_version"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [{
            "product_id": r["product_id"], "product_version": r["product_version"],
            "kind": r["kind"], "source_product_id": r["source_product_id"],
            "design_id": r["design_id"], "payload_sha256": r["payload_sha256"],
            "created_at": r["created_at"],
            "status_summary": json.loads(r["status_summary_json"]),
        } for r in rows]

    # -- traceability -----------------------------------------------------

    def trace_reference(self, reference_id: str) -> dict[str, Any]:
        """Everything the calibration service knows about a reference identity."""
        with self._connect() as conn:
            ref = conn.execute("SELECT * FROM refs WHERE reference_id = ?",
                               (reference_id,)).fetchone()
            if ref is None:
                raise model.ValidationError(f"unknown reference_id {reference_id!r}")
            meass = conn.execute(
                "SELECT * FROM measurements WHERE reference_id = ? "
                "ORDER BY created_at, rowid", (reference_id,)).fetchall()
            events = conn.execute(
                "SELECT * FROM events WHERE (entity='reference' AND entity_id=?) OR "
                "(entity='measurement' AND entity_id IN (SELECT measurement_id FROM "
                "measurements WHERE reference_id=?)) ORDER BY event_id",
                (reference_id, reference_id)).fetchall()
            products = conn.execute(
                "SELECT product_id, kind, product_version, created_at, "
                "status_summary_json FROM products ORDER BY kind, product_version"
            ).fetchall()
            meas_ids = [m["measurement_id"] for m in meass]
            if meas_ids:
                placeholders = ",".join("?" for _ in meas_ids)
                included = conn.execute(
                    "SELECT DISTINCT product_id, reference_version "
                    "FROM product_fit_points WHERE measurement_id IN "
                    f"({placeholders})", tuple(meas_ids)).fetchall()
            else:
                included = []
        meas_list = [{
            "measurement_id": m["measurement_id"], "I": m["I"], "Q": m["Q"],
            "U": m["U"], "reference_version": m["reference_version"],
            "y_by_channel": json.loads(m["y_json"]), "status": m["status"],
            "created_at": m["created_at"], "retracted_at": m["retracted_at"],
            "retraction_reason": m["retraction_reason"],
        } for m in meass]
        included_by_product: dict[str, set[int]] = {}
        for r in included:
            included_by_product.setdefault(r["product_id"], set()).add(
                r["reference_version"])
        referencing_products = []
        for p in products:
            pid = p["product_id"]
            referencing_products.append({
                "product_id": pid, "kind": p["kind"],
                "product_version": p["product_version"],
                "created_at": p["created_at"],
                "status_summary": json.loads(p["status_summary_json"]),
                "included_reference_versions": sorted(
                    included_by_product.get(pid, set())),
                "included": pid in included_by_product,
            })
        return {
            "reference": dict(ref),
            "measurements": meas_list,
            "product_versions": referencing_products,
            "events": [_event_row(e) for e in events],
            "note": "product_versions[*].included / included_reference_versions "
                    "are derived from retained per-fit-point provenance "
                    "(product_fit_points); external products have no local "
                    "provenance, so they report included=False here.",
        }

    def trace_product(self, product_id: str) -> dict[str, Any]:
        product = self.get_product(product_id)
        design_id = product["design_id"]
        design = None
        if design_id:
            try:
                design = self.get_design(design_id)
            except model.ValidationError:
                design = None
        with self._connect() as conn:
            ptrows = conn.execute(
                "SELECT * FROM product_fit_points WHERE product_id = ? "
                "ORDER BY channel, position", (product_id,)).fetchall()
        fit_points: dict[str, list[dict[str, Any]]] = {}
        for r in ptrows:
            fit_points.setdefault(r["channel"], []).append({
                "position": r["position"],
                "measurement_id": r["measurement_id"],
                "reference_id": r["reference_id"],
                "reference_version": r["reference_version"],
                "s": r["s"], "y": r["y"], "residual": r["residual"],
            })
        return {
            "product": {
                "product_id": product["product_id"],
                "product_version": product["product_version"],
                "kind": product["kind"],
                "created_at": product["created_at"],
                "source_product_id": product["source_product_id"],
                "design_fingerprint": product["design_fingerprint"],
                "payload_sha256": product["payload_sha256"],
                "status_summary": product["status_summary"],
                "channel_status": product["channel_status"],
                "channels": product["payload"]["channels"],
            },
            "design": design,
            "fit_points": fit_points,
            "note": "fit_points are the per-reference s/y/residual used (or attempted) "
                    "when fitting each channel; external products have no local "
                    "fit_points because this store did not produce them.",
        }

    def events(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY event_id").fetchall()
        return [_event_row(r) for r in rows]


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"], "at": row["at"], "entity": row["entity"],
        "entity_id": row["entity_id"], "action": row["action"],
        "detail": json.loads(row["detail_json"]),
    }
