"""Independent 3D trajectory warehouse.

The warehouse consumes calibration products as **external JSON** files.  It
never opens the calibration graph database or reads graph edges; it only sees
the product's camera-to-world transforms and intrinsics.

Runs are keyed by ``(dataset, product)``.  Replaying the same observations
against a corrected product creates a *new* run while every earlier run keeps
its stored product reference, points, residuals and segments intact.

For each ``(track_id, t)`` the contributing observations are triangulated with
the weighted infinite-line solver. A point is published only when at least two
cameras contribute and the normal-matrix eigen ratio clears the configured
threshold. Degenerate and geometrically inconsistent solutions are stored with
their failure evidence and are never interpolated over.

A *trajectory* is the time-ordered set of points for one track.  Two adjacent
stored points yield an interpolating segment only when both endpoints are
published and their time gap does not exceed ``max_gap``; otherwise the
segment is marked unavailable with the reason.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass

import numpy as np

from .graph import CalibrationProduct, load_product
from .observations import (
    Observation,
    load_observations_jsonl,
    observations_sha256,
    project_world_point,
)
from .solver import (
    DEFAULT_MIN_EIGEN_RATIO,
    augment_with_camera_frames,
    build_camera_lines,
    triangulate,
)


class WarehouseError(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    graph_version INTEGER NOT NULL,
    world_root TEXT NOT NULL,
    integrity TEXT NOT NULL,
    product_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    obs_sha256 TEXT NOT NULL UNIQUE,
    n_observations INTEGER NOT NULL,
    path TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    dataset_id TEXT NOT NULL,
    obs_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    t INTEGER NOT NULL,
    camera_id TEXT NOT NULL,
    u REAL NOT NULL,
    v REAL NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY (dataset_id, obs_id)
);
CREATE INDEX IF NOT EXISTS obs_by_identity ON observations(dataset_id, track_id, t, camera_id);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    min_eigen_ratio REAL NOT NULL,
    max_gap INTEGER NOT NULL,
    n_points INTEGER NOT NULL,
    n_published INTEGER NOT NULL,
    n_degenerate INTEGER NOT NULL,
    n_inconsistent INTEGER NOT NULL,
    n_segments_allowed INTEGER NOT NULL,
    n_segments_blocked INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(dataset_id, product_id, min_eigen_ratio, max_gap)
);
CREATE TABLE IF NOT EXISTS points (
    run_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    t INTEGER NOT NULL,
    status TEXT NOT NULL,
    x REAL, y REAL, z REAL,
    n_cameras INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    eigenvalue_min REAL NOT NULL,
    eigenvalue_max REAL NOT NULL,
    eigen_ratio REAL NOT NULL,
    min_eigen_ratio_threshold REAL NOT NULL,
    rationale TEXT NOT NULL,
    PRIMARY KEY (run_id, track_id, t)
);
CREATE TABLE IF NOT EXISTS point_cameras (
    run_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    t INTEGER NOT NULL,
    camera_id TEXT NOT NULL,
    obs_id TEXT NOT NULL,
    weight REAL NOT NULL,
    line_distance REAL,
    camera_z REAL,
    z_positive INTEGER,
    u REAL NOT NULL,
    v REAL NOT NULL,
    u_hat REAL,
    v_hat REAL,
    du REAL,
    dv REAL,
    pixel_residual REAL,
    PRIMARY KEY (run_id, track_id, t, camera_id)
);
CREATE TABLE IF NOT EXISTS segments (
    run_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    t0 INTEGER NOT NULL,
    t1 INTEGER NOT NULL,
    interpolate_allowed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (run_id, track_id, t0, t1)
);
"""


