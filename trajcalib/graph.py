"""Validated calibration graph service.

Frames are coordinate systems with a unique ``frame_id``. An edge
``(src -> dst)`` stores a rigid transform ``x_dst = R @ x_src + t``.

Mutation is transactional: every batch is validated as a whole (matrix
validity, finite values, and path consistency of every affected connected
component).  If anything is invalid the graph is left untouched.  Committed
graphs are versioned and snapshotted in SQLite when a database path is given.

Publication takes a world-root frame and emits, for every *reachable* camera,
the camera-to-world transform derived from the graph.  Cameras in other
components are listed as unreachable; they are never given a fake identity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from .geometry import (
    GeometryError,
    RigidTransform,
    transform_close,
    transform_diff,
    validate_transform,
)

DEFAULT_TOL = 1e-9


class GraphError(RuntimeError):
    """Raised when a graph mutation or query is rejected."""


@dataclass
class CameraIntrinsics:
    fx: float
    fy: float
    cx: float
    cy: float

    def __post_init__(self) -> None:
        vals = [self.fx, self.fy, self.cx, self.cy]
        if not all(np.isfinite(v) for v in vals):
            raise GraphError("camera intrinsics must be finite")
        if self.fx <= 0 or self.fy <= 0:
            raise GraphError("fx and fy must be positive")

    def as_dict(self) -> dict:
        return {"fx": self.fx, "fy": self.fy, "cx": self.cx, "cy": self.cy}


@dataclass
class Frame:
    frame_id: str
    intrinsics: CameraIntrinsics | None = None

    @property
    def is_camera(self) -> bool:
        return self.intrinsics is not None


@dataclass
class Edge:
    edge_id: str
    src: str
    dst: str
    transform: RigidTransform

    def as_dict(self) -> dict:
        return {"edge_id": self.edge_id, "src": self.src, "dst": self.dst, **self.transform.as_dict()}


@dataclass
class GraphState:
    frames: dict[str, Frame] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)
    version: int = 0
    history: list[dict] = field(default_factory=list)

    def to_serializable(self) -> dict:
        return {
            "version": self.version,
            "frames": {
                fid: {
                    "frame_id": f.frame_id,
                    "intrinsics": f.intrinsics.as_dict() if f.intrinsics else None,
                }
                for fid, f in sorted(self.frames.items())
            },
            "edges": {eid: e.as_dict() for eid, e in sorted(self.edges.items())},
        }

    @staticmethod
    def from_serializable(obj: dict) -> "GraphState":
        st = GraphState(version=int(obj.get("version", 0)))
        for fid, f in obj.get("frames", {}).items():
            intr = CameraIntrinsics(**f["intrinsics"]) if f.get("intrinsics") else None
            st.frames[fid] = Frame(fid, intr)
        for eid, e in obj.get("edges", {}).items():
            st.edges[eid] = Edge(eid, e["src"], e["dst"], RigidTransform.from_dict(e))
        return st

    def copy_without_history(self) -> "GraphState":
        return GraphState(
            frames=dict(self.frames),
            edges=dict(self.edges),
            version=self.version,
        )


def _sha256(obj) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CalibrationProduct:
    product_id: str
    graph_version: int
    world_root: str
    tolerance: float
    cam_to_world: dict[str, RigidTransform]
    intrinsics: dict[str, CameraIntrinsics]
    reachable_frames: list[str]
    unreachable_cameras: dict[str, str]
    edges_used: list[str]
    created_at: float
    integrity: str = ""

    def payload(self) -> dict:
        return {
            "product_id": self.product_id,
            "graph_version": self.graph_version,
            "world_root": self.world_root,
            "tolerance": self.tolerance,
            "cam_to_world": {k: v.as_dict() for k, v in sorted(self.cam_to_world.items())},
            "intrinsics": {k: v.as_dict() for k, v in sorted(self.intrinsics.items())},
            "reachable_frames": sorted(self.reachable_frames),
            "unreachable_cameras": dict(sorted(self.unreachable_cameras.items())),
            "edges_used": sorted(self.edges_used),
            "created_at": self.created_at,
        }

    def sign(self) -> "CalibrationProduct":
        self.integrity = _sha256(self.payload())
        return self

    def as_dict(self) -> dict:
        d = self.payload()
        d["integrity"] = self.integrity
        return d

    def verify(self) -> bool:
        return self.integrity == _sha256(self.payload())

    @staticmethod
    def from_dict(obj: dict) -> "CalibrationProduct":
        p = CalibrationProduct(
            product_id=obj["product_id"],
            graph_version=int(obj["graph_version"]),
            world_root=obj["world_root"],
            tolerance=float(obj["tolerance"]),
            cam_to_world={k: RigidTransform.from_dict(v) for k, v in obj["cam_to_world"].items()},
            intrinsics={k: CameraIntrinsics(**v) for k, v in obj["intrinsics"].items()},
            reachable_frames=list(obj["reachable_frames"]),
            unreachable_cameras=dict(obj["unreachable_cameras"]),
            edges_used=list(obj["edges_used"]),
            created_at=float(obj["created_at"]),
            integrity=obj.get("integrity", ""),
        )
        return p


@dataclass
class ConsistencyReport:
    ok: bool
    components: dict[str, list[str]]
    root_maps: dict[str, dict[str, RigidTransform]]
    conflicts: list[dict]


class CalibrationGraphService:
    """In-memory graph with atomic validation and optional SQLite snapshots."""

    def __init__(self, tolerance: float = DEFAULT_TOL, db_path: str | None = None) -> None:
        if not np.isfinite(tolerance) or tolerance <= 0:
            raise GraphError("tolerance must be finite and positive")
        self.tolerance = float(tolerance)
        self.db_path = db_path
        self.state = GraphState()
        self._db: sqlite3.Connection | None = None
        if db_path:
            self._open_db()
            self._load_latest()
            if not self.state.frames:
                self._persist("init")

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _open_db(self) -> None:
        self._db = sqlite3.connect(self.db_path)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS graph_versions (
                   version INTEGER PRIMARY KEY,
                   created_at REAL NOT NULL,
                   note TEXT NOT NULL,
                   integrity TEXT NOT NULL,
                   state_json TEXT NOT NULL
               )"""
        )
        self._db.commit()

    def _load_latest(self) -> None:
        assert self._db is not None
        row = self._db.execute(
            "SELECT state_json FROM graph_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row:
            self.state = GraphState.from_serializable(json.loads(row[0]))

    def _persist(self, note: str) -> None:
        if not self._db:
            return
        payload = self.state.to_serializable()
        self._db.execute(
            "INSERT INTO graph_versions (version, created_at, note, integrity, state_json) VALUES (?,?,?,?,?)",
            (
                self.state.version,
                time.time(),
                note,
                _sha256(payload),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # validation / consistency
    #
    # Each node n gets A[n] with x_root = A[n] @ x_n.  An edge f->t with E
    # therefore satisfies A[f] == A[t] ∘ E.
    # ------------------------------------------------------------------
    def _check_components(self, frames: dict[str, Frame], edges: dict[str, Edge]) -> ConsistencyReport:
        tol = self.tolerance
        adjacency: dict[str, list[tuple[str, Edge, bool]]] = {fid: [] for fid in frames}
        for e in edges.values():
            # forward direction: e.dst = E @ e.src ; inverse: e.src = E^-1 @ e.dst
            adjacency[e.src].append((e.dst, e, True))
            adjacency[e.dst].append((e.src, e, False))

        seen: set[str] = set()
        components: dict[str, list[str]] = {}
        root_maps: dict[str, dict[str, RigidTransform]] = {}
        conflicts: list[dict] = []

        for start in sorted(frames):
            if start in seen:
                continue
            A: dict[str, RigidTransform] = {start: RigidTransform.identity()}
            queue = [start]
            seen.add(start)
            comp: list[str] = []
            while queue:
                node = queue.pop(0)
                comp.append(node)
                for nbr, edge, forward in adjacency[node]:
                    if forward:
                        # node is edge.src, nbr is edge.dst:
                        # A[node] == A[nbr] ∘ E  =>  A[nbr] = A[node] ∘ E^-1
                        cand = A[node].compose(edge.transform.inverse())
                    else:
                        # node is edge.dst, nbr is edge.src:
                        # A[nbr] == A[node] ∘ E
                        cand = A[node].compose(edge.transform)
                    if nbr in A:
                        if not transform_close(A[nbr], cand, tol):
                            conflicts.append(
                                {
                                    "edge_id": edge.edge_id,
                                    "via_node": node,
                                    "node": nbr,
                                    "diff": transform_diff(A[nbr], cand),
                                }
                            )
                    else:
                        A[nbr] = cand
                        seen.add(nbr)
                        queue.append(nbr)
            component_id = min(comp)
            components[component_id] = sorted(comp)
            root_maps[component_id] = A

        return ConsistencyReport(not conflicts, components, root_maps, conflicts)

    def validate(self) -> ConsistencyReport:
        return self._check_components(self.state.frames, self.state.edges)

    def _validate_proposal(self, frames: dict[str, Frame], edges: dict[str, Edge]) -> None:
        for e in edges.values():
            try:
                validate_transform(e.transform, self.tolerance, what=f"edge {e.edge_id}")
            except GeometryError as exc:
                raise GraphError(str(exc)) from exc
            if e.src not in frames or e.dst not in frames:
                raise GraphError(f"edge {e.edge_id} references an unknown frame")
        report = self._check_components(frames, edges)
        if not report.ok:
            c = report.conflicts[0]
            raise GraphError(
                "inconsistent cycle rejected: edge "
                f"{c['edge_id']} gives {c['node']} two transforms "
                f"(rotation {c['diff']['rotation_deg']:.3e} deg, "
                f"translation {c['diff']['translation_max_abs']:.3e})"
            )

    def _commit(self, frames: dict[str, Frame], edges: dict[str, Edge], note: str) -> int:
        # Atomicity: validate the complete candidate graph before touching state.
        self._validate_proposal(frames, edges)
        self.state.frames = frames
        self.state.edges = edges
        self.state.version += 1
        self.state.history.append({"version": self.state.version, "note": note, "ts": time.time()})
        if self._db:
            self._persist(note)
        return self.state.version

    # ------------------------------------------------------------------
    # frame / edge mutations
    # ------------------------------------------------------------------
    def add_frame(self, frame_id: str, intrinsics: CameraIntrinsics | None = None) -> int:
        if not frame_id:
            raise GraphError("frame_id must be non-empty")
        if frame_id in self.state.frames:
            raise GraphError(f"duplicate frame_id: {frame_id}")
        frames = dict(self.state.frames)
        frames[frame_id] = Frame(frame_id, intrinsics)
        return self._commit(frames, dict(self.state.edges), f"add_frame {frame_id}")

    def add_frames(self, frames: Sequence[tuple[str, CameraIntrinsics | None]]) -> int:
        new = dict(self.state.frames)
        for fid, intr in frames:
            if not fid or fid in new:
                raise GraphError(f"duplicate or empty frame_id: {fid!r}")
            new[fid] = Frame(fid, intr)
        return self._commit(new, dict(self.state.edges), "add_frames")

    def add_edge(self, edge_id: str, src: str, dst: str, transform: RigidTransform | dict) -> int:
        if not edge_id:
            raise GraphError("edge_id must be non-empty")
        if edge_id in self.state.edges:
            raise GraphError(f"duplicate edge_id: {edge_id}")
        T = transform if isinstance(transform, RigidTransform) else RigidTransform.from_dict(transform)
        edges = dict(self.state.edges)
        edges[edge_id] = Edge(edge_id, src, dst, T)
        return self._commit(dict(self.state.frames), edges, f"add_edge {edge_id}")

    def remove_edge(self, edge_id: str) -> int:
        if edge_id not in self.state.edges:
            raise GraphError(f"unknown edge_id: {edge_id}")
        edges = dict(self.state.edges)
        del edges[edge_id]
        return self._commit(dict(self.state.frames), edges, f"remove_edge {edge_id}")

    def replace_edge(self, edge_id: str, transform: RigidTransform | dict) -> int:
        if edge_id not in self.state.edges:
            raise GraphError(f"unknown edge_id: {edge_id}")
        T = transform if isinstance(transform, RigidTransform) else RigidTransform.from_dict(transform)
        edges = dict(self.state.edges)
        old = edges[edge_id]
        edges[edge_id] = Edge(edge_id, old.src, old.dst, T)
        return self._commit(dict(self.state.frames), edges, f"replace_edge {edge_id}")

    def apply_batch(self, ops: Sequence[dict], note: str = "batch") -> int:
        """Apply add_frame/add_edge/remove_edge/replace_edge atomically."""
        frames = dict(self.state.frames)
        edges = dict(self.state.edges)
        for op in ops:
            kind = op.get("op")
            if kind == "add_frame":
                fid = op["frame_id"]
                if not fid or fid in frames:
                    raise GraphError(f"duplicate or empty frame_id: {fid!r}")
                intr = CameraIntrinsics(**op["intrinsics"]) if op.get("intrinsics") else None
                frames[fid] = Frame(fid, intr)
            elif kind == "add_edge":
                eid = op["edge_id"]
                if not eid or eid in edges:
                    raise GraphError(f"duplicate or empty edge_id: {eid!r}")
                T = RigidTransform.from_dict(op["transform"])
                edges[eid] = Edge(eid, op["src"], op["dst"], T)
            elif kind == "remove_edge":
                if op["edge_id"] not in edges:
                    raise GraphError(f"unknown edge_id: {op['edge_id']}")
                del edges[op["edge_id"]]
            elif kind == "replace_edge":
                eid = op["edge_id"]
                if eid not in edges:
                    raise GraphError(f"unknown edge_id: {eid}")
                old = edges[eid]
                edges[eid] = Edge(eid, old.src, old.dst, RigidTransform.from_dict(op["transform"]))
            else:
                raise GraphError(f"unknown op {kind!r}")
        self._validate_proposal(frames, edges)
        return self._commit(frames, edges, note)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def _root_maps(self) -> tuple[ConsistencyReport, dict[str, tuple[str, RigidTransform]]]:
        report = self.validate()
        if not report.ok:
            # Should be unreachable because commits are validated; still fail loud.
            raise GraphError("graph is inconsistent; refusing query")
        lookup: dict[str, tuple[str, RigidTransform]] = {}
        for comp, A in report.root_maps.items():
            for fid, T in A.items():
                lookup[fid] = (comp, T)
        return report, lookup

    def component_of(self, frame_id: str) -> str:
        _, lookup = self._root_maps()
        if frame_id not in lookup:
            raise GraphError(f"unknown frame: {frame_id}")
        return lookup[frame_id][0]

    def components(self) -> dict[str, list[str]]:
        return self.validate().components

    def transform(self, src: str, dst: str) -> RigidTransform:
        """Transform taking coordinates in *src* into *dst*: x_dst = T @ x_src."""
        _, lookup = self._root_maps()
        if src not in lookup:
            raise GraphError(f"unknown frame: {src}")
        if dst not in lookup:
            raise GraphError(f"unknown frame: {dst}")
        if lookup[src][0] != lookup[dst][0]:
            raise GraphError(f"frames {src} and {dst} are not connected")
        # x_dst = A[dst]^-1 A[src] x_src
        return lookup[dst][1].inverse().compose(lookup[src][1])

    def frame_to_world(self, frame_id: str, world_root: str) -> RigidTransform:
        return self.transform(frame_id, world_root)

    # ------------------------------------------------------------------
    # publication
    # ------------------------------------------------------------------
    def publish(
        self,
        product_id: str,
        world_root: str,
        created_at: float | None = None,
        require_consistent: bool = True,
    ) -> CalibrationProduct:
        if world_root not in self.state.frames:
            raise GraphError(f"unknown world root frame: {world_root}")
        report, lookup = self._root_maps() if require_consistent else (None, None)
        if report is None:
            report = self.validate()
            lookup = {}
            for comp, A in report.root_maps.items():
                for fid, T in A.items():
                    lookup[fid] = (comp, T)
        if not report.ok:
            raise GraphError("cannot publish inconsistent graph")

        root_comp = lookup[world_root][0]
        reachable = report.components[root_comp]
        cam_to_world: dict[str, RigidTransform] = {}
        intrinsics: dict[str, CameraIntrinsics] = {}
        unreachable: dict[str, str] = {}
        for fid, frame in sorted(self.state.frames.items()):
            if not frame.is_camera:
                continue
            if fid in lookup and lookup[fid][0] == root_comp:
                cam_to_world[fid] = self.transform(fid, world_root)
                intrinsics[fid] = frame.intrinsics  # type: ignore[assignment]
            else:
                unreachable[fid] = "no path to world root"

        # Only edges inside the world component were actually used.
        edges_used = [
            eid
            for eid, e in sorted(self.state.edges.items())
            if e.src in root_comp_set(root_comp, report) and e.dst in root_comp_set(root_comp, report)
        ]
        product = CalibrationProduct(
            product_id=product_id,
            graph_version=self.state.version,
            world_root=world_root,
            tolerance=self.tolerance,
            cam_to_world=cam_to_world,
            intrinsics=intrinsics,
            reachable_frames=reachable,
            unreachable_cameras=unreachable,
            edges_used=edges_used,
            created_at=time.time() if created_at is None else created_at,
        ).sign()
        return product


def root_comp_set(comp_id: str, report: ConsistencyReport) -> set[str]:
    return set(report.components[comp_id])


def save_product(product: CalibrationProduct, path: str) -> None:
    data = product.as_dict()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def load_product(path: str) -> CalibrationProduct:
    with open(path, "r", encoding="utf-8") as fh:
        obj = json.load(fh)
    product = CalibrationProduct.from_dict(obj)
    if not product.verify():
        raise GraphError(f"product integrity check failed for {path}")
    return product


def load_latest_product_from_graph(db_path: str) -> tuple[dict, int]:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT version, state_json FROM graph_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise GraphError("no graph versions stored")
    return json.loads(row[1]), int(row[0])
