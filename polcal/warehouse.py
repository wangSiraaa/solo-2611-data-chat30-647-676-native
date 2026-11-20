"""Observation warehouse: raw exposures, pinned products, inversion runs.

Key invariants:

* An exposure has a unique ``exposure_id`` and at most one reading per channel.
  Readings carry a positive weight.  Missing channels are simply absent.
* A run *really reads a complete product payload* pinned by id.  It never reads
  the calibration references.  The warehouse therefore works with imported
  external products identically to internally published ones.
* Channel eligibility = calibrated in the pinned product AND present in the
  exposure AND angle participates in a full-rank design.  Ineligible readings
  are routed to explicit locations (``qc``); they are never zero-filled.
* Solutions keep per-channel corrected values, predicted raw readings and
  residuals, plus the unconstrained solution.  Model-inconsistent solutions are
  flagged and keep the unconstrained values -- no clamping onto the feasible
  region is reported as "consistent".
* Runs are immutable and re-openable; a re-run (e.g. after a reference
  correction) creates a new run that redoes eligibility, correction and
  inversion against the new product, while the old run stays bound to the old
  product.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import numpy as np

from . import model

RUN_MODEL = model.MODEL_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS exposures (
    exposure_id  TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    exposure_id      TEXT NOT NULL REFERENCES exposures(exposure_id),
    channel          TEXT NOT NULL,
    y_raw            REAL NOT NULL,
    weight           REAL NOT NULL,
    added_at         TEXT NOT NULL,
    PRIMARY KEY (exposure_id, channel),
    CHECK (weight > 0)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    exposure_id        TEXT NOT NULL REFERENCES exposures(exposure_id),
    product_id         TEXT NOT NULL,
    product_version    INTEGER NOT NULL,
    product_sha256     TEXT NOT NULL,
    config_json        TEXT NOT NULL,
    method             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    solution_id        TEXT NOT NULL UNIQUE,
    solution_json      TEXT NOT NULL,
    solution_sha256    TEXT NOT NULL,
    reof               TEXT,
    reof_of            TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_exposure ON runs(exposure_id);
CREATE INDEX IF NOT EXISTS idx_runs_product ON runs(product_id);
CREATE TRIGGER IF NOT EXISTS runs_no_update
BEFORE UPDATE ON runs
BEGIN
    SELECT RAISE(ABORT, 'inversion runs are immutable; create a new run');
END;
CREATE TRIGGER IF NOT EXISTS runs_no_delete
BEFORE DELETE ON runs
BEGIN
    SELECT RAISE(ABORT, 'inversion runs are immutable');
END;
CREATE TABLE IF NOT EXISTS warehouse_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT,
    action     TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class ObservationWarehouse:
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
            "INSERT INTO warehouse_events(at, entity, entity_id, action, detail_json)"
            " VALUES(?,?,?,?,?)",
            (utcnow(), entity, entity_id, action, model.json_dumps(dict(detail or {}))),
        )

    # -- exposures & readings --------------------------------------------

    def add_exposure(
        self, exposure_id: str, readings: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Add an exposure with at most one reading per channel.

        Each reading: ``{channel, y_raw, weight}``; weight must be finite and > 0.
        """
        if not isinstance(exposure_id, str) or not exposure_id:
            raise model.ValidationError("exposure_id must be a non-empty string")
        clean: dict[str, tuple[float, float]] = {}
        for r in readings:
            ch = r.get("channel")
            if not isinstance(ch, str) or not ch:
                raise model.ValidationError("reading channel must be a non-empty string")
            if ch in clean:
                raise model.ValidationError(
                    f"exposure {exposure_id!r}: more than one reading for channel {ch!r}")
            y = model.require_finite(r.get("y_raw"), f"reading[{ch}].y_raw")
            w = model.require_finite(r.get("weight"), f"reading[{ch}].weight")
            if not w > 0.0:
                raise model.ValidationError(
                    f"exposure {exposure_id!r}: channel {ch!r} weight must be > 0")
            clean[ch] = (y, w)

        with self._tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM exposures WHERE exposure_id = ?", (exposure_id,)
            ).fetchone()
            if exists is not None:
                raise model.ValidationError(f"exposure {exposure_id!r} already exists")
            now = utcnow()
            conn.execute("INSERT INTO exposures(exposure_id, created_at) VALUES(?,?)",
                         (exposure_id, now))
            for ch in sorted(clean):
                y, w = clean[ch]
                conn.execute(
                    "INSERT INTO readings(exposure_id, channel, y_raw, weight, added_at)"
                    " VALUES(?,?,?,?,?)", (exposure_id, ch, y, w, now))
            self._event(conn, "exposure", exposure_id, "add",
                        {"channels": sorted(clean)})
        return self.get_exposure(exposure_id)

    def get_exposure(self, exposure_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM exposures WHERE exposure_id = ?",
                               (exposure_id,)).fetchone()
            if row is None:
                raise model.ValidationError(f"unknown exposure_id {exposure_id!r}")
            rrows = conn.execute(
                "SELECT * FROM readings WHERE exposure_id = ? ORDER BY channel",
                (exposure_id,)).fetchall()
        return {
            "exposure_id": row["exposure_id"],
            "created_at": row["created_at"],
            "readings": [{
                "channel": r["channel"], "y_raw": r["y_raw"],
                "weight": r["weight"], "added_at": r["added_at"],
            } for r in rrows],
        }

    def list_exposures(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT exposure_id FROM exposures ORDER BY exposure_id").fetchall()
        return [r["exposure_id"] for r in rows]

    # -- inversion --------------------------------------------------------

    def invert(
        self, exposure_id: str, product_payload: Mapping[str, Any],
        *, reof_of: str | None = None,
    ) -> dict[str, Any]:
        """Pin a product payload, invert one exposure, persist the run.

        ``reof_of`` links this run to a previous run it re-computes.
        """
        product = model.validate_payload(product_payload)
        if not isinstance(product.get("product_id"), str) or not product["product_id"]:
            raise model.ValidationError(
                "the pinned product payload must carry a non-empty product_id")
        exposure = self.get_exposure(exposure_id)
        cfg = self.config
        readings = {r["channel"]: r for r in exposure["readings"]}
        product_channels = product["channels"]

        qc: dict[str, Any] = {
            "uncalibratable": {},     # in product, not calibratable
            "not_in_product": [],     # reading channel absent from the product
            "missing": [],            # calibrated in product, no reading
            "insufficient_design": {
                "reason": None, "rank": 0, "n_candidate": 0,
                "singular_values": [], "rank_tol": cfg["rank_tol"],
                "used_channels": [],
            },
        }

        candidates: list[dict[str, Any]] = []
        for ch in sorted(readings):
            if ch not in product_channels:
                qc["not_in_product"].append(ch)
                continue
            pc = product_channels[ch]
            if pc["status"] != "calibrated":
                qc["uncalibratable"][ch] = {
                    "reason": pc["reason"], "theta_deg": pc["theta_deg"]}
                continue
            candidates.append({
                "channel": ch,
                "theta_deg": pc["theta_deg"],
                "y_raw": readings[ch]["y_raw"],
                "weight": readings[ch]["weight"],
                "gain": pc["gain"],
                "offset": pc["offset"],
                "corrected_s": (readings[ch]["y_raw"] - pc["offset"]) / pc["gain"],
            })

        for ch in sorted(product_channels):
            pc = product_channels[ch]
            if pc["status"] == "calibrated" and ch not in readings:
                qc["missing"].append(ch)

        # Full model-consistency would like at least 3 channels; a 2-channel
        # design may be full rank but is not used (configured minimum).
        min_ch = int(cfg["min_channels_for_inversion"])
        n = len(candidates)
        # Model in raw-reading space:  y = b + (g/2)*(I + Q*cos2θ + U*sin2θ).
        # Reading residual r = y - y_hat satisfies r = -(g/2)*(A p - x) with
        # x = 2*(y-b)/g and A = [1, cos2θ, sin2θ].  Minimizing the *raw-space*
        # weighted RSS sum(w*r^2) is therefore the same as a least-squares solve
        # with design-space weights w*(g/2)^2.  This keeps the reported
        # sum_squared_weighted_residual exactly equal to what was minimized.
        A = np.array([[1.0, *model.cos2_sin2(c["theta_deg"])] for c in candidates],
                     dtype=float) if n else np.zeros((0, 3))
        x_data = np.array([2.0 * c["corrected_s"] for c in candidates], dtype=float)
        w_data = np.array([c["weight"] * (c["gain"] / 2.0) ** 2
                           for c in candidates], dtype=float)

        rank_info: dict[str, Any] = {
            "rank_tol": cfg["rank_tol"], "rank": 0, "n_candidate": n,
            "singular_values": [], "full_rank_required": 3,
            "min_channels_for_inversion": min_ch,
        }
        status: str
        solution: dict[str, Any]
        used: list[dict[str, Any]] = []

        if n < min_ch:
            status = "insufficient_channels"
            rank_info["reason"] = (
                f"only {n} candidate readings; at least {min_ch} are required")
            qc["insufficient_design"].update({
                "reason": rank_info["reason"], "rank": 0, "n_candidate": n,
                "singular_values": [], "rank_tol": cfg["rank_tol"],
                "used_channels": []})
            solution = self._empty_solution(status, rank_info)
        else:
            rank, sv, sol = model.weighted_solve(A, x_data, w_data, cfg["rank_tol"])
            rank_info["rank"] = int(rank)
            rank_info["singular_values"] = [float(v) for v in sv]
            if rank < 3 or sol is None:
                status = "rank_deficient"
                rank_info["reason"] = (
                    f"design rank {rank} < 3 at rank_tol={cfg['rank_tol']}; "
                    "near-collinear angles are not treated as a determined solution")
                qc["insufficient_design"].update({
                    "reason": rank_info["reason"], "rank": int(rank),
                    "n_candidate": n, "singular_values": [float(v) for v in sv],
                    "rank_tol": cfg["rank_tol"], "used_channels": []})
                solution = self._empty_solution(status, rank_info)
            else:
                used = candidates
                status = "solved"
                solution = self._solution_from(used, sol, cfg)
                rank_info["reason"] = None
                solution["rank_diagnostics"] = dict(rank_info)
                consistency = model.check_model_constraints(
                    solution["I"], solution["Q"], solution["U"], cfg)
                solution["constraint_check"] = consistency
                if consistency["consistent"]:
                    solution["status"] = "consistent"
                    solution["polarization"] = model.polarization_report(
                        solution["I"], solution["Q"], solution["U"], cfg, True)
                else:
                    solution["status"] = "model_inconsistent"
                    solution["polarization"] = model.polarization_report(
                        solution["I"], solution["Q"], solution["U"], cfg, False)
                    solution["note"] = (
                        "unconstrained least-squares solution violates I>=0 or "
                        "Q^2+U^2<=I^2 within the recorded tolerance; the "
                        "unconstrained values are retained and flagged. No clamping "
                        "onto the feasible region is performed or reported as "
                        "a consistent observation.")

        solution.update({
            "run_status": status,
            "product_id": product.get("product_id"),
            "product_version": product.get("product_version"),
            "product_format": product.get("format"),
            "product_model_version": product.get("model_version"),
            "exposure_id": exposure_id,
            "model_statement": product.get("model_statement", model.MODEL_STATEMENT),
            "disclaimer": model.DISCLAIMER,
            "qc": qc,
            "used_channels": [c["channel"] for c in used],
        })

        run_id = _new_id("run")
        solution_id = _new_id("sol")
        now = utcnow()
        sol_text = model.json_dumps(solution)
        with self._tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM exposures WHERE exposure_id = ?", (exposure_id,)
            ).fetchone()
            if exists is None:
                raise model.ValidationError(f"unknown exposure_id {exposure_id!r}")
            conn.execute(
                "INSERT INTO runs(run_id, exposure_id, product_id, product_version,"
                " product_sha256, config_json, method, created_at, solution_id,"
                " solution_json, solution_sha256, reof, reof_of)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, exposure_id, str(product.get("product_id")),
                 int(product.get("product_version") or 0),
                 model.sha256_of(product), model.json_dumps(cfg),
                 "raw-space weighted least squares minimizing sum(w*(y-y_hat)^2) "
                 "via A=[1,cos2θ,sin2θ], x=2*(y-b)/g, design weights "
                 "w*(g/2)^2; "
                 f"configured rank_tol={cfg['rank_tol']}",
                 now, solution_id, sol_text, model.sha256_of(solution),
                 "yes" if reof_of else None, reof_of),
            )
            self._event(conn, "run", run_id, "invert", {
                "exposure_id": exposure_id, "product_id": product.get("product_id"),
                "product_version": product.get("product_version"),
                "run_status": status, "reof_of": reof_of,
                "used_channels": [c["channel"] for c in used],
            })
        return self.get_run(run_id)

    def _empty_solution(self, status: str, rank_info: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": status,
            "I": None, "Q": None, "U": None,
            "rank_diagnostics": dict(rank_info),
            "constraint_check": None,
            "polarization": {
                "dolp": None, "dolp_defined": False,
                "angle_deg": None, "angle_defined": False,
                "undefined_reasons": [f"solution_status:{status}"],
                "zero_tol": self.config["zero_tol"],
            },
            "channels": [],
            "sum_squared_weighted_residual": None,
            "n_equations": 0,
        }

    def _solution_from(
        self, used: Sequence[Mapping[str, Any]], sol: np.ndarray,
        cfg: Mapping[str, Any],
    ) -> dict[str, Any]:
        I, Q, U = (float(v) for v in sol)
        channels = []
        ssr = 0.0
        for c in used:
            c2, s2 = model.cos2_sin2(c["theta_deg"])
            s_corr = (I + Q * c2 + U * s2) / 2.0
            y_pred = c["gain"] * s_corr + c["offset"]
            res = c["y_raw"] - y_pred
            wres = c["weight"] * res * res
            ssr += wres
            channels.append({
                "channel": c["channel"],
                "theta_deg": c["theta_deg"],
                "y_raw": c["y_raw"],
                "weight": c["weight"],
                "gain": c["gain"],
                "offset": c["offset"],
                "corrected_s": c["corrected_s"],
                "predicted_s": s_corr,
                "predicted_y_raw": y_pred,
                "residual": res,
                "weighted_residual_sq": wres,
            })
        return {
            "status": "solved",
            "I": I, "Q": Q, "U": U,
            "rank_diagnostics": None,  # filled by caller
            "channels": channels,
            "n_equations": len(used),
            "sum_squared_weighted_residual": float(ssr),
        }

    # -- reads / reopen / trace ------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise model.ValidationError(f"unknown run_id {run_id!r}")
        return self._run_row(row)

    def _run_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "exposure_id": row["exposure_id"],
            "product_id": row["product_id"],
            "product_version": row["product_version"],
            "product_sha256": row["product_sha256"],
            "config": json.loads(row["config_json"]),
            "method": row["method"],
            "created_at": row["created_at"],
            "solution_id": row["solution_id"],
            "solution_sha256": row["solution_sha256"],
            "reof": row["reof"],
            "reof_of": row["reof_of"],
            "solution": json.loads(row["solution_json"]),
        }

    def list_runs(self, exposure_id: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT run_id, exposure_id, product_id, product_version, created_at,"
               " solution_json, reof_of FROM runs")
        args: tuple[Any, ...] = ()
        if exposure_id is not None:
            sql += " WHERE exposure_id = ?"
            args = (exposure_id,)
        sql += " ORDER BY created_at, rowid"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            sol = json.loads(r["solution_json"])
            out.append({
                "run_id": r["run_id"], "exposure_id": r["exposure_id"],
                "product_id": r["product_id"],
                "product_version": r["product_version"],
                "created_at": r["created_at"],
                "run_status": sol.get("run_status"),
                "solution_status": sol.get("status"),
                "I": sol.get("I"), "Q": sol.get("Q"), "U": sol.get("U"),
                "reof_of": r["reof_of"],
            })
        return out

    def reopen_run(
        self, run_id: str, product_payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Re-open a run and optionally verify the pinned product unchanged."""
        run = self.get_run(run_id)
        integrity = {
            "run_solution_sha256_ok": (
                model.sha256_of(run["solution"]) == run["solution_sha256"]),
            "product_supplied": product_payload is not None,
        }
        if product_payload is not None:
            normalized = model.validate_payload(product_payload)
            digest = model.sha256_of(normalized)
            integrity["product_sha256"] = digest
            integrity["product_sha256_ok"] = digest == run["product_sha256"]
            integrity["product_id_ok"] = (
                normalized.get("product_id") == run["product_id"])
        run["integrity"] = integrity
        return run

    def rerun(
        self, prior_run_id: str, product_payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Re-invert the same exposure against a (possibly new) product.

        Creates a new immutable run linked to the prior one; the prior run keeps
        its original product binding.
        """
        prior = self.get_run(prior_run_id)
        return self.invert(prior["exposure_id"], product_payload,
                           reof_of=prior_run_id)

    def trace_exposure(self, exposure_id: str) -> dict[str, Any]:
        exposure = self.get_exposure(exposure_id)
        runs = self.list_runs(exposure_id)
        return {
            "exposure": exposure,
            "runs": runs,
            "note": "Each run binds one product id/version/sha; different products "
                    "yield independent runs for the same raw readings.",
        }

    def trace_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        exposure = self.get_exposure(run["exposure_id"])
        return {
            "run": run,
            "exposure": exposure,
            "note": "The run solution is immutable; per-channel corrected values, "
                    "predictions and residuals are retained in the solution.",
        }

    def events(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM warehouse_events ORDER BY event_id").fetchall()
        return [{
            "event_id": r["event_id"], "at": r["at"], "entity": r["entity"],
            "entity_id": r["entity_id"], "action": r["action"],
            "detail": json.loads(r["detail_json"]),
        } for r in rows]
