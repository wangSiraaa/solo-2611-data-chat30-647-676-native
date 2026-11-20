"""Borehole analysis repository.

The repository consumes immutable catalogue products (either local products
read from a catalogue database or external product JSON files), imports
boreholes and samples, runs the exact correlation solver and persists:

* the selected global scheme per product (immutable once stored);
* segment-to-horizon assignments (UNKNOWN included);
* per-horizon / per-borehole top-bottom, cored and gap thickness;
* explicit gap attribution (same-horizon rule only);
* sample membership under half-open ``[start, end)`` semantics.

Re-solving against a corrected product genuinely produces a *new* scheme and
new sample attributions; previous schemes stay fixed and queryable forever.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import rational as R
from .geometry import (
    SAMPLE_UNKNOWN,
    assign_samples,
    build_statistics,
    validate_borehole,
)
from .models import BoreholeInput, GapInput, Product, SampleInput, SegmentInput
from .poset import PartialOrder
from .solver import (
    CorrelationContradiction,
    CorrelationResult,
    detect_contradictions,
    solve_all,
)
from .storage import connect, fetch_all_dicts, require_schema

MAX_BOREHOLES = 8
MAX_SEGMENTS_PER_HOLE = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product (
    product_id   TEXT PRIMARY KEY,
    version      INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('local', 'external')),
    origin       TEXT NOT NULL,
    note         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    imported_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS borehole (
    borehole_id TEXT PRIMARY KEY,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segment (
    borehole_id   TEXT NOT NULL REFERENCES borehole(borehole_id),
    segment_id    TEXT NOT NULL,
    ordinal       INTEGER NOT NULL,
    start_depth   TEXT NOT NULL,
    end_depth     TEXT NOT NULL,
    lithology     TEXT NOT NULL,
    weight        INTEGER NOT NULL CHECK (weight > 0),
    fixed_horizon TEXT,
    PRIMARY KEY (borehole_id, segment_id),
    UNIQUE (borehole_id, ordinal)
);

CREATE TABLE IF NOT EXISTS gap (
    borehole_id TEXT NOT NULL REFERENCES borehole(borehole_id),
    gap_id      TEXT NOT NULL,
    start_depth TEXT NOT NULL,
    end_depth   TEXT NOT NULL,
    PRIMARY KEY (borehole_id, gap_id)
);

CREATE TABLE IF NOT EXISTS sample (
    sample_id   TEXT PRIMARY KEY,
    borehole_id TEXT REFERENCES borehole(borehole_id),
    depth       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheme (
    scheme_id    TEXT PRIMARY KEY,
    product_id   TEXT NOT NULL REFERENCES product(product_id),
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('selected', 'contradiction')),
    total_unknown_weight INTEGER,
    total_switches       INTEGER,
    contradiction_json   TEXT
);

CREATE TABLE IF NOT EXISTS assignment (
    scheme_id    TEXT NOT NULL REFERENCES scheme(scheme_id),
    borehole_id  TEXT NOT NULL,
    segment_id   TEXT NOT NULL,
    ordinal      INTEGER NOT NULL,
    horizon_id   TEXT,            -- NULL means UNKNOWN
    is_unknown   INTEGER NOT NULL,
    PRIMARY KEY (scheme_id, borehole_id, segment_id)
);

CREATE TABLE IF NOT EXISTS horizon_stat (
    scheme_id        TEXT NOT NULL REFERENCES scheme(scheme_id),
    borehole_id      TEXT NOT NULL,
    horizon_id       TEXT NOT NULL,
    top_depth        TEXT NOT NULL,
    bottom_depth     TEXT NOT NULL,
    cored_thickness  TEXT NOT NULL,
    gap_thickness    TEXT NOT NULL,
    segment_ids_json TEXT NOT NULL,
    gap_ids_json     TEXT NOT NULL,
    PRIMARY KEY (scheme_id, borehole_id, horizon_id)
);

CREATE TABLE IF NOT EXISTS gap_attribution (
    scheme_id      TEXT NOT NULL REFERENCES scheme(scheme_id),
    borehole_id    TEXT NOT NULL,
    gap_id         TEXT NOT NULL,
    start_depth    TEXT NOT NULL,
    end_depth      TEXT NOT NULL,
    thickness      TEXT NOT NULL,
    horizon_id     TEXT,          -- NULL == UNDETERMINED
    reason         TEXT NOT NULL,
    PRIMARY KEY (scheme_id, borehole_id, gap_id)
);

CREATE TABLE IF NOT EXISTS sample_membership (
    scheme_id     TEXT NOT NULL REFERENCES scheme(scheme_id),
    sample_id     TEXT NOT NULL,
    borehole_id   TEXT,
    depth         TEXT NOT NULL,
    status        TEXT NOT NULL,
    segment_id    TEXT,
    horizon_id    TEXT,
    gap_id        TEXT,
    note          TEXT NOT NULL,
    PRIMARY KEY (scheme_id, sample_id)
);
"""


