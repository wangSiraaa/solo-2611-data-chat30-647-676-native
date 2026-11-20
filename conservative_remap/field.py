"""Conservative cell-constant field warehouse over planar triangle meshes.

The warehouse is independent of the mesh editor.  It accepts any valid planar
triangulation of the same rectangular domain (as a plain snapshot, a mapping or
a published ``MeshService`` version), stores cell-constant densities, and
produces conservative target densities by exact polygon overlap.

A *run* binds immutable source mesh, density set and target mesh identities.
When the real source mesh is refined or a density is corrected a new run is
created; old runs keep their own identity, densities and area matrix.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from fractions import Fraction

from .geometry import (
    GeometryError,
    GeometryPolicy,
    MeshSnapshot,
    MeshValidationError,
    bbox_overlap,
    coerce_snapshot,
    fraction_from_text,
    fraction_text,
    geometry_fingerprint,
    id_from_token,
    id_token,
    intersection_area,
    polygon_area,
    to_fraction,
    validate_mesh,
)


class FieldError(RuntimeError):
    """A field or run cannot be interpreted."""


class IncompleteFieldError(FieldError):
    """A density set or remap run lacks a value required for publication."""


class RemapError(FieldError):
    """The remap itself is impossible (e.g. different domains)."""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationResult:
    """Integral of a cell-constant field over a (clipped) query rectangle."""

    integral: Fraction
    covered_area: Fraction
    query: tuple
    clipped_query: tuple | None
    outside_area: Fraction
    cell_count: int

    def as_dict(self) -> dict:
        return {
            "integral": self.integral,
            "covered_area": self.covered_area,
            "query": self.query,
            "clipped_query": self.clipped_query,
            "outside_area": self.outside_area,
            "cell_count": self.cell_count,
        }


@dataclass(frozen=True)
class RunSummary:
    """Stable description of a remap run, safe to compare across sessions."""

    run_id: int
    source_mesh_id: int
    density_set_id: int
    target_mesh_id: int
    source_fingerprint: str
    target_fingerprint: str
    published: bool
    label: str | None
    created_at: str
    source_area: Fraction
    target_area: Fraction
    source_integral: Fraction
    target_integral: Fraction
    conservation_gap: Fraction
    missing_source_cells: int
    incomplete_targets: int
    zero_area_skipped: int

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _finite(value, what="density") -> Fraction:
    """Reject bools/non-finite input and return an exact Fraction."""
    if isinstance(value, bool):
        raise FieldError(f"{what} must be a finite number, got bool")
    try:
        return to_fraction(value)
    except GeometryError as exc:
        raise FieldError(f"invalid {what}: {exc}") from exc


def _rect_ring(rect) -> list:
    x0, y0, x1, y1 = (to_fraction(v) for v in rect)
    if not (x0 < x1 and y0 < y1):
        raise FieldError("query rectangle must satisfy xmin < xmax and ymin < ymax")
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _clip_rect(rect, domain):
    """Clip a query rectangle to the domain.

    Returns ``(clipped_tuple_or_None, outside_area)`` where ``outside_area`` is
    the query area lying outside the domain.
    """
    x0, y0, x1, y1 = (to_fraction(v) for v in rect)
    dx0, dy0, dx1, dy1 = (to_fraction(v) for v in domain)
    cx0, cy0 = max(x0, dx0), max(y0, dy0)
    cx1, cy1 = min(x1, dx1), min(y1, dy1)
    full = (x1 - x0) * (y1 - y0)
    if not (cx0 < cx1 and cy0 < cy1):
        return None, full
    outside = full - (cx1 - cx0) * (cy1 - cy0)
    return (cx0, cy0, cx1, cy1), outside


def _ring_from_snapshot(snapshot: MeshSnapshot, cid) -> list:
    return [
        (to_fraction(snapshot.vertices[vid][0]), to_fraction(snapshot.vertices[vid][1]))
        for vid in snapshot.cells[cid]
    ]


def _mesh_rows(snapshot: MeshSnapshot):
    vertices = []
    for vid, (x, y) in snapshot.vertices.items():
        vertices.append((id_token(vid), fraction_text(x), fraction_text(y)))
    cells = []
    for cid, tri in snapshot.cells.items():
        cells.append(
            (
                id_token(cid),
                id_token(tri[0]),
                id_token(tri[1]),
                id_token(tri[2]),
            )
        )
    return vertices, cells


# ---------------------------------------------------------------------------
# Warehouse
# ---------------------------------------------------------------------------


class FieldWarehouse:
    """Independent conservative field store and remap engine."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.isolation_level = None
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mesh (
                mesh_id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                domain TEXT NOT NULL,
                policy TEXT NOT NULL,
                label TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mesh_vertex (
                mesh_id INTEGER NOT NULL REFERENCES mesh(mesh_id),
                vid TEXT NOT NULL,
                x TEXT NOT NULL,
                y TEXT NOT NULL,
                PRIMARY KEY (mesh_id, vid)
            );
            CREATE TABLE IF NOT EXISTS mesh_cell (
                mesh_id INTEGER NOT NULL REFERENCES mesh(mesh_id),
                cid TEXT NOT NULL,
                v1 TEXT NOT NULL,
                v2 TEXT NOT NULL,
                v3 TEXT NOT NULL,
                PRIMARY KEY (mesh_id, cid)
            );
            CREATE TABLE IF NOT EXISTS density_set (
                density_set_id INTEGER PRIMARY KEY AUTOINCREMENT,
                mesh_id INTEGER NOT NULL REFERENCES mesh(mesh_id),
                label TEXT,
                complete INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS density_value (
                density_set_id INTEGER NOT NULL REFERENCES density_set(density_set_id),
                cid TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (density_set_id, cid)
            );
            CREATE TABLE IF NOT EXISTS run (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_mesh_id INTEGER NOT NULL REFERENCES mesh(mesh_id),
                density_set_id INTEGER NOT NULL REFERENCES density_set(density_set_id),
                target_mesh_id INTEGER NOT NULL REFERENCES mesh(mesh_id),
                source_fingerprint TEXT NOT NULL,
                target_fingerprint TEXT NOT NULL,
                published INTEGER NOT NULL,
                label TEXT,
                created_at TEXT NOT NULL,
                missing_source_cells INTEGER NOT NULL,
                incomplete_targets INTEGER NOT NULL,
                zero_area_skipped INTEGER NOT NULL,
                conservation_gap TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_target (
                run_id INTEGER NOT NULL REFERENCES run(run_id),
                cid TEXT NOT NULL,
                area TEXT NOT NULL,
                density TEXT,
                source_area_sum TEXT NOT NULL,
                missing_sources INTEGER NOT NULL,
                complete INTEGER NOT NULL,
                PRIMARY KEY (run_id, cid)
            );
            CREATE TABLE IF NOT EXISTS contribution (
                run_id INTEGER NOT NULL REFERENCES run(run_id),
                target_cid TEXT NOT NULL,
                source_cid TEXT NOT NULL,
                overlap_area TEXT NOT NULL,
                source_density TEXT NOT NULL,
                weighted TEXT NOT NULL,
                PRIMARY KEY (run_id, target_cid, source_cid)
            );
            CREATE INDEX IF NOT EXISTS idx_contrib_run_target
                ON contribution(run_id, target_cid);
            CREATE INDEX IF NOT EXISTS idx_contrib_run_source
                ON contribution(run_id, source_cid);
            """
        )

    # -- mesh registration -------------------------------------------------

    def register_mesh(self, mesh, *, role: str = "source", label: str | None = None) -> int:
        """Validate and persist an external mesh snapshot.  Returns ``mesh_id``."""
        if hasattr(mesh, "export"):
            try:
                snap = mesh.export()
            except TypeError:
                snap = mesh.export(None)
        else:
            snap = coerce_snapshot(mesh)
        if snap.domain is None:
            raise FieldError("mesh snapshot needs a domain")
        dom = tuple(to_fraction(v) for v in snap.domain)
        try:
            validate_mesh(
                snap.vertices,
                snap.cells,
                dom,
                area_tolerance=snap.policy.area_tolerance,
                coverage_tolerance=snap.policy.coverage_tolerance,
            )
        except GeometryError as exc:
            raise MeshValidationError(str(exc)) from exc
        fp = geometry_fingerprint(snap.vertices, snap.cells, dom)
        policy = snap.policy
        vertices, cells = _mesh_rows(snap)
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "INSERT INTO mesh(role, fingerprint, domain, policy, label, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    role,
                    fp,
                    json.dumps([fraction_text(v) for v in dom]),
                    json.dumps(
                        {
                            "orientation": policy.orientation,
                            "area_tolerance": policy.area_tolerance,
                            "coverage_tolerance": policy.coverage_tolerance,
                            "conservation_tolerance": policy.conservation_tolerance,
                        }
                    ),
                    label,
                    _now(),
                ),
            )
            mid = cur.lastrowid
            cur.executemany(
                "INSERT INTO mesh_vertex(mesh_id, vid, x, y) VALUES(?,?,?,?)",
                [(mid, *row) for row in vertices],
            )
            cur.executemany(
                "INSERT INTO mesh_cell(mesh_id, cid, v1, v2, v3) VALUES(?,?,?,?,?)",
                [(mid, *row) for row in cells],
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return mid

    def mesh_snapshot(self, mesh_id: int) -> MeshSnapshot:
        row = self._conn.execute(
            "SELECT domain, policy FROM mesh WHERE mesh_id=?", (mesh_id,)
        ).fetchone()
        if row is None:
            raise FieldError(f"mesh {mesh_id} does not exist")
        domain = tuple(fraction_from_text(s) for s in json.loads(row[0]))
        policy = GeometryPolicy(**json.loads(row[1]))
        vertices = {}
        for vid, x, y in self._conn.execute(
            "SELECT vid, x, y FROM mesh_vertex WHERE mesh_id=?", (mesh_id,)
        ):
            vertices[id_from_token(vid)] = (fraction_from_text(x), fraction_from_text(y))
        cells = {}
        for cid, a, b, c in self._conn.execute(
            "SELECT cid, v1, v2, v3 FROM mesh_cell WHERE mesh_id=?", (mesh_id,)
        ):
            cells[id_from_token(cid)] = (
                id_from_token(a),
                id_from_token(b),
                id_from_token(c),
            )
        return MeshSnapshot(vertices=vertices, cells=cells, domain=domain, policy=policy)

    def mesh_fingerprint(self, mesh_id: int) -> str:
        row = self._conn.execute(
            "SELECT fingerprint FROM mesh WHERE mesh_id=?", (mesh_id,)
        ).fetchone()
        if row is None:
            raise FieldError(f"mesh {mesh_id} does not exist")
        return row[0]

    def mesh_info(self, mesh_id: int) -> dict:
        row = self._conn.execute(
            "SELECT role, fingerprint, domain, label, created_at FROM mesh WHERE mesh_id=?",
            (mesh_id,),
        ).fetchone()
        if row is None:
            raise FieldError(f"mesh {mesh_id} does not exist")
        snap = self.mesh_snapshot(mesh_id)
        rep = validate_mesh(snap.vertices, snap.cells, snap.domain)
        return {
            "mesh_id": mesh_id,
            "role": row[0],
            "fingerprint": row[1],
            "domain": tuple(fraction_from_text(s) for s in json.loads(row[2])),
            "label": row[3],
            "created_at": row[4],
            "cell_count": len(snap.cells),
            "vertex_count": len(snap.vertices),
            "area_total": rep["area_total"],
        }

    # -- densities ---------------------------------------------------------

    def add_density(
        self,
        mesh_id: int,
        values: dict,
        *,
        label: str | None = None,
        complete: bool | None = None,
    ) -> int:
        """Store exactly one finite value per provided source cell.

        ``values`` maps cell id -> finite number (positive or negative).  When
        ``complete`` is true the mapping must cover exactly the mesh's cells.
        When false, a partial set is allowed and the set is marked incomplete;
        an incomplete set can be inspected but never fully published.
        """
        snap = self.mesh_snapshot(mesh_id)
        cell_ids = set(snap.cells.keys())
        by_token = {id_token(cid): cid for cid in cell_ids}
        seen = {}
        for cid, val in values.items():
            if cid not in cell_ids:
                # Accept a JSON-encoded token too (ids that are plain strings
                # compare equal, so this mainly guards int/str ambiguity).
                tok = id_token(cid)
                if tok not in by_token:
                    raise FieldError(f"density references unknown cell {cid!r}")
                cid = by_token[tok]
            if id_token(cid) in seen:
                raise FieldError(f"duplicate density for cell {cid!r}")
            seen[id_token(cid)] = _finite(val)
        if complete is None:
            complete = len(seen) == len(cell_ids)
        if complete and set(seen) != {id_token(c) for c in cell_ids}:
            missing = [c for c in cell_ids if id_token(c) not in seen]
            raise IncompleteFieldError(
                f"complete density set is missing {len(missing)} cells, e.g. {missing[:5]}"
            )
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "INSERT INTO density_set(mesh_id, label, complete, created_at) "
                "VALUES(?,?,?,?)",
                (mesh_id, label, 1 if complete else 0, _now()),
            )
            did = cur.lastrowid
            cur.executemany(
                "INSERT INTO density_value(density_set_id, cid, value) VALUES(?,?,?)",
                [(did, tok, fraction_text(v)) for tok, v in seen.items()],
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return did

    def density_set_info(self, density_set_id: int) -> dict:
        row = self._conn.execute(
            "SELECT mesh_id, label, complete, created_at FROM density_set WHERE density_set_id=?",
            (density_set_id,),
        ).fetchone()
        if row is None:
            raise FieldError(f"density set {density_set_id} does not exist")
        values = {
            id_from_token(cid): fraction_from_text(v)
            for cid, v in self._conn.execute(
                "SELECT cid, value FROM density_value WHERE density_set_id=?",
                (density_set_id,),
            )
        }
        snap = self.mesh_snapshot(row[0])
        missing = [cid for cid in snap.cells if cid not in values]
        return {
            "density_set_id": density_set_id,
            "mesh_id": row[0],
            "label": row[1],
            "declared_complete": bool(row[2]),
            "created_at": row[3],
            "value_count": len(values),
            "missing_cells": missing,
            "complete": not missing and bool(row[2]),
        }

    def density_values(self, density_set_id: int) -> dict:
        return {
            id_from_token(cid): fraction_from_text(v)
            for cid, v in self._conn.execute(
                "SELECT cid, value FROM density_value WHERE density_set_id=?",
                (density_set_id,),
            )
        }

    # -- remap -------------------------------------------------------------

    def remap(
        self,
        source_mesh_id: int,
        density_set_id: int,
        target_mesh_id: int,
        *,
        label: str | None = None,
        allow_incomplete: bool = False,
        publish: bool = True,
    ) -> RunSummary:
        """Build contribution relations and target densities for a new run.

        Every nonzero overlap is stored as its own contribution.  Zero-area
        edge/vertex touching is skipped entirely, so it cannot inject density.
        Missing source values are never treated as zero: an incomplete density
        set aborts the remap unless ``allow_incomplete`` builds a draft run.
        """
        drow = self._conn.execute(
            "SELECT mesh_id, complete FROM density_set WHERE density_set_id=?",
            (density_set_id,),
        ).fetchone()
        if drow is None:
            raise FieldError(f"density set {density_set_id} does not exist")
        if drow[0] != source_mesh_id:
            raise FieldError("density set belongs to a different source mesh")
        src = self.mesh_snapshot(source_mesh_id)
        tgt = self.mesh_snapshot(target_mesh_id)
        if tuple(src.domain) != tuple(tgt.domain):
            raise RemapError(
                f"source domain {tuple(src.domain)} != target domain {tuple(tgt.domain)}"
            )
        values = self.density_values(density_set_id)
        missing = [cid for cid in src.cells if cid not in values]
        if missing and not allow_incomplete:
            raise IncompleteFieldError(
                f"density set {density_set_id} is missing {len(missing)} source cells; "
                "cannot publish a complete remap"
            )
        if missing and publish:
            raise IncompleteFieldError(
                "missing source values cannot be published; pass publish=False for a draft"
            )

        src_rings = {
            cid: [
                (
                    to_fraction(src.vertices[vid][0]),
                    to_fraction(src.vertices[vid][1]),
                )
                for vid in tri
            ]
            for cid, tri in src.cells.items()
        }
        tgt_rings = {
            cid: [
                (
                    to_fraction(tgt.vertices[vid][0]),
                    to_fraction(tgt.vertices[vid][1]),
                )
                for vid in tri
            ]
            for cid, tri in tgt.cells.items()
        }
        src_boxes = {
            cid: _bbox(ring) for cid, ring in src_rings.items()
        }
        tgt_area = {cid: polygon_area(ring) for cid, ring in tgt_rings.items()}

        contributions = []
        target_rows = []
        zero_skipped = 0
        incomplete_targets = 0
        for tcid, t_ring in tgt_rings.items():
            t_box = _bbox(t_ring)
            parts = []
            missing_sources = 0
            total_area = Fraction(0)
            for scid, s_ring in src_rings.items():
                if not bbox_overlap(t_box, src_boxes[scid]):
                    continue
                area = intersection_area(t_ring, s_ring)
                if area == 0:
                    # Edge or vertex touching: no density contribution.
                    zero_skipped += 1
                    continue
                total_area += area
                if scid not in values:
                    missing_sources += 1
                    continue
                parts.append((scid, area, values[scid]))
            density = None
            complete = missing_sources == 0
            if complete:
                weighted = sum((a * d for _, a, d in parts), Fraction(0))
                density = weighted / tgt_area[tcid]
            else:
                incomplete_targets += 1
            target_rows.append(
                (
                    tcid,
                    tgt_area[tcid],
                    density,
                    total_area,
                    missing_sources,
                    complete,
                )
            )
            for scid, area, d in parts:
                contributions.append(
                    (tcid, scid, area, d, area * d)
                )
            # Detect a target that is not fully covered by the source mesh.
            if abs(total_area - tgt_area[tcid]) > to_fraction(tgt.policy.coverage_tolerance):
                raise RemapError(
                    f"target cell {tcid!r} overlaps source union by {float(total_area)} "
                    f"of its area {float(tgt_area[tcid])}"
                )

        source_integral = sum(
            (polygon_area(src_rings[cid]) * d for cid, d in values.items()),
            Fraction(0),
        )
        if incomplete_targets:
            target_integral = Fraction(0)
            gap = Fraction(0)
        else:
            target_integral = sum(
                (
                    area * density
                    for _, area, density, _, _, _ in target_rows
                    if density is not None
                ),
                Fraction(0),
            )
            gap = target_integral - source_integral
            tol = src.policy.conservation_tolerance
            scale = max(abs(source_integral), abs(target_integral), Fraction(1))
            if abs(gap) > to_fraction(tol) * scale:
                raise RemapError(
                    f"conservation failed: source {float(source_integral)} vs "
                    f"target {float(target_integral)} (gap {float(gap)})"
                )
        src_fp = self.mesh_fingerprint(source_mesh_id)
        tgt_fp = self.mesh_fingerprint(target_mesh_id)
        published = 1 if (publish and not missing and not incomplete_targets) else 0
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "INSERT INTO run(source_mesh_id, density_set_id, target_mesh_id, "
                "source_fingerprint, target_fingerprint, published, label, created_at, "
                "missing_source_cells, incomplete_targets, zero_area_skipped, conservation_gap) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_mesh_id,
                    density_set_id,
                    target_mesh_id,
                    src_fp,
                    tgt_fp,
                    published,
                    label,
                    _now(),
                    len(missing),
                    incomplete_targets,
                    zero_skipped,
                    fraction_text(gap),
                ),
            )
            run_id = cur.lastrowid
            cur.executemany(
                "INSERT INTO run_target(run_id, cid, area, density, source_area_sum, "
                "missing_sources, complete) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        id_token(r[0]),
                        fraction_text(r[1]),
                        None if r[2] is None else fraction_text(r[2]),
                        fraction_text(r[3]),
                        r[4],
                        1 if r[5] else 0,
                    )
                    for r in target_rows
                ],
            )
            cur.executemany(
                "INSERT INTO contribution(run_id, target_cid, source_cid, overlap_area, "
                "source_density, weighted) VALUES(?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        id_token(t),
                        id_token(s),
                        fraction_text(a),
                        fraction_text(d),
                        fraction_text(w),
                    )
                    for t, s, a, d, w in contributions
                ],
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return self.run_summary(run_id)

    def publish_run(self, run_id: int) -> RunSummary:
        """Publish a draft run once every target has complete source coverage."""
        row = self._conn.execute(
            "SELECT published, incomplete_targets, missing_source_cells "
            "FROM run WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise FieldError(f"run {run_id} does not exist")
        if row[0]:
            return self.run_summary(run_id)
        if row[1] or row[2]:
            raise IncompleteFieldError(
                f"run {run_id} has {row[1]} incomplete targets and "
                f"{row[2]} missing source values; create a new run from a "
                "complete density set and current meshes instead"
            )
        with self._conn:
            self._conn.execute("UPDATE run SET published=1 WHERE run_id=?", (run_id,))
        return self.run_summary(run_id)

    # -- run access --------------------------------------------------------

    def run_summary(self, run_id: int) -> RunSummary:
        row = self._conn.execute(
            "SELECT source_mesh_id, density_set_id, target_mesh_id, source_fingerprint, "
            "target_fingerprint, published, label, created_at, missing_source_cells, "
            "incomplete_targets, zero_area_skipped, conservation_gap "
            "FROM run WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise FieldError(f"run {run_id} does not exist")
        src_area = source_integral = target_integral = Fraction(0)
        src = self.mesh_snapshot(row[0])
        tgt = self.mesh_snapshot(row[2])
        src_area = sum((_cell_area(src, cid) for cid in src.cells), Fraction(0))
        target_area = sum((_cell_area(tgt, cid) for cid in tgt.cells), Fraction(0))
        if row[8] == 0 and row[9] == 0:
            values = self.density_values(row[1])
            source_integral = sum(
                (_cell_area(src, cid) * values[cid] for cid in src.cells), Fraction(0)
            )
            target_integral = sum(
                (_cell_area(tgt, cid) * d for cid, d in self.target_densities(run_id).items()),
                Fraction(0),
            )
        return RunSummary(
            run_id=run_id,
            source_mesh_id=row[0],
            density_set_id=row[1],
            target_mesh_id=row[2],
            source_fingerprint=row[3],
            target_fingerprint=row[4],
            published=bool(row[5]),
            label=row[6],
            created_at=row[7],
            source_area=Fraction(src_area),
            target_area=Fraction(target_area),
            source_integral=Fraction(source_integral),
            target_integral=Fraction(target_integral),
            conservation_gap=fraction_from_text(row[11]),
            missing_source_cells=row[8],
            incomplete_targets=row[9],
            zero_area_skipped=row[10],
        )

    def target_densities(self, run_id: int) -> dict:
        """Return ``{target_cell_id: Fraction}`` for a complete run."""
        out = {}
        for cid, density, complete in self._conn.execute(
            "SELECT cid, density, complete FROM run_target WHERE run_id=?", (run_id,)
        ):
            if not complete or density is None:
                raise IncompleteFieldError(
                    f"run {run_id} target {id_from_token(cid)!r} has no complete density"
                )
            out[id_from_token(cid)] = fraction_from_text(density)
        if not out:
            raise FieldError(f"run {run_id} has no target cells")
        return out

    def target_cell(self, run_id: int, cid) -> dict:
        row = self._conn.execute(
            "SELECT area, density, source_area_sum, missing_sources, complete "
            "FROM run_target WHERE run_id=? AND cid=?",
            (run_id, id_token(cid)),
        ).fetchone()
        if row is None:
            raise KeyError(cid)
        return {
            "run_id": run_id,
            "cell_id": cid,
            "area": fraction_from_text(row[0]),
            "density": None if row[1] is None else fraction_from_text(row[1]),
            "source_area_sum": fraction_from_text(row[2]),
            "missing_sources": row[3],
            "complete": bool(row[4]),
        }

    def contributions(self, run_id: int, target_cid=None) -> list:
        """Stored nonzero overlap contributions, optionally for one target."""
        if target_cid is None:
            rows = self._conn.execute(
                "SELECT target_cid, source_cid, overlap_area, source_density, weighted "
                "FROM contribution WHERE run_id=? ORDER BY target_cid, source_cid",
                (run_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT target_cid, source_cid, overlap_area, source_density, weighted "
                "FROM contribution WHERE run_id=? AND target_cid=? "
                "ORDER BY source_cid",
                (run_id, id_token(target_cid)),
            ).fetchall()
        return [
            {
                "target_cell_id": id_from_token(t),
                "source_cell_id": id_from_token(s),
                "overlap_area": fraction_from_text(a),
                "source_density": fraction_from_text(d),
                "weighted": fraction_from_text(w),
            }
            for t, s, a, d, w in rows
        ]

    def trace(self, run_id: int, target_cid) -> dict:
        """Full source contribution trace for one target cell of a run."""
        row = self._conn.execute(
            "SELECT source_mesh_id, density_set_id, target_mesh_id, source_fingerprint, "
            "target_fingerprint, published, label FROM run WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise FieldError(f"run {run_id} does not exist")
        cell = self.target_cell(run_id, target_cid)
        contribs = self.contributions(run_id, target_cid)
        return {
            "run_id": run_id,
            "label": row[6],
            "published": bool(row[5]),
            "source_mesh_id": row[0],
            "density_set_id": row[1],
            "target_mesh_id": row[2],
            "source_fingerprint": row[3],
            "target_fingerprint": row[4],
            "target": cell,
            "contributions": contribs,
            "source_count": len(contribs),
        }

    def trace_all(self, run_id: int) -> dict:
        out = {}
        for (tok,) in self._conn.execute(
            "SELECT cid FROM run_target WHERE run_id=? ORDER BY cid", (run_id,)
        ):
            cid = id_from_token(tok)
            out[cid] = self.trace(run_id, cid)
        return out

    def is_run_stale(self, run_id: int, source=None, target=None) -> dict:
        """Compare a run's pinned mesh identities with current mesh identities.

        ``source``/``target`` may each be a fingerprint string, a
        ``MeshSnapshot``/mapping, or a ``MeshService`` (its current published
        version is used).  A stale run is exactly the case where geometry
        changed and its stored area matrix must not be reused.
        """
        row = self._conn.execute(
            "SELECT source_fingerprint, target_fingerprint FROM run WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise FieldError(f"run {run_id} does not exist")
        cur_src = self._fingerprint_of(source)
        cur_tgt = self._fingerprint_of(target)
        src_stale = cur_src is not None and cur_src != row[0]
        tgt_stale = cur_tgt is not None and cur_tgt != row[1]
        return {
            "run_id": run_id,
            "source_stale": src_stale,
            "target_stale": tgt_stale,
            "stale": src_stale or tgt_stale,
            "run_source_fingerprint": row[0],
            "run_target_fingerprint": row[1],
            "current_source_fingerprint": cur_src,
            "current_target_fingerprint": cur_tgt,
        }

    @staticmethod
    def _fingerprint_of(mesh_obj):
        if mesh_obj is None:
            return None
        if isinstance(mesh_obj, str):
            return mesh_obj
        if hasattr(mesh_obj, "export"):
            mesh_obj = mesh_obj.export()
        snap = coerce_snapshot(mesh_obj)
        return geometry_fingerprint(snap.vertices, snap.cells, snap.domain)

    # -- integration -------------------------------------------------------

    def integrate(self, run_id: int, rect) -> IntegrationResult:
        """Integrate a published run over a rectangular subregion.

        Each target triangle contributes ``density * area(triangle ∩ query)``,
        computed from the actual intersection, and the covered area is returned
        alongside the integral.
        """
        row = self._conn.execute(
            "SELECT published, incomplete_targets FROM run WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise FieldError(f"run {run_id} does not exist")
        if not row[0] or row[1]:
            raise IncompleteFieldError(f"run {run_id} is not a complete published run")
        densities = self.target_densities(run_id)
        mesh_id = self._conn.execute(
            "SELECT target_mesh_id FROM run WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        tgt = self.mesh_snapshot(mesh_id)
        domain = tgt.domain
        clipped, outside = _clip_rect(tuple(to_fraction(v) for v in rect), domain)
        if clipped is None:
            return IntegrationResult(
                integral=Fraction(0),
                covered_area=Fraction(0),
                query=tuple(to_fraction(v) for v in rect),
                clipped_query=None,
                outside_area=(to_fraction(rect[2]) - to_fraction(rect[0]))
                * (to_fraction(rect[3]) - to_fraction(rect[1])),
                cell_count=0,
            )
        q_ring = _rect_ring(clipped)
        integral = Fraction(0)
        covered = Fraction(0)
        n = 0
        for cid, density in densities.items():
            ring = _ring_from_snapshot(tgt, cid)
            a = intersection_area(ring, q_ring)
            if a == 0:
                continue
            integral += density * a
            covered += a
            n += 1
        return IntegrationResult(
            integral=integral,
            covered_area=covered,
            query=tuple(to_fraction(v) for v in rect),
            clipped_query=tuple(to_fraction(v) for v in clipped),
            outside_area=outside,
            cell_count=n,
        )

    def integrate_source(self, density_set_id: int, rect) -> IntegrationResult:
        """Directly integrate the source field over a rectangle (ground truth)."""
        drow = self._conn.execute(
            "SELECT mesh_id FROM density_set WHERE density_set_id=?", (density_set_id,)
        ).fetchone()
        if drow is None:
            raise FieldError(f"density set {density_set_id} does not exist")
        values = self.density_values(density_set_id)
        mesh = self.mesh_snapshot(drow[0])
        missing = [cid for cid in mesh.cells if cid not in values]
        if missing:
            raise IncompleteFieldError(
                f"density set {density_set_id} is missing {len(missing)} cells"
            )
        clipped, outside = _clip_rect(tuple(to_fraction(v) for v in rect), mesh.domain)
        if clipped is None:
            return IntegrationResult(
                integral=Fraction(0),
                covered_area=Fraction(0),
                query=tuple(to_fraction(v) for v in rect),
                clipped_query=None,
                outside_area=_rect_area(rect),
                cell_count=0,
            )
        q_ring = _rect_ring(clipped)
        integral = Fraction(0)
        covered = Fraction(0)
        n = 0
        for cid, density in values.items():
            ring = _ring_from_snapshot(mesh, cid)
            a = intersection_area(ring, q_ring)
            if a == 0:
                continue
            integral += density * a
            covered += a
            n += 1
        return IntegrationResult(
            integral=integral,
            covered_area=covered,
            query=tuple(to_fraction(v) for v in rect),
            clipped_query=tuple(to_fraction(v) for v in clipped),
            outside_area=outside,
            cell_count=n,
        )

    # -- introspection -----------------------------------------------------

    def runs(self) -> list:
        rows = self._conn.execute(
            "SELECT run_id FROM run ORDER BY run_id"
        ).fetchall()
        return [self.run_summary(r[0]) for r in rows]

    def run_exists(self, run_id: int) -> bool:
        return (
            self._conn.execute("SELECT 1 FROM run WHERE run_id=?", (run_id,)).fetchone()
            is not None
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _bbox(ring) -> tuple:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _cell_area(snapshot: MeshSnapshot, cid) -> Fraction:
    return polygon_area(_ring_from_snapshot(snapshot, cid))


def _rect_area(rect) -> Fraction:
    x0, y0, x1, y1 = (to_fraction(v) for v in rect)
    return (x1 - x0) * (y1 - y0) if x1 > x0 and y1 > y0 else Fraction(0)
