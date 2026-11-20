"""Independent horizon catalogue service (SQLite-backed).

The catalogue owns *only* the horizon partial order and lithology
acceptance sets.  It never imports boreholes, segments or samples.

Workflow
--------
1. ``init`` a catalogue database.
2. Edit the working draft: ``horizon-add``, ``lith-add``/``lith-remove``,
   ``edge-add`` (self-loops and directed cycles are rejected immediately) and
   ``edge-remove``.
3. ``validate`` the draft.
4. ``publish`` an immutable, versioned product.  Publishing is atomic and is
   refused while the draft contains a cycle or any other violation, so a
   cyclic correction can never be partially published.
5. Query products: transitive relation with its edge path, sequence
   validation and incomparability evidence.  Every published edge and
   lithology acceptance row keeps its original ``source`` provenance.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Edge, Horizon, Product
from .poset import (
    CycleError,
    PartialOrder,
    PosetError,
    UnknownHorizonError,
)
from .storage import connect, fetch_all_dicts, require_schema

MAX_HORIZONS = 12

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS horizon (
    horizon_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    source     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lithology_accept (
    horizon_id TEXT NOT NULL REFERENCES horizon(horizon_id),
    lith_code  TEXT NOT NULL,
    source     TEXT NOT NULL,
    PRIMARY KEY (horizon_id, lith_code)
);

CREATE TABLE IF NOT EXISTS edge (
    parent_id TEXT NOT NULL REFERENCES horizon(horizon_id),
    child_id  TEXT NOT NULL REFERENCES horizon(horizon_id),
    source    TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id),
    CHECK (parent_id <> child_id)
);

CREATE TABLE IF NOT EXISTS product (
    product_id TEXT PRIMARY KEY,
    version    INTEGER NOT NULL UNIQUE,
    kind       TEXT NOT NULL CHECK (kind IN ('local', 'external')),
    note       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_horizon (
    product_id TEXT NOT NULL REFERENCES product(product_id),
    horizon_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    source     TEXT NOT NULL,
    PRIMARY KEY (product_id, horizon_id)
);

CREATE TABLE IF NOT EXISTS product_lithology (
    product_id TEXT NOT NULL REFERENCES product(product_id),
    horizon_id TEXT NOT NULL,
    lith_code  TEXT NOT NULL,
    source     TEXT NOT NULL,
    PRIMARY KEY (product_id, horizon_id, lith_code)
);

CREATE TABLE IF NOT EXISTS product_edge (
    product_id TEXT NOT NULL REFERENCES product(product_id),
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    source     TEXT NOT NULL,
    PRIMARY KEY (product_id, parent_id, child_id)
);
"""


class CatalogError(RuntimeError):
    pass


