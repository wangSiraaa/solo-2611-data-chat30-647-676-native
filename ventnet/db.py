"""SQLite-backed catalog directory and observation store.

Everything is append-only by identity: catalogs are keyed by their SHA-256
identity, experiments keep the identity of the catalog revision they were run
against.  Revising a flow inserts a *new* catalog row; the old experiment rows
and their recorded basis are untouched.  Source events and person visits are
stored in their own tables, independently of any computed result, so results
are never the only copy of the inputs.
"""

from __future__ import annotations

import json
import sqlite3
from fractions import Fraction
from typing import Mapping, Sequence

from .catalog import Catalog
from .errors import NotFoundError, ValidationError
from .experiment import Experiment, SourceEvent, Visit, run_experiment
from .rational import parse_nonnegative, rational_pair

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalogs (
    identity       TEXT PRIMARY KEY,
    parent_identity TEXT,
    revision       INTEGER NOT NULL,
    name           TEXT NOT NULL,
    product_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experiments (
    identity         TEXT PRIMARY KEY,
    catalog_identity TEXT NOT NULL,
    window_start     REAL NOT NULL,
    window_end       REAL NOT NULL,
    c0_json          TEXT NOT NULL,
    result_json      TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (catalog_identity) REFERENCES catalogs(identity)
);

-- Source events persist independently of any experiment result.
CREATE TABLE IF NOT EXISTS source_events (
    experiment_id TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    time_s        REAL NOT NULL,
    room          TEXT NOT NULL,
    strength_num  INTEGER NOT NULL,
    strength_den  INTEGER NOT NULL,
    PRIMARY KEY (experiment_id, seq),
    FOREIGN KEY (experiment_id) REFERENCES experiments(identity)
);

-- Person visits persist independently of any experiment result.
CREATE TABLE IF NOT EXISTS visits (
    experiment_id TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    visit_id      TEXT NOT NULL,
    person        TEXT NOT NULL,
    room          TEXT NOT NULL,
    start_s       REAL NOT NULL,
    end_s         REAL NOT NULL,
    PRIMARY KEY (experiment_id, seq),
    FOREIGN KEY (experiment_id) REFERENCES experiments(identity)
);

CREATE TABLE IF NOT EXISTS experiment_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id    TEXT NOT NULL,
    catalog_identity TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (experiment_id) REFERENCES experiments(identity)
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- catalogs ---------------------------------------------------------
    def save_catalog(self, catalog: Catalog) -> str:
        """Persist a published catalog. Returns its identity."""
        catalog.require_published()
        cur = self.conn.execute(
            "SELECT identity FROM catalogs WHERE identity=?", (catalog.identity,)
        )
        if cur.fetchone() is None:
            self.conn.execute(
                "INSERT INTO catalogs(identity,parent_identity,revision,name,product_json)"
                " VALUES(?,?,?,?,?)",
                (
                    catalog.identity,
                    catalog.parent_identity,
                    catalog.revision,
                    catalog.name,
                    json.dumps(catalog.to_dict(), sort_keys=True, separators=(",", ":")),
                ),
            )
            self.conn.commit()
        return catalog.identity

    def get_catalog(self, identity: str) -> Catalog:
        row = self.conn.execute(
            "SELECT product_json FROM catalogs WHERE identity=?", (identity,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"catalog {identity!r} not found")
        product = json.loads(row["product_json"])
        cat = Catalog.from_product(product)
        # Trust but verify: the stored identity must match recomputed content.
        if cat.identity != identity:
            raise ValidationError(
                f"catalog identity mismatch for {identity!r}: content hashes to {cat.identity}"
            )
        return cat

    def verify_catalog(self, identity: str) -> dict:
        """Recompute the identity from stored contents and compare."""
        row = self.conn.execute(
            "SELECT identity, product_json FROM catalogs WHERE identity=?", (identity,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"catalog {identity!r} not found")
        product = json.loads(row["product_json"])
        recomputed = Catalog.from_product(product).identity
        report = Catalog.from_product(product).validate()
        return {
            "stored_identity": identity,
            "recomputed_identity": recomputed,
            "ok": recomputed == identity and report["balanced"],
            "balance": report,
        }

    def list_catalogs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT identity,parent_identity,revision,name FROM catalogs ORDER BY revision, created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- experiments ------------------------------------------------------
    def _persist_experiment_inputs(self, exp: Experiment) -> None:
        ident = exp.identity
        for seq, ev in enumerate(exp.source_events):
            f = Fraction(ev.strength)
            self.conn.execute(
                "INSERT OR REPLACE INTO source_events"
                "(experiment_id,seq,time_s,room,strength_num,strength_den)"
                " VALUES(?,?,?,?,?,?)",
                (ident, seq, ev.time, ev.room, f.numerator, f.denominator),
            )
        for seq, v in enumerate(exp.visits):
            self.conn.execute(
                "INSERT OR REPLACE INTO visits"
                "(experiment_id,seq,visit_id,person,room,start_s,end_s)"
                " VALUES(?,?,?,?,?,?,?)",
                (ident, seq, v.visit_id, v.person, v.room, v.start, v.end),
            )

    def import_experiment(self, *, catalog: Catalog | str, initial_concentration: Mapping,
                          source_events: Sequence[Mapping], window: Sequence[float],
                          visits: Sequence[Mapping]) -> Experiment:
        """Persist an experiment (inputs only) without computing a result."""
        if isinstance(catalog, str):
            catalog_obj = self.get_catalog(catalog)
        else:
            catalog_obj = catalog
            self.save_catalog(catalog_obj)
        src = [
            SourceEvent(
                time=float(e["time"]),
                room=str(e["room"]),
                strength=parse_nonnegative(e["strength"], field="source strength"),
            )
            for e in source_events
        ]
        vis = [
            Visit(
                visit_id=str(v["visit_id"]),
                person=str(v["person"]),
                room=str(v["room"]),
                start=float(v["start"]),
                end=float(v["end"]),
            )
            for v in visits
        ]
        exp = Experiment(
            catalog=catalog_obj,
            initial_concentration=initial_concentration,
            source_events=src,
            window_start=float(window[0]),
            window_end=float(window[1]),
            visits=vis,
        )
        c0_json = json.dumps(
            {k: rational_pair(v) for k, v in exp.initial_concentration.items()},
            sort_keys=True,
            separators=(",", ":"),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO experiments"
            "(identity,catalog_identity,window_start,window_end,c0_json)"
            " VALUES(?,?,?,?,?)",
            (exp.identity, catalog_obj.identity, exp.window_start, exp.window_end, c0_json),
        )
        # Rewrite inputs in case the same identity was re-imported.
        self.conn.execute("DELETE FROM source_events WHERE experiment_id=?", (exp.identity,))
        self.conn.execute("DELETE FROM visits WHERE experiment_id=?", (exp.identity,))
        self._persist_experiment_inputs(exp)
        self.conn.commit()
        return exp

    def load_experiment(self, identity: str) -> Experiment:
        row = self.conn.execute(
            "SELECT * FROM experiments WHERE identity=?", (identity,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"experiment {identity!r} not found")
        catalog = self.get_catalog(row["catalog_identity"])
        c0 = {k: Fraction(v["num"], v["den"]) for k, v in json.loads(row["c0_json"]).items()}
        src_rows = self.conn.execute(
            "SELECT * FROM source_events WHERE experiment_id=? ORDER BY seq", (identity,)
        ).fetchall()
        sources = [
            SourceEvent(
                time=r["time_s"],
                room=r["room"],
                strength=Fraction(r["strength_num"], r["strength_den"]),
            )
            for r in src_rows
        ]
        vis_rows = self.conn.execute(
            "SELECT * FROM visits WHERE experiment_id=? ORDER BY seq", (identity,)
        ).fetchall()
        visits = [
            Visit(
                visit_id=r["visit_id"],
                person=r["person"],
                room=r["room"],
                start=r["start_s"],
                end=r["end_s"],
            )
            for r in vis_rows
        ]
        return Experiment(
            catalog=catalog,
            initial_concentration=c0,
            source_events=sources,
            window_start=row["window_start"],
            window_end=row["window_end"],
            visits=visits,
        )

    def run(self, identity: str, *, atol: float = 1e-7) -> dict:
        exp = self.load_experiment(identity)
        result = run_experiment(exp, atol=atol)
        payload = result.to_dict()
        self.conn.execute(
            "UPDATE experiments SET result_json=? WHERE identity=?",
            (json.dumps(payload, separators=(",", ":")), identity),
        )
        self.conn.execute(
            "INSERT INTO experiment_runs(experiment_id,catalog_identity,result_json)"
            " VALUES(?,?,?)",
            (identity, result.catalog_identity, json.dumps(payload, separators=(",", ":"))),
        )
        self.conn.commit()
        return payload

    def get_result(self, identity: str) -> dict:
        row = self.conn.execute(
            "SELECT result_json FROM experiments WHERE identity=?", (identity,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"experiment {identity!r} not found")
        if row["result_json"] is None:
            raise NotFoundError(f"experiment {identity!r} has not been run yet")
        return json.loads(row["result_json"])

    def list_experiments(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT identity,catalog_identity,window_start,window_end,"
            "(result_json IS NOT NULL) AS has_result FROM experiments ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- revisions --------------------------------------------------------
    def revise_flow(self, identity: str, src: str, dst: str, new_flow) -> Catalog:
        """Create a new balanced catalog revision and persist it."""
        return self.revise_flows(identity, {(src, dst): new_flow})

    def revise_flows(self, identity: str, changes: Mapping) -> Catalog:
        """Atomically revise a group of flows and persist a new revision."""
        parent = self.get_catalog(identity)
        revised = parent.revise_flows(changes)
        self.save_catalog(revised)
        return revised