@dataclass
class RunSummary:
    run_id: str
    dataset_id: str
    product_id: str
    n_points: int
    n_published: int
    n_degenerate: int
    n_inconsistent: int
    n_segments_allowed: int
    n_segments_blocked: int

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class TrajectoryWarehouse:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------
    # ingestion
    # ------------------------------------------------------------------
    def register_product(self, product: CalibrationProduct) -> None:
        if not product.verify():
            raise WarehouseError(f"product {product.product_id} failed integrity check")
        payload = json.dumps(product.as_dict(), sort_keys=True)
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO products VALUES (?,?,?,?,?,?)",
                (
                    product.product_id,
                    product.graph_version,
                    product.world_root,
                    product.integrity,
                    payload,
                    product.created_at,
                ),
            )
            existing = self._db.execute(
                "SELECT integrity, product_json FROM products WHERE product_id=?",
                (product.product_id,),
            ).fetchone()
        if existing["integrity"] != product.integrity or existing["product_json"] != payload:
            raise WarehouseError(
                f"product id {product.product_id} already registered with different content"
            )

    def register_product_file(self, path: str) -> CalibrationProduct:
        product = load_product(path)
        self.register_product(product)
        return product

    def get_product(self, product_id: str) -> CalibrationProduct:
        row = self._db.execute(
            "SELECT product_json FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        if not row:
            raise WarehouseError(f"unknown product {product_id}")
        return CalibrationProduct.from_dict(json.loads(row["product_json"]))

    def ingest_dataset(
        self, observations: list[Observation], path: str | None = None
    ) -> str:
        if not observations:
            raise WarehouseError("empty observation dataset")
        sha = observations_sha256(observations)
        row = self._db.execute(
            "SELECT dataset_id FROM datasets WHERE obs_sha256=?", (sha,)
        ).fetchone()
        if row:
            return row["dataset_id"]
        dataset_id = "ds_" + sha[:16]
        with self._db:
            self._db.execute(
                "INSERT INTO datasets VALUES (?,?,?,?,?)",
                (dataset_id, sha, len(observations), path, time.time()),
            )
            self._db.executemany(
                "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
                [
                    (dataset_id, o.obs_id, o.track_id, o.t, o.camera_id, o.u, o.v, o.weight)
                    for o in observations
                ],
            )
        return dataset_id

    def ingest_jsonl(self, path: str) -> str:
        return self.ingest_dataset(load_observations_jsonl(path), path=path)

    def dataset_observations(self, dataset_id: str) -> list[Observation]:
        rows = self._db.execute(
            "SELECT * FROM observations WHERE dataset_id=? ORDER BY track_id, t, camera_id",
            (dataset_id,),
        ).fetchall()
        return [
            Observation(r["obs_id"], r["track_id"], r["t"], r["camera_id"], r["u"], r["v"], r["weight"])
            for r in rows
        ]

    # ------------------------------------------------------------------
    # run computation
    # ------------------------------------------------------------------
    def run(
        self,
        dataset_id: str,
        product_id: str,
        min_eigen_ratio: float = DEFAULT_MIN_EIGEN_RATIO,
        max_gap: int = 1,
    ) -> RunSummary:
        if max_gap < 0:
            raise WarehouseError("max_gap must be >= 0")
        existing = self._db.execute(
            "SELECT run_id FROM runs WHERE dataset_id=? AND product_id=? AND min_eigen_ratio=? AND max_gap=?",
            (dataset_id, product_id, min_eigen_ratio, max_gap),
        ).fetchone()
        if existing:
            return self.get_run(existing["run_id"])

        product = self.get_product(product_id)
        observations = self.dataset_observations(dataset_id)
        if not observations:
            raise WarehouseError(f"dataset {dataset_id} has no observations")

        # Fail loudly if a camera is missing from the product: unreachable
        # cameras are never silently treated as identity.
        for obs in observations:
            if obs.camera_id not in product.cam_to_world:
                raise WarehouseError(
                    f"observation {obs.obs_id} uses camera {obs.camera_id} "
                    "which is unreachable in the product"
                )

        product_intrinsics = {cid: i.as_dict() for cid, i in product.intrinsics.items()}

        grouped: dict[tuple[str, int], list[Observation]] = {}
        for obs in observations:
            grouped.setdefault((obs.track_id, obs.t), []).append(obs)

        run_id = "run_" + uuid.uuid4().hex[:16]
        point_rows = []
        pc_rows = []
        counts = {"published": 0, "degenerate": 0, "inconsistent": 0}

        for (track_id, t) in sorted(grouped):
            obs_list = grouped[(track_id, t)]
            lines = build_camera_lines(obs_list, product.cam_to_world, product_intrinsics)
            sol = triangulate(track_id, t, lines, min_eigen_ratio=min_eigen_ratio)
            if sol.X is not None:
                sol = augment_with_camera_frames(sol, lines, product.cam_to_world)
            counts[sol.status] += 1

            X = sol.X
            point_rows.append(
                (
                    run_id,
                    track_id,
                    t,
                    sol.status,
                    None if X is None else float(X[0]),
                    None if X is None else float(X[1]),
                    None if X is None else float(X[2]),
                    sol.n_cameras,
                    sol.rank,
                    sol.eigenvalue_min,
                    sol.eigenvalue_max,
                    sol.eigen_ratio,
                    sol.min_eigen_ratio_threshold,
                    sol.rationale,
                )
            )
            for line in lines:
                info = sol.residuals[line.camera_id]
                u_hat = v_hat = du = dv = pres = None
                z_pos = None
                if X is not None:
                    proj = project_world_point(
                        X, product_intrinsics[line.camera_id], product.cam_to_world[line.camera_id]
                    )
                    z_pos = int(proj["in_front"])
                    if proj["in_front"]:
                        u_hat, v_hat = proj["u"], proj["v"]
                        du = float(obs_by_id(obs_list, line.obs_id).u - u_hat)
                        dv = float(obs_by_id(obs_list, line.obs_id).v - v_hat)
                        pres = float(np.hypot(du, dv))
                pc_rows.append(
                    (
                        run_id,
                        track_id,
                        t,
                        line.camera_id,
                        line.obs_id,
                        line.weight,
                        info["line_distance"],
                        info["camera_z"],
                        z_pos,
                        obs_by_id(obs_list, line.obs_id).u,
                        obs_by_id(obs_list, line.obs_id).v,
                        u_hat,
                        v_hat,
                        du,
                        dv,
                        pres,
                    )
                )

        segments = self._build_segments(run_id, point_rows, max_gap)
        n_allowed = sum(1 for s in segments if s[4] == 1)

        with self._db:
            self._db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    dataset_id,
                    product_id,
                    min_eigen_ratio,
                    max_gap,
                    len(point_rows),
                    counts["published"],
                    counts["degenerate"],
                    counts["inconsistent"],
                    n_allowed,
                    len(segments) - n_allowed,
                    time.time(),
                ),
            )
            self._db.executemany(
                "INSERT INTO points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", point_rows
            )
            self._db.executemany(
                "INSERT INTO point_cameras VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pc_rows
            )
            self._db.executemany(
                "INSERT INTO segments VALUES (?,?,?,?,?,?)", segments
            )
        return self.get_run(run_id)

    def _build_segments(self, run_id: str, point_rows: list[tuple], max_gap: int) -> list[tuple]:
        by_track: dict[str, list[tuple[int, str]]] = {}
        for row in point_rows:
            # row: run_id, track_id, t, status, ...
            by_track.setdefault(row[1], []).append((row[2], row[3]))
        out: list[tuple] = []
        for track_id, items in by_track.items():
            items.sort()
            for (t0, s0), (t1, s1) in zip(items, items[1:]):
                if (t1 - t0) > max_gap:
                    allowed, reason = 0, f"time gap {t1 - t0} > max_gap {max_gap}"
                elif s0 != "published":
                    allowed, reason = 0, f"endpoint t={t0} unavailable ({s0})"
                elif s1 != "published":
                    allowed, reason = 0, f"endpoint t={t1} unavailable ({s1})"
                else:
                    allowed, reason = 1, "both endpoints published and gap within limit"
                out.append((run_id, track_id, t0, t1, allowed, reason))
        return out

    # ------------------------------------------------------------------
    # retrieval
    # ------------------------------------------------------------------
    def get_run(self, run_id: str) -> RunSummary:
        r = self._db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not r:
            raise WarehouseError(f"unknown run {run_id}")
        return RunSummary(
            run_id=r["run_id"],
            dataset_id=r["dataset_id"],
            product_id=r["product_id"],
            n_points=r["n_points"],
            n_published=r["n_published"],
            n_degenerate=r["n_degenerate"],
            n_inconsistent=r["n_inconsistent"],
            n_segments_allowed=r["n_segments_allowed"],
            n_segments_blocked=r["n_segments_blocked"],
        )

    def list_runs(self) -> list[dict]:
        rows = self._db.execute("SELECT * FROM runs ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def list_products(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT product_id, graph_version, world_root, integrity, created_at FROM products ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_tracks(self, run_id: str) -> list[dict]:
        rows = self._db.execute(
            """SELECT track_id, COUNT(*) AS n, SUM(status='published') AS n_published,
                      MIN(t) AS t_min, MAX(t) AS t_max
               FROM points WHERE run_id=? GROUP BY track_id ORDER BY track_id""",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_point(self, run_id: str, track_id: str, t: int) -> dict:
        r = self._db.execute(
            "SELECT * FROM points WHERE run_id=? AND track_id=? AND t=?",
            (run_id, track_id, t),
        ).fetchone()
        if not r:
            raise WarehouseError(f"no point ({track_id}, t={t}) in run {run_id}")
        d = dict(r)
        if d["x"] is not None:
            d["X"] = [d.pop("x"), d.pop("y"), d.pop("z")]
        else:
            d.pop("x")
            d.pop("y")
            d.pop("z")
            d["X"] = None
        return d

    def trace_point(self, run_id: str, track_id: str, t: int) -> dict:
        """Full provenance: point -> per-camera observation -> actual product transform."""
        point = self.get_point(run_id, track_id, t)
        product = self.get_product(self.get_run(run_id).product_id)
        rows = self._db.execute(
            "SELECT * FROM point_cameras WHERE run_id=? AND track_id=? AND t=? ORDER BY camera_id",
            (run_id, track_id, t),
        ).fetchall()
        cameras = []
        for r in rows:
            cid = r["camera_id"]
            T = product.cam_to_world[cid]
            cameras.append(
                {
                    "camera_id": cid,
                    "obs_id": r["obs_id"],
                    "observation": {"u": r["u"], "v": r["v"], "weight": r["weight"]},
                    "camera_to_world": T.as_dict(),
                    "intrinsics": product.intrinsics[cid].as_dict(),
                    "line_distance": r["line_distance"],
                    "camera_z": r["camera_z"],
                    "z_positive": None if r["z_positive"] is None else bool(r["z_positive"]),
                    "projected": {"u": r["u_hat"], "v": r["v_hat"]},
                    "pixel_residual": None
                    if r["pixel_residual"] is None
                    else {"du": r["du"], "dv": r["dv"], "norm": r["pixel_residual"]},
                }
            )
        return {
            "run_id": run_id,
            "product_id": product.product_id,
            "world_root": product.world_root,
            "point": point,
            "cameras": cameras,
        }

    def trajectory(self, run_id: str, track_id: str) -> dict:
        points = [
            self.get_point(run_id, track_id, int(r["t"]))
            for r in self._db.execute(
                "SELECT t FROM points WHERE run_id=? AND track_id=? ORDER BY t",
                (run_id, track_id),
            ).fetchall()
        ]
        segs = [
            dict(r)
            for r in self._db.execute(
                "SELECT * FROM segments WHERE run_id=? AND track_id=? ORDER BY t0",
                (run_id, track_id),
            ).fetchall()
        ]
        return {"run_id": run_id, "track_id": track_id, "points": points, "segments": segs}

    def interpolate(
        self, run_id: str, track_id: str, t: float
    ) -> dict:
        """Linear position between adjacent *available* endpoints, or a refusal."""
        data = self.trajectory(run_id, track_id)
        pts = {p["t"]: p for p in data["points"]}
        if not pts:
            return {"available": False, "reason": "track has no points"}
        if float(t).is_integer() and int(t) in pts:
            p = pts[int(t)]
            return {
                "available": p["status"] == "published",
                "exact": True,
                "point": p if p["status"] == "published" else None,
                "reason": "exact sample is not published" if p["status"] != "published" else "exact sample",
            }
        lower = max((k for k in pts if k < t), default=None)
        upper = min((k for k in pts if k > t), default=None)
        if lower is None or upper is None:
            return {"available": False, "reason": "outside the sampled time range"}
        seg = next(
            (s for s in data["segments"] if s["t0"] == lower and s["t1"] == upper), None
        )
        if seg is None or not seg["interpolate_allowed"]:
            return {
                "available": False,
                "reason": None if seg is None else seg["reason"],
                "t0": lower,
                "t1": upper,
            }
        a, b = pts[lower], pts[upper]
        alpha = (t - lower) / (upper - lower)
        X = [
            (1.0 - alpha) * a["X"][i] + alpha * b["X"][i]
            for i in range(3)
        ]
        return {"available": True, "exact": False, "X": X, "t0": lower, "t1": upper, "alpha": alpha}

    def get_observation(self, dataset_id: str, obs_id: str) -> dict:
        r = self._db.execute(
            "SELECT * FROM observations WHERE dataset_id=? AND obs_id=?",
            (dataset_id, obs_id),
        ).fetchone()
        if not r:
            raise WarehouseError(f"unknown observation {obs_id}")
        return dict(r)


def obs_by_id(observations: list[Observation], obs_id: str) -> Observation:
    for o in observations:
        if o.obs_id == obs_id:
            return o
    raise WarehouseError(f"observation {obs_id} not in group")
