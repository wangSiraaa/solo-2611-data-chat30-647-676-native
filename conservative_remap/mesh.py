"""Planar triangle mesh service with exact validation and persisted versions.

The service owns a fixed rectangular domain.  Every imported mesh is validated
against every structural invariant (positive exact area per cell, complete edge
sharing with no hanging nodes, no positive-area overlap, union exactly equal to
the rectangle, boundary exactly the rectangle).  Meshes are immutable versions
stored in SQLite; splitting an edge creates a *preview* first, and only an
explicit publish writes a new version, so a rejected operation never leaves a
half-built mesh behind.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

from .geometry import (
    GeometryError,
    GeometryPolicy,
    MeshSnapshot,
    MeshValidationError,
    coerce_snapshot,
    fraction_text,
    geometry_fingerprint,
    id_from_token,
    id_token,
    orient_cells_ccw,
    to_fraction,
    validate_mesh,
)


class MeshOperationError(RuntimeError):
    """A requested mesh operation is not permitted on the current state."""


# ---------------------------------------------------------------------------
# Preview / publish results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitPreview:
    """A validated, non-persisted description of an edge split."""

    base_version: int
    edge: tuple
    t: Fraction
    new_vertex_id: object
    new_vertex_xy: tuple
    vertices: dict
    cells: dict
    domain: tuple
    policy: GeometryPolicy
    replaced_cells: tuple
    new_cells: tuple
    lineage: dict
    fingerprint: str
    report: dict

    def to_snapshot(self) -> MeshSnapshot:
        return MeshSnapshot(
            vertices=dict(self.vertices),
            cells=dict(self.cells),
            domain=tuple(self.domain),
            policy=self.policy,
            adjacency=self.report.get("adjacency"),
        )

    def cell_ids(self) -> tuple:
        return tuple(self.cells.keys())


@dataclass(frozen=True)
class SplitResult:
    """Outcome of a published split."""

    version_id: int
    preview: SplitPreview


@dataclass(frozen=True)
class MeshVersionInfo:
    version_id: int
    parent_id: int | None
    kind: str
    fingerprint: str
    published: bool
    note: str | None
    created_at: str
    cell_count: int
    vertex_count: int
    area_total: Fraction
    domain_area: Fraction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_id(candidate, used: set):
    if candidate not in used:
        used.add(candidate)
        return candidate
    n = 2
    while f"{candidate}#{n}" in used:
        n += 1
    candidate = f"{candidate}#{n}"
    used.add(candidate)
    return candidate


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _policy_from_json(text: str) -> GeometryPolicy:
    data = json.loads(text)
    return GeometryPolicy(**data)


def _domain_from_json(text: str) -> tuple:
    return tuple(Fraction(s) for s in json.loads(text))


def _domain_to_json(domain: Iterable) -> str:
    return json.dumps([fraction_text(v) for v in domain])


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MeshService:
    """Independent mesh service: import, adjacency, split-edge, publish."""

    def __init__(self, path: str = ":memory:", *, domain=None, policy: GeometryPolicy | None = None):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.isolation_level = None  # explicit transactions
        self._init_schema()
        if domain is not None:
            self._set_domain(tuple(to_fraction(v) for v in domain))
        if policy is not None:
            self._set_policy(policy)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER REFERENCES version(version_id),
                kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                published INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS version_vertex (
                version_id INTEGER NOT NULL REFERENCES version(version_id),
                vid TEXT NOT NULL,
                x TEXT NOT NULL,
                y TEXT NOT NULL,
                PRIMARY KEY (version_id, vid)
            );
            CREATE TABLE IF NOT EXISTS version_cell (
                version_id INTEGER NOT NULL REFERENCES version(version_id),
                cid TEXT NOT NULL,
                v1 TEXT NOT NULL,
                v2 TEXT NOT NULL,
                v3 TEXT NOT NULL,
                parent_cid TEXT,
                parent_version INTEGER,
                origin_cid TEXT NOT NULL,
                op_version INTEGER NOT NULL,
                PRIMARY KEY (version_id, cid)
            );
            CREATE TABLE IF NOT EXISTS operation_log (
                op_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL REFERENCES version(version_id),
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cell_version ON version_cell(version_id);
            """
        )

    # -- meta --------------------------------------------------------------

    def _get_meta(self, key):
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def _set_meta(self, key, value) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @property
    def domain(self) -> tuple | None:
        text = self._get_meta("domain")
        return None if text is None else _domain_from_json(text)

    @property
    def policy(self) -> GeometryPolicy:
        text = self._get_meta("policy")
        return GeometryPolicy() if text is None else _policy_from_json(text)

    def _set_domain(self, domain: tuple) -> None:
        current = self.domain
        if current is not None and tuple(domain) != current:
            raise MeshOperationError(
                f"service domain is fixed at {current}, got {tuple(domain)}"
            )
        self._set_meta("domain", _domain_to_json(domain))

    def _set_policy(self, policy: GeometryPolicy) -> None:
        self._set_meta(
            "policy",
            json.dumps(
                {
                    "orientation": policy.orientation,
                    "area_tolerance": policy.area_tolerance,
                    "coverage_tolerance": policy.coverage_tolerance,
                    "conservation_tolerance": policy.conservation_tolerance,
                }
            ),
        )

    # -- import ------------------------------------------------------------

    def import_mesh(
        self,
        mesh=None,
        *,
        vertices: dict | None = None,
        cells: dict | None = None,
        domain=None,
        policy: GeometryPolicy | None = None,
        note: str | None = None,
        publish: bool = True,
    ) -> int:
        """Validate and persist a new mesh version.

        Accepts a ``MeshSnapshot``/mapping via ``mesh`` or explicit
        ``vertices``/``cells``.  The domain is fixed after the first import.
        """
        if mesh is None:
            if vertices is None or cells is None:
                raise TypeError("provide mesh=... or vertices=/cells=")
            snap = MeshSnapshot(
                vertices=dict(vertices),
                cells=dict(cells),
                domain=tuple(domain) if domain is not None else self.domain,
                policy=policy or self.policy,
            )
        else:
            snap = coerce_snapshot(mesh)
        if snap.domain is None:
            raise MeshOperationError("import requires a domain")
        dom = tuple(to_fraction(v) for v in snap.domain)
        self._set_domain(dom)
        pol = policy or snap.policy or self.policy
        vertices = dict(snap.vertices)
        cells = orient_cells_ccw(vertices, snap.cells)
        report = self._validate(vertices, cells, dom, pol)
        fingerprint = geometry_fingerprint(vertices, cells, dom)

        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "INSERT INTO version(parent_id, kind, fingerprint, published, note, created_at) "
                "VALUES(NULL, 'import', ?, ?, ?, ?)",
                (fingerprint, 1 if publish else 0, note, _now()),
            )
            vid = cur.lastrowid
            self._insert_snapshot(
                cur,
                vid,
                vertices,
                cells,
                parent_version=None,
                origins={cid: cid for cid in cells},
                op_version=vid,
            )
            payload = {
                "kind": "import",
                "cell_count": len(cells),
                "vertex_count": len(vertices),
                "area_total": fraction_text(report["area_total"]),
                "fingerprint": fingerprint,
            }
            cur.execute(
                "INSERT INTO operation_log(version_id, kind, payload, created_at) "
                "VALUES(?, 'import', ?, ?)",
                (vid, json.dumps(payload), _now()),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return vid

    def _validate(self, vertices, cells, domain, policy) -> dict:
        try:
            report = validate_mesh(
                vertices,
                cells,
                domain,
                area_tolerance=policy.area_tolerance,
                coverage_tolerance=policy.coverage_tolerance,
            )
        except GeometryError as exc:
            raise MeshValidationError(str(exc)) from exc
        return report

    def _insert_snapshot(
        self,
        cur,
        version_id: int,
        vertices: dict,
        cells: dict,
        *,
        parent_version: int | None,
        origins: dict,
        op_version: int | None,
        lineage: dict | None = None,
    ) -> None:
        rows = []
        for vid, (x, y) in vertices.items():
            rows.append((version_id, id_token(vid), fraction_text(to_fraction(x)), fraction_text(to_fraction(y))))
        cur.executemany(
            "INSERT INTO version_vertex(version_id, vid, x, y) VALUES(?,?,?,?)", rows
        )
        lineage = lineage or {}
        cell_rows = []
        for cid, tri in cells.items():
            meta = lineage.get(cid)
            if meta is None:
                parent_cid = cid
                parent_v = parent_version
            else:
                parent_cid = meta["parent_cid"]
                parent_v = meta["parent_version"]
            origin = origins.get(cid, cid)
            op_v = (meta or {}).get("op_version", op_version if op_version is not None else version_id)
            cell_rows.append(
                (
                    version_id,
                    id_token(cid),
                    id_token(tri[0]),
                    id_token(tri[1]),
                    id_token(tri[2]),
                    None if parent_cid is None else id_token(parent_cid),
                    parent_v,
                    id_token(origin),
                    op_v,
                )
            )
        cur.executemany(
            "INSERT INTO version_cell(version_id, cid, v1, v2, v3, parent_cid, "
            "parent_version, origin_cid, op_version) VALUES(?,?,?,?,?,?,?,?,?)",
            cell_rows,
        )

    # -- version resolution ------------------------------------------------

    def _latest_version(self, published_only: bool = True) -> int:
        sql = "SELECT MAX(version_id) FROM version"
        if published_only:
            sql += " WHERE published=1"
        row = self._conn.execute(sql).fetchone()
        if row is None or row[0] is None:
            raise MeshOperationError("no mesh version has been published")
        return row[0]

    def _resolve_version(self, version_id=None, *, published_only=False) -> int:
        if version_id is None:
            return self._latest_version(published_only=True)
        row = self._conn.execute(
            "SELECT version_id, published FROM version WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise MeshOperationError(f"version {version_id} does not exist")
        if published_only and not row[1]:
            raise MeshOperationError(f"version {version_id} is not published")
        return version_id

    def versions(self) -> list:
        rows = self._conn.execute(
            "SELECT version_id, parent_id, kind, fingerprint, published, note, created_at "
            "FROM version ORDER BY version_id"
        ).fetchall()
        out = []
        for vid, parent, kind, fp, pub, note, created in rows:
            ncell = self._conn.execute(
                "SELECT COUNT(*) FROM version_cell WHERE version_id=?", (vid,)
            ).fetchone()[0]
            nvert = self._conn.execute(
                "SELECT COUNT(*) FROM version_vertex WHERE version_id=?", (vid,)
            ).fetchone()[0]
            rep = self.report(vid)
            out.append(
                MeshVersionInfo(
                    vid,
                    parent,
                    kind,
                    fp,
                    bool(pub),
                    note,
                    created,
                    ncell,
                    nvert,
                    rep["area_total"],
                    rep["domain_area"],
                )
            )
        return out

    def current_version(self) -> int:
        return self._latest_version(published_only=True)

    def publish(self, version_id: int) -> int:
        """Publish an imported draft version."""
        row = self._conn.execute(
            "SELECT kind, published, fingerprint FROM version WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise MeshOperationError(f"version {version_id} does not exist")
        if row[0] != "import":
            raise MeshOperationError("split versions are published by publish_split")
        if row[1]:
            return version_id
        vertices, cells, _ = self._load(version_id)
        self._validate(vertices, cells, self.domain, self.policy)
        with self._conn:
            self._conn.execute(
                "UPDATE version SET published=1 WHERE version_id=?", (version_id,)
            )
        return version_id

    def discard(self, version_id: int) -> None:
        """Delete an unpublished draft import version."""
        row = self._conn.execute(
            "SELECT kind, published FROM version WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise MeshOperationError(f"version {version_id} does not exist")
        if row[1]:
            raise MeshOperationError("a published version is immutable")
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("DELETE FROM version_cell WHERE version_id=?", (version_id,))
            cur.execute("DELETE FROM version_vertex WHERE version_id=?", (version_id,))
            cur.execute("DELETE FROM operation_log WHERE version_id=?", (version_id,))
            cur.execute("DELETE FROM version WHERE version_id=?", (version_id,))
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    # -- read access -------------------------------------------------------

    def _load(self, version_id: int):
        vertices = {}
        for vid_tok, x, y in self._conn.execute(
            "SELECT vid, x, y FROM version_vertex WHERE version_id=?", (version_id,)
        ):
            vertices[id_from_token(vid_tok)] = (Fraction(x), Fraction(y))
        cells = {}
        meta = {}
        for row in self._conn.execute(
            "SELECT cid, v1, v2, v3, parent_cid, parent_version, origin_cid, op_version "
            "FROM version_cell WHERE version_id=?",
            (version_id,),
        ):
            cid = id_from_token(row[0])
            cells[cid] = (
                id_from_token(row[1]),
                id_from_token(row[2]),
                id_from_token(row[3]),
            )
            meta[cid] = {
                "parent_cid": None if row[4] is None else id_from_token(row[4]),
                "parent_version": row[5],
                "origin_cid": id_from_token(row[6]),
                "op_version": row[7],
            }
        return vertices, cells, meta

    def export(self, version_id=None, *, published_only: bool = False) -> MeshSnapshot:
        vid = self._resolve_version(version_id, published_only=published_only)
        vertices, cells, _ = self._load(vid)
        return MeshSnapshot(
            vertices=vertices,
            cells=cells,
            domain=self.domain,
            policy=self.policy,
        )

    def to_snapshot(self, version_id=None) -> MeshSnapshot:
        return self.export(version_id, published_only=bool(version_id is None))

    def get_vertex(self, vid, version_id=None):
        vid_i = self._resolve_version(version_id)
        row = self._conn.execute(
            "SELECT x, y FROM version_vertex WHERE version_id=? AND vid=?",
            (vid_i, id_token(vid)),
        ).fetchone()
        if row is None:
            raise KeyError(vid)
        return (Fraction(row[0]), Fraction(row[1]))

    def get_cell(self, cid, version_id=None) -> tuple:
        vid = self._resolve_version(version_id)
        row = self._conn.execute(
            "SELECT v1, v2, v3 FROM version_cell WHERE version_id=? AND cid=?",
            (vid, id_token(cid)),
        ).fetchone()
        if row is None:
            raise KeyError(cid)
        return tuple(id_from_token(t) for t in row)

    def cell_ids(self, version_id=None) -> tuple:
        vid = self._resolve_version(version_id)
        return tuple(
            id_from_token(r[0])
            for r in self._conn.execute(
                "SELECT cid FROM version_cell WHERE version_id=? ORDER BY cid", (vid,)
            )
        )

    def report(self, version_id=None) -> dict:
        """Validate persisted geometry and return area/exactness statistics."""
        vid = self._resolve_version(version_id)
        vertices, cells, _ = self._load(vid)
        rep = self._validate(vertices, cells, self.domain, self.policy)
        from .geometry import polygon_area

        return {
            "version_id": vid,
            "cell_count": len(cells),
            "vertex_count": len(vertices),
            "area_total": rep["area_total"],
            "domain_area": rep["domain_area"],
            "cell_areas": {
                cid: polygon_area(ring) for cid, ring in rep["cell_rings"].items()
            },
            "boundary_loop": tuple(rep["boundary_loop"]),
            "adjacency": rep["adjacency"],
            "policy": self.policy,
        }

    def cell_areas(self, version_id=None) -> dict:
        vid = self._resolve_version(version_id)
        vertices, cells, _ = self._load(vid)
        rep = self._validate(vertices, cells, self.domain, self.policy)
        from .geometry import polygon_area

        return {cid: polygon_area(ring) for cid, ring in rep["cell_rings"].items()}

    # -- adjacency ---------------------------------------------------------

    def _edge_structure(self, version_id):
        vertices, cells, _ = self._load(version_id)
        owners = {}
        for cid, tri in cells.items():
            for k in range(3):
                u, v = tri[k], tri[(k + 1) % 3]
                key = (u, v) if id_token(u) < id_token(v) else (v, u)
                owners.setdefault(key, []).append(cid)
        return vertices, cells, owners

    def edge_adjacency(self, version_id=None) -> dict:
        """Map cell -> {(u, v): neighbouring cell} for every shared edge.

        Edge tuples use the canonical ``(min_token, max_token)`` order.  Cells
        with no shared edge (a one-cell mesh) get an empty mapping.
        """
        vid = self._resolve_version(version_id)
        _, cells, owners = self._edge_structure(vid)
        adj = {cid: {} for cid in cells}
        for edge, cs in owners.items():
            if len(cs) == 2:
                a, b = cs
                adj[a][edge] = b
                adj[b][edge] = a
        return adj

    def neighbors(self, cid, version_id=None) -> tuple:
        vid = self._resolve_version(version_id)
        adj = self.edge_adjacency(vid)
        if cid not in adj:
            raise KeyError(cid)
        return tuple(adj[cid].values())

    def shared_edges(self, version_id=None) -> list:
        """List ``(u, v, cell_a, cell_b)`` for all interior edges."""
        vid = self._resolve_version(version_id)
        _, _, owners = self._edge_structure(vid)
        return [
            (edge[0], edge[1], cs[0], cs[1])
            for edge, cs in owners.items()
            if len(cs) == 2
        ]

    def boundary_edges(self, version_id=None) -> list:
        """List directed ``(u, v)`` boundary edges in boundary-loop order."""
        vid = self._resolve_version(version_id)
        _, cells, owners = self._edge_structure(vid)
        nxt = {}
        for cid, tri in cells.items():
            for k in range(3):
                u, v = tri[k], tri[(k + 1) % 3]
                key = (u, v) if id_token(u) < id_token(v) else (v, u)
                if len(owners[key]) == 1:
                    nxt[u] = v
        if not nxt:
            return []
        start = next(iter(nxt))
        loop = [start]
        cur = nxt[start]
        while cur != start:
            loop.append(cur)
            if cur not in nxt or len(loop) > len(nxt) + 1:
                raise MeshOperationError("boundary is not a single closed loop")
            cur = nxt[cur]
        return [(loop[i], loop[(i + 1) % len(loop)]) for i in range(len(loop))]

    def find_edge(self, u, v, version_id=None):
        """Return ``(edge, cells)`` for the undirected edge ``{u, v}``."""
        vid = self._resolve_version(version_id)
        _, _, owners = self._edge_structure(vid)
        key = (u, v) if id_token(u) < id_token(v) else (v, u)
        cs = owners.get(key)
        if not cs:
            raise MeshOperationError(f"edge {u!r}-{v!r} is not present in version {vid}")
        return key, list(cs)

    # -- split edge --------------------------------------------------------

    def preview_split(
        self,
        version_id=None,
        edge=None,
        *,
        t=None,
        point=None,
    ) -> SplitPreview:
        """Validate and return a split preview without persisting anything.

        ``edge`` is ``(u, v)``.  ``t`` in ``(0, 1)`` positions the new vertex;
        alternatively ``point=(x, y)`` must lie strictly inside the segment.
        Both cells adjacent to an interior edge are split; a boundary edge has
        one adjacent cell and is refined without changing the outer boundary.
        """
        base = self._resolve_version(version_id, published_only=True)
        if edge is None or len(edge) != 2:
            raise MeshOperationError("edge must be a pair of vertex ids")
        u, v = edge
        if id_token(u) == id_token(v):
            raise MeshOperationError("edge endpoints must be distinct")
        vertices, cells, meta = self._load(base)
        if u not in vertices or v not in vertices:
            raise MeshOperationError(f"edge {u!r}-{v!r} references an unknown vertex")
        # Locate every cell that actually owns this edge (not merely both ids).
        adjacent = []
        for cid, tri in cells.items():
            for k in range(3):
                a, b = tri[k], tri[(k + 1) % 3]
                if {id_token(a), id_token(b)} == {id_token(u), id_token(v)}:
                    adjacent.append(cid)
                    break
        if not adjacent:
            raise MeshOperationError(f"edge {u!r}-{v!r} is not present in version {base}")
        if len(adjacent) > 2:
            raise MeshOperationError("an edge cannot have more than two adjacent cells")

        # New point position.
        px, py = vertices[u]
        qx, qy = vertices[v]
        if point is not None:
            wx, wy = to_fraction(point[0]), to_fraction(point[1])
            dx, dy = qx - px, qy - py
            denom = dx * dx + dy * dy
            t_frac = ((wx - px) * dx + (wy - py) * dy) / denom
            cross = (wx - px) * dy - (wy - py) * dx
            if cross != 0:
                raise MeshOperationError("point is not collinear with the edge")
            if not (0 < t_frac < 1):
                raise MeshOperationError("point must lie strictly inside the edge")
        else:
            t_frac = to_fraction(Fraction(1, 2) if t is None else t)
            if not (0 < t_frac < 1):
                raise MeshOperationError("t must satisfy 0 < t < 1")
            wx = px + t_frac * (qx - px)
            wy = py + t_frac * (qy - py)

        used_vertices = set(vertices.keys())
        new_vid = _fresh_id(f"s{base}_{len(vertices)}", used_vertices)
        new_vertices = dict(vertices)
        new_vertices[new_vid] = (wx, wy)

        used_cells = set(cells.keys())
        new_cells = dict(cells)
        lineage = {}
        replaced = []
        created = []
        for cid in adjacent:
            tri = cells[cid]
            ring = list(tri)
            # Find the directed occurrence of the edge inside the ring.  Rings
            # are stored CCW after import.
            idx = None
            for k in range(3):
                if {id_token(ring[k]), id_token(ring[(k + 1) % 3])} == {
                    id_token(u),
                    id_token(v),
                }:
                    idx = k
                    break
            assert idx is not None
            p = ring[idx]
            q = ring[(idx + 1) % 3]
            r = ring[(idx + 2) % 3]
            del new_cells[cid]
            replaced.append(cid)
            id1 = _fresh_id(f"{cid}/a", used_cells)
            id2 = _fresh_id(f"{cid}/b", used_cells)
            new_cells[id1] = (p, new_vid, r)
            new_cells[id2] = (new_vid, q, r)
            created.extend([id1, id2])
            origin = meta[cid]["origin_cid"]
            for ncid in (id1, id2):
                lineage[ncid] = {
                    "parent_cid": cid,
                    "parent_version": base,
                    "origin_cid": origin,
                    "op_version": None,  # filled on publish
                    "split_edge": (u, v),
                    "new_vertex": new_vid,
                }

        pol = self.policy
        report = self._validate(new_vertices, new_cells, self.domain, pol)
        fingerprint = geometry_fingerprint(new_vertices, new_cells, self.domain)
        return SplitPreview(
            base_version=base,
            edge=(u, v),
            t=t_frac,
            new_vertex_id=new_vid,
            new_vertex_xy=(wx, wy),
            vertices=new_vertices,
            cells=new_cells,
            domain=self.domain,
            policy=pol,
            replaced_cells=tuple(replaced),
            new_cells=tuple(created),
            lineage=lineage,
            fingerprint=fingerprint,
            report=report,
        )

    def publish_split(self, preview: SplitPreview, *, note: str | None = None) -> SplitResult:
        """Atomically publish a previously validated split preview."""
        if not isinstance(preview, SplitPreview):
            raise TypeError("publish_split expects a SplitPreview")
        # The base still exists (versions are immutable), so a stale preview is
        # still geometrically valid; it simply becomes a sibling branch.
        self._resolve_version(preview.base_version, published_only=True)
        report = self._validate(preview.vertices, preview.cells, self.domain, preview.policy)
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "INSERT INTO version(parent_id, kind, fingerprint, published, note, created_at) "
                "VALUES(?, 'split', ?, 1, ?, ?)",
                (preview.base_version, preview.fingerprint, note, _now()),
            )
            vid = cur.lastrowid
            _, old_cells, old_meta = self._load(preview.base_version)
            origins = {cid: old_meta[cid]["origin_cid"] for cid in old_cells}
            for cid in preview.cells:
                if cid not in origins:
                    origins[cid] = preview.lineage[cid]["origin_cid"]
            lineage = {}
            for cid in preview.cells:
                if cid in preview.lineage:
                    entry = dict(preview.lineage[cid])
                    entry["op_version"] = vid
                    lineage[cid] = entry
                else:
                    # Unchanged cell: parent is itself in the base version.
                    lineage[cid] = {
                        "parent_cid": cid,
                        "parent_version": preview.base_version,
                        "origin_cid": origins[cid],
                        "op_version": old_meta[cid]["op_version"],
                    }
            self._insert_snapshot(
                cur,
                vid,
                preview.vertices,
                preview.cells,
                parent_version=preview.base_version,
                origins=origins,
                op_version=vid,
                lineage=lineage,
            )
            payload = {
                "kind": "split",
                "base_version": preview.base_version,
                "edge": [id_token(preview.edge[0]), id_token(preview.edge[1])],
                "t": fraction_text(preview.t),
                "new_vertex": id_token(preview.new_vertex_id),
                "new_vertex_xy": [fraction_text(c) for c in preview.new_vertex_xy],
                "replaced_cells": [id_token(c) for c in preview.replaced_cells],
                "new_cells": [id_token(c) for c in preview.new_cells],
                "area_total": fraction_text(report["area_total"]),
                "fingerprint": preview.fingerprint,
            }
            cur.execute(
                "INSERT INTO operation_log(version_id, kind, payload, created_at) "
                "VALUES(?, 'split', ?, ?)",
                (vid, json.dumps(payload), _now()),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return SplitResult(version_id=vid, preview=preview)

    def split_edge(
        self,
        version_id=None,
        edge=None,
        *,
        t=None,
        point=None,
        publish: bool = True,
        note: str | None = None,
    ):
        """Preview a split and, when ``publish`` is true, publish it atomically.

        A rejected operation raises before any row is written; with
        ``publish=False`` the caller receives the validated preview only.
        """
        preview = self.preview_split(version_id, edge, t=t, point=point)
        if not publish:
            return preview
        return self.publish_split(preview, note=note)

    # -- lineage -----------------------------------------------------------

    def cell_provenance(self, cid, version_id=None) -> dict:
        vid = self._resolve_version(version_id)
        row = self._conn.execute(
            "SELECT parent_cid, parent_version, origin_cid, op_version "
            "FROM version_cell WHERE version_id=? AND cid=?",
            (vid, id_token(cid)),
        ).fetchone()
        if row is None:
            raise KeyError(cid)
        origin = id_from_token(row[2])
        chain = self.trace_chain(cid, vid)
        return {
            "version_id": vid,
            "cell_id": cid,
            "origin_cid": origin,
            "immediate_parent_cid": None if row[0] is None else id_from_token(row[0]),
            "immediate_parent_version": row[1],
            "created_version": row[3],
            "chain": chain,
        }

    def trace_chain(self, cid, version_id=None) -> list:
        """Walk parent pointers back to the originating import cell."""
        vid = self._resolve_version(version_id)
        chain = []
        seen = set()
        cur_v, cur_c = vid, cid
        while True:
            if (cur_v, id_token(cur_c)) in seen:
                raise MeshOperationError("lineage cycle detected")
            seen.add((cur_v, id_token(cur_c)))
            row = self._conn.execute(
                "SELECT parent_cid, parent_version, origin_cid, op_version "
                "FROM version_cell WHERE version_id=? AND cid=?",
                (cur_v, id_token(cur_c)),
            ).fetchone()
            if row is None:
                raise MeshOperationError(
                    f"cell {cur_c!r} not found in version {cur_v}"
                )
            chain.append(
                {
                    "version_id": cur_v,
                    "cell_id": cur_c,
                    "origin_cid": id_from_token(row[2]),
                    "created_version": row[3],
                }
            )
            if row[0] is None or row[1] is None:
                break
            parent_cid = id_from_token(row[0])
            parent_version = row[1]
            if parent_cid == cur_c and parent_version == cur_v:
                break
            cur_v, cur_c = parent_version, parent_cid
        chain.reverse()
        return chain

    def operation_log(self, version_id=None) -> list:
        if version_id is None:
            rows = self._conn.execute(
                "SELECT op_id, version_id, kind, payload, created_at FROM operation_log "
                "ORDER BY op_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT op_id, version_id, kind, payload, created_at FROM operation_log "
                "WHERE version_id=? ORDER BY op_id",
                (self._resolve_version(version_id),),
            ).fetchall()
        return [
            {
                "op_id": r[0],
                "version_id": r[1],
                "kind": r[2],
                "payload": json.loads(r[3]),
                "created_at": r[4],
            }
            for r in rows
        ]