class CatalogService:
    def __init__(self, db_path: str | Path):
        self.conn = connect(db_path)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Schema / meta
    # ------------------------------------------------------------------ #

    def init(self) -> None:
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('product_seq', '0')"
        )
        self.conn.commit()

    def _require_db(self) -> None:
        require_schema(self.conn, "horizon")

    def _next_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='product_seq'"
        ).fetchone()
        return int(row["value"]) + 1

    # ------------------------------------------------------------------ #
    # Draft: horizons and lithology sets
    # ------------------------------------------------------------------ #

    def add_horizon(
        self, horizon_id: str, name: str, lithology: list[str], source: str
    ) -> None:
        self._require_db()
        if not horizon_id:
            raise CatalogError("horizon id must be non-empty")
        count = self.conn.execute("SELECT COUNT(*) AS c FROM horizon").fetchone()["c"]
        if count >= MAX_HORIZONS:
            raise CatalogError(f"horizon limit exceeded (max {MAX_HORIZONS})")
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO horizon(horizon_id, name, source) VALUES (?,?,?)",
                    (horizon_id, name, source),
                )
                for code in sorted(set(lithology)):
                    self.conn.execute(
                        "INSERT INTO lithology_accept(horizon_id, lith_code, source)"
                        " VALUES (?,?,?)",
                        (horizon_id, code, source),
                    )
        except sqlite3.IntegrityError as exc:
            raise CatalogError(f"horizon {horizon_id!r} already exists") from exc

    def add_lithology(self, horizon_id: str, lith_code: str, source: str) -> None:
        self._require_db()
        self._require_horizon(horizon_id)
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO lithology_accept(horizon_id, lith_code, source)"
                    " VALUES (?,?,?)",
                    (horizon_id, lith_code, source),
                )
        except sqlite3.IntegrityError:
            raise CatalogError(
                f"lithology {lith_code!r} already accepted by {horizon_id!r}"
            ) from None

    def remove_lithology(self, horizon_id: str, lith_code: str) -> None:
        self._require_db()
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM lithology_accept WHERE horizon_id=? AND lith_code=?",
                (horizon_id, lith_code),
            )
        if cur.rowcount == 0:
            raise CatalogError(
                f"lithology {lith_code!r} is not accepted by {horizon_id!r}"
            )

    def _require_horizon(self, horizon_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM horizon WHERE horizon_id=?", (horizon_id,)
        ).fetchone()
        if row is None:
            raise CatalogError(f"unknown horizon {horizon_id!r}")

    # ------------------------------------------------------------------ #
    # Draft: edges
    # ------------------------------------------------------------------ #

    def add_edge(self, parent_id: str, child_id: str, source: str) -> None:
        """Add a direct shallower_than edge, rejecting loops and cycles."""
        self._require_db()
        self._require_horizon(parent_id)
        self._require_horizon(child_id)
        if parent_id == child_id:
            raise CatalogError(f"self-loop forbidden on horizon {parent_id!r}")

        existing = {
            (r["parent_id"], r["child_id"])
            for r in self.conn.execute("SELECT parent_id, child_id FROM edge")
        }
        if (parent_id, child_id) in existing:
            raise CatalogError(
                f"edge {parent_id!r} -> {child_id!r} already exists"
            )
        existing.add((parent_id, child_id))
        horizon_ids = [
            r["horizon_id"] for r in self.conn.execute(
                "SELECT horizon_id FROM horizon ORDER BY horizon_id"
            )
        ]
        try:
            PartialOrder(horizon_ids, list(existing))
        except CycleError as exc:
            raise CatalogError(
                f"edge rejected because it would create a directed cycle: {exc}"
            ) from exc
        except PosetError as exc:
            raise CatalogError(str(exc)) from exc

        with self.conn:
            self.conn.execute(
                "INSERT INTO edge(parent_id, child_id, source) VALUES (?,?,?)",
                (parent_id, child_id, source),
            )

    def remove_edge(self, parent_id: str, child_id: str) -> None:
        self._require_db()
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM edge WHERE parent_id=? AND child_id=?",
                (parent_id, child_id),
            )
        if cur.rowcount == 0:
            raise CatalogError(
                f"edge {parent_id!r} -> {child_id!r} does not exist"
            )

    # ------------------------------------------------------------------ #
    # Draft introspection / validation
    # ------------------------------------------------------------------ #

    def list_horizons(self) -> list[dict]:
        self._require_db()
        rows = fetch_all_dicts(
            self.conn,
            "SELECT horizon_id, name, source FROM horizon ORDER BY horizon_id",
        )
        for row in rows:
            row["acceptable_lithology"] = [
                r["lith_code"]
                for r in self.conn.execute(
                    "SELECT lith_code FROM lithology_accept WHERE horizon_id=?"
                    " ORDER BY lith_code",
                    (row["horizon_id"],),
                )
            ]
        return rows

    def list_edges(self) -> list[dict]:
        self._require_db()
        return fetch_all_dicts(
            self.conn,
            "SELECT parent_id, child_id, source FROM edge"
            " ORDER BY parent_id, child_id",
        )

    def _draft_parts(self) -> tuple[list[Horizon], list[Edge]]:
        horizons: list[Horizon] = []
        for row in self.list_horizons():
            lith_rows = self.conn.execute(
                "SELECT lith_code, source FROM lithology_accept WHERE horizon_id=?"
                " ORDER BY lith_code",
                (row["horizon_id"],),
            ).fetchall()
            # Lithology provenance is per row; horizon source is its own.
            horizons.append(
                Horizon(
                    horizon_id=row["horizon_id"],
                    name=row["name"],
                    acceptable_lithology=frozenset(r["lith_code"] for r in lith_rows),
                    source=row["source"],
                )
            )
        edges = [
            Edge(r["parent_id"], r["child_id"], r["source"])
            for r in self.conn.execute(
                "SELECT parent_id, child_id, source FROM edge"
                " ORDER BY parent_id, child_id"
            )
        ]
        return horizons, edges

    def build_draft_poset(self) -> PartialOrder:
        """Build the draft partial order, raising on any structural defect."""
        horizons, edges = self._draft_parts()
        ids = [h.horizon_id for h in horizons]
        return PartialOrder(ids, [(e.parent_id, e.child_id) for e in edges])

    def validate_draft(self) -> dict:
        """Return ``{'ok': bool, 'errors': [...], 'warnings': [...]}``."""
        self._require_db()
        errors: list[str] = []
        warnings: list[str] = []
        horizons, edges = self._draft_parts()
        if not horizons:
            errors.append("catalogue contains no horizons")
        ids = [h.horizon_id for h in horizons]
        id_set = set(ids)
        for edge in edges:
            if edge.parent_id not in id_set or edge.child_id not in id_set:
                errors.append(
                    f"edge {edge.parent_id!r} -> {edge.child_id!r} references "
                    f"an unknown horizon"
                )
            if edge.parent_id == edge.child_id:
                errors.append(f"self-loop forbidden on horizon {edge.parent_id!r}")
        known_edges = [(e.parent_id, e.child_id) for e in edges
                       if e.parent_id in id_set and e.child_id in id_set
                       and e.parent_id != e.child_id]
        try:
            PartialOrder(ids, known_edges)
        except CycleError as exc:
            errors.append(str(exc))
        except UnknownHorizonError as exc:
            errors.append(str(exc))
        for horizon in horizons:
            if not horizon.acceptable_lithology:
                warnings.append(
                    f"horizon {horizon.horizon_id!r} accepts no lithology code; "
                    f"no segment can be assigned to it"
                )
        return {"ok": not errors, "errors": errors, "warnings": warnings}

    # ------------------------------------------------------------------ #
    # Publishing (immutable products, atomic, no partial publish on cycles)
    # ------------------------------------------------------------------ #

    def publish(self, note: str, product_id: str | None = None) -> Product:
        """Freeze the current draft into a new immutable local product."""
        self._require_db()
        report = self.validate_draft()
        if not report["ok"]:
            raise CatalogError(
                "refusing to publish an invalid catalogue: "
                + "; ".join(report["errors"])
            )
        horizons, edges = self._draft_parts()
        version = self._next_version()
        pid = product_id or f"P{version:04d}"
        product = Product(
            product_id=pid,
            version=version,
            kind="local",
            note=note,
            horizons=tuple(horizons),
            edges=tuple(edges),
        )
        created = datetime.now(timezone.utc).isoformat()
        with self.conn:
            try:
                self.conn.execute(
                    "INSERT INTO product(product_id, version, kind, note,"
                    " payload_json, created_at) VALUES (?,?,?,?,?,?)",
                    (pid, version, "local", note, product.to_json(), created),
                )
            except sqlite3.IntegrityError as exc:
                raise CatalogError(f"product {pid!r} already exists") from exc
            for horizon in horizons:
                self.conn.execute(
                    "INSERT INTO product_horizon(product_id, horizon_id, name,"
                    " source) VALUES (?,?,?,?)",
                    (pid, horizon.horizon_id, horizon.name, horizon.source),
                )
                for lith in sorted(horizon.acceptable_lithology):
                    lith_source = self.conn.execute(
                        "SELECT source FROM lithology_accept WHERE horizon_id=?"
                        " AND lith_code=?",
                        (horizon.horizon_id, lith),
                    ).fetchone()["source"]
                    self.conn.execute(
                        "INSERT INTO product_lithology(product_id, horizon_id,"
                        " lith_code, source) VALUES (?,?,?,?)",
                        (pid, horizon.horizon_id, lith, lith_source),
                    )
            for edge in edges:
                self.conn.execute(
                    "INSERT INTO product_edge(product_id, parent_id, child_id,"
                    " source) VALUES (?,?,?,?)",
                    (pid, edge.parent_id, edge.child_id, edge.source),
                )
            self.conn.execute(
                "UPDATE meta SET value=? WHERE key='product_seq'", (str(version),)
            )
        return product

    def list_products(self) -> list[dict]:
        self._require_db()
        return fetch_all_dicts(
            self.conn,
            "SELECT product_id, version, kind, note, created_at FROM product"
            " ORDER BY version",
        )

    def get_product(self, product_id: str) -> Product:
        self._require_db()
        row = self.conn.execute(
            "SELECT payload_json FROM product WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise CatalogError(f"unknown product {product_id!r}")
        return Product.from_json(row["payload_json"])

    def latest_product(self) -> Product | None:
        self._require_db()
        row = self.conn.execute(
            "SELECT payload_json FROM product ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return Product.from_json(row["payload_json"]) if row else None

    def export_product(self, product_id: str) -> str:
        return self.get_product(product_id).to_json()

    # ------------------------------------------------------------------ #
    # Queries (draft by default, or any published product)
    # ------------------------------------------------------------------ #

    def _poset_for(self, product_id: str | None) -> PartialOrder:
        if product_id is None:
            return self.build_draft_poset()
        product = self.get_product(product_id)
        return PartialOrder(
            [h.horizon_id for h in product.horizons],
            [(e.parent_id, e.child_id) for e in product.edges],
        )

    def query_relation(
        self, shallower: str, deeper: str, product_id: str | None = None
    ) -> dict:
        poset = self._poset_for(product_id)
        path = poset.path(shallower, deeper)
        return {
            "shallower": shallower,
            "deeper": deeper,
            "shallower_than": path is not None,
            "witness_path": list(path) if path else None,
            "product_id": product_id,
        }

    def query_evidence(
        self, left_id: str, right_id: str, product_id: str | None = None
    ) -> dict:
        poset = self._poset_for(product_id)
        evidence = poset.evidence(left_id, right_id)
        return {
            "left": left_id,
            "right": right_id,
            "incomparable": evidence.is_incomparable,
            "forward_path": list(evidence.forward_path)
            if evidence.forward_path else None,
            "backward_path": list(evidence.backward_path)
            if evidence.backward_path else None,
            "explanation": evidence.explain(),
            "product_id": product_id,
        }

    def validate_sequence(
        self, sequence: list[str], product_id: str | None = None
    ) -> dict:
        poset = self._poset_for(product_id)
        violations = poset.validate_sequence(sequence)
        return {
            "sequence": sequence,
            "valid": not violations,
            "violations": violations,
            "product_id": product_id,
        }

    def provenance(self, product_id: str) -> dict:
        """Relational provenance view of a published product."""
        self.get_product(product_id)  # existence check
        return {
            "product_id": product_id,
            "horizons": fetch_all_dicts(
                self.conn,
                "SELECT horizon_id, name, source FROM product_horizon"
                " WHERE product_id=? ORDER BY horizon_id",
                (product_id,),
            ),
            "lithology": fetch_all_dicts(
                self.conn,
                "SELECT horizon_id, lith_code, source FROM product_lithology"
                " WHERE product_id=? ORDER BY horizon_id, lith_code",
                (product_id,),
            ),
            "edges": fetch_all_dicts(
                self.conn,
                "SELECT parent_id, child_id, source FROM product_edge"
                " WHERE product_id=? ORDER BY parent_id, child_id",
                (product_id,),
            ),
        }