class RepoError(RuntimeError):
    pass


class BoreholeRepository:
    def __init__(self, db_path: str | Path):
        self.conn = connect(db_path)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #

    def init(self) -> None:
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('scheme_seq', '0')"
        )
        self.conn.commit()

    def _require_db(self) -> None:
        require_schema(self.conn, "borehole")

    def _next_scheme_id(self) -> str:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='scheme_seq'"
        ).fetchone()
        nxt = int(row["value"]) + 1
        return f"S{nxt:04d}", nxt

    # ------------------------------------------------------------------ #
    # Products (local catalogue product or external product JSON)
    # ------------------------------------------------------------------ #

    def import_product(self, product: Product, origin: str) -> None:
        self._require_db()
        if product.kind not in ("local", "external"):
            raise RepoError(f"unsupported product kind {product.kind!r}")
        # Reject structurally invalid external products before storage.
        PartialOrder(
            [h.horizon_id for h in product.horizons],
            [(e.parent_id, e.child_id) for e in product.edges],
        )
        if len(product.horizons) > 12:
            raise RepoError("product exceeds the 12-horizon limit")
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO product(product_id, version, kind, origin,"
                    " note, payload_json, imported_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        product.product_id,
                        product.version,
                        product.kind,
                        origin,
                        product.note,
                        product.to_json(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        except sqlite3.IntegrityError:
            raise RepoError(
                f"product {product.product_id!r} is already imported; "
                f"corrections must be published as a new product"
            ) from None

    def import_product_file(self, path: str | Path) -> Product:
        product = Product.from_json(Path(path).read_text(encoding="utf-8"))
        if product.kind != "external":
            product = Product(
                product_id=product.product_id,
                version=product.version,
                kind="external",
                note=product.note,
                horizons=product.horizons,
                edges=product.edges,
            )
        self.import_product(product, origin=f"file:{Path(path).resolve()}")
        return product

    def list_products(self) -> list[dict]:
        self._require_db()
        return fetch_all_dicts(
            self.conn,
            "SELECT product_id, version, kind, origin, note, imported_at"
            " FROM product ORDER BY version, product_id",
        )

    def get_product(self, product_id: str) -> Product:
        self._require_db()
        row = self.conn.execute(
            "SELECT payload_json FROM product WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise RepoError(f"unknown product {product_id!r}")
        return Product.from_json(row["payload_json"])

    def product_provenance(self, product_id: str) -> dict:
        self._require_db()
        product = self.get_product(product_id)
        row = self.conn.execute(
            "SELECT kind, origin, imported_at FROM product WHERE product_id=?",
            (product_id,),
        ).fetchone()
        return {
            "product_id": product_id,
            "kind": row["kind"],
            "origin": row["origin"],
            "imported_at": row["imported_at"],
            "horizons": [h.to_dict() for h in product.horizons],
            "edges": [e.to_dict() for e in product.edges],
        }

    # ------------------------------------------------------------------ #
    # Boreholes and samples
    # ------------------------------------------------------------------ #

    def import_borehole(self, borehole: BoreholeInput) -> None:
        self._require_db()
        if len(borehole.segments) > MAX_SEGMENTS_PER_HOLE:
            raise RepoError(
                f"borehole {borehole.borehole_id!r} has "
                f"{len(borehole.segments)} segments (limit "
                f"{MAX_SEGMENTS_PER_HOLE})"
            )
        validate_borehole(borehole)
        count = self.conn.execute("SELECT COUNT(*) AS c FROM borehole").fetchone()["c"]
        if count >= MAX_BOREHOLES:
            raise RepoError(f"borehole limit exceeded (max {MAX_BOREHOLES})")
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO borehole(borehole_id, imported_at) VALUES (?,?)",
                    (borehole.borehole_id, datetime.now(timezone.utc).isoformat()),
                )
                for ordinal, segment in enumerate(borehole.segments):
                    self.conn.execute(
                        "INSERT INTO segment(borehole_id, segment_id, ordinal,"
                        " start_depth, end_depth, lithology, weight, fixed_horizon)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (
                            borehole.borehole_id,
                            segment.segment_id,
                            ordinal,
                            segment.start,
                            segment.end,
                            segment.lithology,
                            segment.weight,
                            segment.fixed_horizon,
                        ),
                    )
                for gap in borehole.gaps:
                    self.conn.execute(
                        "INSERT INTO gap(borehole_id, gap_id, start_depth, end_depth)"
                        " VALUES (?,?,?,?)",
                        (borehole.borehole_id, gap.gap_id, gap.start, gap.end),
                    )
        except sqlite3.IntegrityError as exc:
            raise RepoError(
                f"borehole {borehole.borehole_id!r} already exists"
            ) from exc

    def import_sample(self, sample: SampleInput) -> None:
        self._require_db()
        R.to_fraction(sample.depth)  # validate exact rational depth
        if sample.borehole_id is not None:
            row = self.conn.execute(
                "SELECT 1 FROM borehole WHERE borehole_id=?",
                (sample.borehole_id,),
            ).fetchone()
            if row is None:
                raise RepoError(
                    f"sample {sample.sample_id!r} references unknown borehole "
                    f"{sample.borehole_id!r}"
                )
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO sample(sample_id, borehole_id, depth) VALUES (?,?,?)",
                    (sample.sample_id, sample.borehole_id, sample.depth),
                )
        except sqlite3.IntegrityError:
            raise RepoError(f"sample {sample.sample_id!r} already exists") from None

    def list_boreholes(self) -> list[dict]:
        self._require_db()
        return fetch_all_dicts(
            self.conn,
            "SELECT b.borehole_id, "
            " (SELECT COUNT(*) FROM segment s WHERE s.borehole_id=b.borehole_id)"
            "   AS segment_count,"
            " (SELECT COUNT(*) FROM gap g WHERE g.borehole_id=b.borehole_id)"
            "   AS gap_count"
            " FROM borehole b ORDER BY b.borehole_id",
        )

    def load_boreholes(self) -> dict[str, BoreholeInput]:
        boreholes: dict[str, BoreholeInput] = {}
        for brow in self.conn.execute(
            "SELECT borehole_id FROM borehole ORDER BY borehole_id"
        ):
            bid = brow["borehole_id"]
            segments = tuple(
                _segment_from_row(row)
                for row in self.conn.execute(
                    "SELECT * FROM segment WHERE borehole_id=? ORDER BY ordinal",
                    (bid,),
                )
            )
            gaps = tuple(
                GapInput(
                    gap_id=row["gap_id"],
                    start=row["start_depth"],
                    end=row["end_depth"],
                )
                for row in self.conn.execute(
                    "SELECT * FROM gap WHERE borehole_id=? ORDER BY start_depth",
                    (bid,),
                )
            )
            boreholes[bid] = BoreholeInput(bid, segments, gaps)
        return boreholes

    def load_samples(self) -> list[SampleInput]:
        return [
            SampleInput(
                sample_id=row["sample_id"],
                depth=row["depth"],
                borehole_id=row["borehole_id"],
            )
            for row in self.conn.execute(
                "SELECT sample_id, borehole_id, depth FROM sample"
                " ORDER BY sample_id"
            )
        ]

    # ------------------------------------------------------------------ #
    # Solving and persistence
    # ------------------------------------------------------------------ #

    def solve(self, product_id: str, note: str = "") -> dict:
        """Run an exact correlation; persist an immutable scheme.

        Contradictions are also persisted as immutable records (with no
        assignments), so no constraint is ever silently cleared.
        """
        self._require_db()
        product = self.get_product(product_id)
        boreholes = list(self.load_boreholes().values())
        samples = self.load_samples()
        scheme_id, seq = self._next_scheme_id()
        created = datetime.now(timezone.utc).isoformat()

        try:
            result = solve_all(boreholes, product)
        except CorrelationContradiction as exc:
            payload = [
                {
                    "borehole_id": c.borehole_id,
                    "kind": c.kind,
                    "message": c.message,
                    "related_fixes": c.related_fixes,
                    "relations": c.relations,
                }
                for c in exc.contradictions
            ]
            import json

            with self.conn:
                self._insert_scheme_row(
                    scheme_id, product_id, created, "contradiction", None, None,
                    json.dumps(payload, ensure_ascii=False),
                )
                self.conn.execute(
                    "UPDATE meta SET value=? WHERE key='scheme_seq'", (str(seq),)
                )
            return {
                "scheme_id": scheme_id,
                "status": "contradiction",
                "contradictions": payload,
            }

        self._persist_scheme(scheme_id, product_id, created, result, boreholes, samples)
        with self.conn:
            self.conn.execute(
                "UPDATE meta SET value=? WHERE key='scheme_seq'", (str(seq),)
            )
        return {
            "scheme_id": scheme_id,
            "status": "selected",
            "total_unknown_weight": result.total_unknown_weight,
            "total_switches": result.total_switches,
            "lex_vector": list(result.lex_vector),
            "note": note,
        }

    def _insert_scheme_row(
        self, scheme_id, product_id, created, status, unknown_weight,
        switches, contradiction_json,
    ) -> None:
        self.conn.execute(
            "INSERT INTO scheme(scheme_id, product_id, created_at, status,"
            " total_unknown_weight, total_switches, contradiction_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                scheme_id, product_id, created, status, unknown_weight,
                switches, contradiction_json,
            ),
        )

    def _persist_scheme(
        self,
        scheme_id: str,
        product_id: str,
        created: str,
        result: CorrelationResult,
        boreholes: list[BoreholeInput],
        samples: list[SampleInput],
    ) -> None:
        import json

        borehole_by_id = {b.borehole_id: b for b in boreholes}
        with self.conn:
            self._insert_scheme_row(
                scheme_id, product_id, created, "selected",
                result.total_unknown_weight, result.total_switches, None,
            )
            for borehole in boreholes:
                solution = result.hole_solutions[borehole.borehole_id]
                for ordinal, assignment in enumerate(solution.assignments):
                    self.conn.execute(
                        "INSERT INTO assignment(scheme_id, borehole_id,"
                        " segment_id, ordinal, horizon_id, is_unknown)"
                        " VALUES (?,?,?,?,?,?)",
                        (
                            scheme_id,
                            borehole.borehole_id,
                            assignment.segment_id,
                            ordinal,
                            assignment.horizon_id,
                            1 if assignment.is_unknown else 0,
                        ),
                    )
                stats, gap_records = build_statistics(borehole, solution)
                for stat in stats:
                    self.conn.execute(
                        "INSERT INTO horizon_stat(scheme_id, borehole_id,"
                        " horizon_id, top_depth, bottom_depth, cored_thickness,"
                        " gap_thickness, segment_ids_json, gap_ids_json)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            scheme_id,
                            stat.borehole_id,
                            stat.horizon_id,
                            stat.top_depth,
                            stat.bottom_depth,
                            stat.cored_thickness,
                            stat.gap_thickness,
                            json.dumps(list(stat.segment_ids)),
                            json.dumps(list(stat.gap_ids)),
                        ),
                    )
                for gap in gap_records:
                    self.conn.execute(
                        "INSERT INTO gap_attribution(scheme_id, borehole_id,"
                        " gap_id, start_depth, end_depth, thickness, horizon_id,"
                        " reason) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            scheme_id,
                            gap.borehole_id,
                            gap.gap_id,
                            gap.start,
                            gap.end,
                            gap.thickness,
                            gap.horizon_id,
                            gap.reason,
                        ),
                    )
            memberships = assign_samples(
                samples,
                borehole_by_id,
                result.hole_solutions,
            )
            for membership in memberships:
                self.conn.execute(
                    "INSERT INTO sample_membership(scheme_id, sample_id,"
                    " borehole_id, depth, status, segment_id, horizon_id,"
                    " gap_id, note) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        scheme_id,
                        membership.sample_id,
                        membership.borehole_id,
                        membership.depth,
                        membership.status,
                        membership.segment_id,
                        membership.horizon_id,
                        membership.gap_id,
                        membership.note,
                    ),
                )

    def list_schemes(self) -> list[dict]:
        self._require_db()
        return fetch_all_dicts(
            self.conn,
            "SELECT scheme_id, product_id, created_at, status,"
            " total_unknown_weight, total_switches FROM scheme"
            " ORDER BY created_at, scheme_id",
        )

    def _require_scheme(self, scheme_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM scheme WHERE scheme_id=?", (scheme_id,)
        ).fetchone()
        if row is None:
            raise RepoError(f"unknown scheme {scheme_id!r}")
        return dict(row)

    def get_assignments(self, scheme_id: str) -> list[dict]:
        self._require_scheme(scheme_id)
        return fetch_all_dicts(
            self.conn,
            "SELECT borehole_id, segment_id, ordinal, horizon_id, is_unknown"
            " FROM assignment WHERE scheme_id=? ORDER BY borehole_id, ordinal",
            (scheme_id,),
        )

    def get_stats(self, scheme_id: str) -> list[dict]:
        self._require_scheme(scheme_id)
        return fetch_all_dicts(
            self.conn,
            "SELECT * FROM horizon_stat WHERE scheme_id=?"
            " ORDER BY borehole_id, horizon_id",
            (scheme_id,),
        )

    def get_gap_attribution(self, scheme_id: str) -> list[dict]:
        self._require_scheme(scheme_id)
        return fetch_all_dicts(
            self.conn,
            "SELECT borehole_id, gap_id, start_depth, end_depth, thickness,"
            " horizon_id, reason FROM gap_attribution WHERE scheme_id=?"
            " ORDER BY borehole_id, start_depth",
            (scheme_id,),
        )

    def get_sample_membership(self, scheme_id: str) -> list[dict]:
        self._require_scheme(scheme_id)
        return fetch_all_dicts(
            self.conn,
            "SELECT sample_id, borehole_id, depth, status, segment_id,"
            " horizon_id, gap_id, note FROM sample_membership"
            " WHERE scheme_id=? ORDER BY sample_id",
            (scheme_id,),
        )

    def get_contradiction(self, scheme_id: str) -> list[dict]:
        import json

        row = self._require_scheme(scheme_id)
        if row["status"] != "contradiction":
            raise RepoError(f"scheme {scheme_id!r} is not a contradiction record")
        return json.loads(row["contradiction_json"])

    # ------------------------------------------------------------------ #
    # Explanation and provenance
    # ------------------------------------------------------------------ #

    def explain(self, scheme_id: str) -> dict:
        """Explain raw borehole segments, horizon paths and the global choice."""
        row = self._require_scheme(scheme_id)
        product = self.get_product(row["product_id"])
        poset = PartialOrder(
            [h.horizon_id for h in product.horizons],
            [(e.parent_id, e.child_id) for e in product.edges],
        )
        boreholes = self.load_boreholes()

        explanation: dict = {
            "scheme_id": scheme_id,
            "status": row["status"],
            "product_id": product.product_id,
            "product_origin": self.conn.execute(
                "SELECT origin FROM product WHERE product_id=?",
                (product.product_id,),
            ).fetchone()["origin"],
            "boreholes": [],
        }
        if row["status"] == "contradiction":
            import json

            explanation["contradictions"] = json.loads(row["contradiction_json"])
            return explanation

        assignments = self.get_assignments(scheme_id)
        by_hole: dict[str, list[dict]] = {}
        for item in assignments:
            by_hole.setdefault(item["borehole_id"], []).append(item)

        explanation["total_unknown_weight"] = row["total_unknown_weight"]
        explanation["total_switches"] = row["total_switches"]
        explanation["objective_order"] = [
            "minimize total UNKNOWN weight",
            "then minimize per-borehole horizon switches (summed)",
            "then minimize the (borehole id, segment start) horizon-ID vector "
            "lexicographically, UNKNOWN ranked after every real horizon ID",
        ]
        for bid in sorted(boreholes):
            borehole = boreholes[bid]
            hole_rows = by_hole.get(bid, [])
            assigned_ids = [
                None if r["is_unknown"] else r["horizon_id"] for r in hole_rows
            ]
            transitions = []
            previous = None
            for current in assigned_ids:
                if current is None or current == previous:
                    continue
                if previous is not None:
                    path = poset.path(previous, current)
                    transitions.append(
                        {
                            "from": previous,
                            "to": current,
                            "shallower_than_path": list(path) if path else None,
                        }
                    )
                previous = current
            explanation["boreholes"].append(
                {
                    "borehole_id": bid,
                    "raw_segments": [s.to_dict() for s in borehole.segments],
                    "raw_gaps": [g.to_dict() for g in borehole.gaps],
                    "assignment_vector": [
                        "UNKNOWN" if value is None else value for value in assigned_ids
                    ],
                    "horizon_paths": transitions,
                    "unknown_weight": sum(
                        segment.weight
                        for segment, value in zip(borehole.segments, assigned_ids)
                        if value is None
                    ),
                    "switches": sum(
                        1
                        for transition in transitions
                    ),
                }
            )
        return explanation

    # ------------------------------------------------------------------ #
    # Full validation of a stored scheme
    # ------------------------------------------------------------------ #

    def validate(self, scheme_id: str) -> dict:
        """Independently re-derive everything and compare with stored rows."""
        row = self._require_scheme(scheme_id)
        errors: list[str] = []
        product = self.get_product(row["product_id"])
        boreholes = self.load_boreholes()
        samples = self.load_samples()

        if row["status"] == "contradiction":
            poset = PartialOrder(
                [h.horizon_id for h in product.horizons],
                [(e.parent_id, e.child_id) for e in product.edges],
            )
            live = []
            for borehole in boreholes.values():
                live.extend(detect_contradictions(borehole, product, poset))
            if not live:
                errors.append(
                    "stored contradiction is stale: current inputs are feasible"
                )
            return {"scheme_id": scheme_id, "ok": not errors, "errors": errors}

        # Re-solve from scratch.
        try:
            fresh = solve_all(list(boreholes.values()), product)
        except CorrelationContradiction as exc:
            errors.append(f"stored selected scheme is infeasible: {exc}")
            return {"scheme_id": scheme_id, "ok": False, "errors": errors}

        stored_assignments = self.get_assignments(scheme_id)
        fresh_vector = fresh.lex_vector
        stored_vector = tuple(
            "UNKNOWN" if r["is_unknown"] else r["horizon_id"]
            for r in sorted(
                stored_assignments,
                key=lambda r: (r["borehole_id"], r["ordinal"]),
            )
        )
        if stored_vector != tuple(fresh_vector):
            errors.append(
                f"stored assignment vector {list(stored_vector)} does not match "
                f"re-derived optimum {list(fresh_vector)}"
            )
        if row["total_unknown_weight"] != fresh.total_unknown_weight:
            errors.append("stored total UNKNOWN weight disagrees with re-solve")
        if row["total_switches"] != fresh.total_switches:
            errors.append("stored switch count disagrees with re-solve")

        # Fixations must never have been relaxed.
        for item in stored_assignments:
            segment_row = self.conn.execute(
                "SELECT fixed_horizon FROM segment WHERE borehole_id=? AND segment_id=?",
                (item["borehole_id"], item["segment_id"]),
            ).fetchone()
            if segment_row["fixed_horizon"] is not None:
                if item["is_unknown"] or item["horizon_id"] != segment_row["fixed_horizon"]:
                    errors.append(
                        f"fixed segment {item['segment_id']!r} was altered to "
                        f"{item['horizon_id'] or 'UNKNOWN'}"
                    )

        # Re-derive statistics and gap attribution.  The stored-stat index is
        # built once across all boreholes and consumed by the loop, so rows
        # belonging to other boreholes are not mistaken for stale leftovers.
        stored_stats = {
            (r["borehole_id"], r["horizon_id"]): r
            for r in self.get_stats(scheme_id)
        }
        for borehole in boreholes.values():
            solution = fresh.hole_solutions[borehole.borehole_id]
            stats, gaps = build_statistics(borehole, solution)
            for stat in stats:
                key = (stat.borehole_id, stat.horizon_id)
                stored = stored_stats.pop(key, None)
                if stored is None:
                    errors.append(f"missing stored stat for {key}")
                    continue
                for field_name, expected in (
                    ("top_depth", stat.top_depth),
                    ("bottom_depth", stat.bottom_depth),
                    ("cored_thickness", stat.cored_thickness),
                    ("gap_thickness", stat.gap_thickness),
                ):
                    if stored[field_name] != expected:
                        errors.append(
                            f"stat {key} field {field_name}: stored "
                            f"{stored[field_name]!r} != re-derived {expected!r}"
                        )
            stored_gaps = {
                r["gap_id"]: r for r in self.get_gap_attribution(scheme_id)
                if r["borehole_id"] == borehole.borehole_id
            }
            for gap in gaps:
                stored = stored_gaps.get(gap.gap_id)
                if stored is None:
                    errors.append(f"missing gap attribution for {gap.gap_id!r}")
                    continue
                if (stored["horizon_id"] or None) != gap.horizon_id:
                    errors.append(
                        f"gap {gap.gap_id!r}: stored horizon "
                        f"{stored['horizon_id']!r} != re-derived "
                        f"{gap.horizon_id!r}"
                    )
        for leftover in stored_stats:
            errors.append(f"stale stored stat for {leftover}")

        # Re-derive sample membership.
        fresh_membership = {
            m.sample_id: m
            for m in assign_samples(samples, boreholes, fresh.hole_solutions)
        }
        for stored in self.get_sample_membership(scheme_id):
            fresh_item = fresh_membership.get(stored["sample_id"])
            if fresh_item is None:
                errors.append(f"stale sample membership {stored['sample_id']!r}")
                continue
            if stored["status"] != fresh_item.status:
                errors.append(
                    f"sample {stored['sample_id']!r}: stored status "
                    f"{stored['status']!r} != re-derived {fresh_item.status!r}"
                )
            if (stored["horizon_id"] or None) != fresh_item.horizon_id:
                errors.append(
                    f"sample {stored['sample_id']!r}: stored horizon "
                    f"{stored['horizon_id']!r} != re-derived "
                    f"{fresh_item.horizon_id!r}"
                )

        # UNKNOWN samples must never be counted on a known horizon.
        bad = self.conn.execute(
            "SELECT sample_id FROM sample_membership WHERE scheme_id=?"
            " AND status=? AND horizon_id IS NOT NULL",
            (scheme_id, SAMPLE_UNKNOWN),
        ).fetchall()
        if bad:
            errors.append(
                "UNKNOWN-segment samples counted on known horizons: "
                + ", ".join(r["sample_id"] for r in bad)
            )
        return {"scheme_id": scheme_id, "ok": not errors, "errors": errors}


def _segment_from_row(row: sqlite3.Row) -> SegmentInput:
    return SegmentInput(
        segment_id=row["segment_id"],
        start=row["start_depth"],
        end=row["end_depth"],
        lithology=row["lithology"],
        weight=row["weight"],
        fixed_horizon=row["fixed_horizon"],
    )
