"""Weighted multi-view triangulation against infinite rays.

For observations of one ``(track_id, t)`` from several cameras we find the
world point ``X`` minimizing

    sum_i w_i * dist(X, line_i)^2

where ``line_i = (o_i, d_i)`` is the world ray of the measurement. With
``P_i = I - d_i d_i^T`` this is a linear least-squares problem with normal
matrix ``A = sum_i w_i P_i`` and right-hand side ``b = sum_i w_i P_i o_i``.
The point is rejected (kept as a *degenerate* record) when fewer than two
cameras contribute or when ``lambda_min / lambda_max < min_eigen_ratio``;
we never fabricate depth for such cases.

A solved point whose camera-frame depth is non-positive in any contributing
camera is flagged *geometrically inconsistent*. It is retained, not silently
dropped, and the offending observations are named.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import RigidTransform
from .observations import Observation, camera_ray_direction, point_line_distance

DEFAULT_MIN_EIGEN_RATIO = 1e-3


@dataclass
class CameraLine:
    camera_id: str
    obs_id: str
    origin: np.ndarray
    direction: np.ndarray
    weight: float


@dataclass
class PointSolution:
    track_id: str
    t: int
    status: str  # "published" | "degenerate" | "inconsistent"
    X: np.ndarray | None
    n_cameras: int
    rank: int
    eigenvalue_min: float
    eigenvalue_max: float
    eigen_ratio: float
    min_eigen_ratio_threshold: float
    residuals: dict[str, dict]
    cameras: list[str]
    weights: dict[str, float]
    rationale: str

    def as_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "t": self.t,
            "status": self.status,
            "X": None if self.X is None else [float(x) for x in self.X],
            "n_cameras": self.n_cameras,
            "rank": self.rank,
            "eigenvalue_min": float(self.eigenvalue_min),
            "eigenvalue_max": float(self.eigenvalue_max),
            "eigen_ratio": float(self.eigen_ratio),
            "min_eigen_ratio_threshold": float(self.min_eigen_ratio_threshold),
            "residuals": self.residuals,
            "cameras": self.cameras,
            "weights": self.weights,
            "rationale": self.rationale,
        }


def build_camera_lines(
    observations: list[Observation],
    product_cam_to_world: dict[str, RigidTransform],
    product_intrinsics: dict[str, dict],
) -> list[CameraLine]:
    lines: list[CameraLine] = []
    for obs in observations:
        if obs.camera_id not in product_cam_to_world:
            raise KeyError(
                f"camera {obs.camera_id} is not in the calibration product "
                "(unreachable or uncalibrated)"
            )
        T = product_cam_to_world[obs.camera_id]
        intr = product_intrinsics[obs.camera_id]
        d_cam = camera_ray_direction(obs.u, obs.v, intr)
        d_world = T.rotated(d_cam)
        d_world = d_world / np.linalg.norm(d_world)
        lines.append(
            CameraLine(
                camera_id=obs.camera_id,
                obs_id=obs.obs_id,
                origin=T.t.astype(np.float64).copy(),
                direction=d_world,
                weight=obs.weight,
            )
        )
    return lines


def triangulate(
    track_id: str,
    t: int,
    lines: list[CameraLine],
    min_eigen_ratio: float = DEFAULT_MIN_EIGEN_RATIO,
    rank_tol: float = 1e-10,
) -> PointSolution:
    camera_ids = sorted(line.camera_id for line in lines)
    weights_by_cam = {line.camera_id: line.weight for line in lines}
    n = len(lines)

    A = np.zeros((3, 3))
    b = np.zeros(3)
    for line in lines:
        d = line.direction
        P = np.eye(3) - np.outer(d, d)
        A += line.weight * P
        b += line.weight * (P @ line.origin)

    eigvals, eigvecs = np.linalg.eigh(A)
    eigvals = np.clip(eigvals, 0.0, None)
    emax = float(eigvals[-1])
    emin = float(eigvals[0])
    denom = emax if emax > 0 else 1.0
    ratio = emin / denom if emax > 0 else 0.0
    rank = int(np.sum(eigvals > rank_tol * denom)) if emax > 0 else 0

    base = dict(
        track_id=track_id,
        t=t,
        n_cameras=n,
        rank=rank,
        eigenvalue_min=emin,
        eigenvalue_max=emax,
        eigen_ratio=ratio,
        min_eigen_ratio_threshold=min_eigen_ratio,
        cameras=camera_ids,
        weights=weights_by_cam,
    )

    if n < 2:
        return PointSolution(
            status="degenerate",
            X=None,
            residuals=_line_residuals(None, lines),
            rationale=f"only {n} camera(s) contribute; at least 2 required",
            **base,
        )

    if emax <= 0 or rank < 3 or ratio < min_eigen_ratio:
        return PointSolution(
            status="degenerate",
            X=None,
            residuals=_line_residuals(None, lines),
            rationale=(
                f"normal matrix rank {rank}/3, eigen ratio {ratio:.3e} "
                f"< threshold {min_eigen_ratio:.3e}; rays do not constrain depth"
            ),
            **base,
        )

    X = np.linalg.solve(A, b)

    # Camera-frame residual diagnostics (line distances here; camera depth and
    # pixel residuals are filled by ``augment_with_camera_frames`` using the
    # actual product poses).
    residuals = _line_residuals(X, lines)
    return PointSolution(
        status="published",
        X=X,
        residuals=residuals,
        rationale="solved; depth and pixel checks filled from product poses",
        **base,
    )


def _line_residuals(X: np.ndarray | None, lines: list[CameraLine]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in lines:
        if X is None:
            out[line.camera_id] = {
                "obs_id": line.obs_id,
                "line_distance": float("nan"),
                "weight": line.weight,
                "z_positive": None,
                "camera_z": float("nan"),
            }
        else:
            # z in camera frame needs the camera pose; supplied by caller for
            # publication through ``augment_residuals``. Here z_positive is a
            # geometric placeholder set by augment_residuals.
            out[line.camera_id] = {
                "obs_id": line.obs_id,
                "line_distance": point_line_distance(X, line.origin, line.direction),
                "weight": line.weight,
                "z_positive": None,
                "camera_z": float("nan"),
            }
    return out


def augment_with_camera_frames(
    solution: PointSolution,
    lines: list[CameraLine],
    cam_to_world: dict[str, RigidTransform],
) -> PointSolution:
    """Fill camera-frame depth / behind-camera flags from the actual product poses."""
    if solution.X is None:
        return solution
    X = solution.X
    behind: list[str] = []
    for line in lines:
        T = cam_to_world[line.camera_id]
        q = T.R.T @ (X - T.t)
        info = solution.residuals[line.camera_id]
        info["camera_z"] = float(q[2])
        info["camera_point"] = q.tolist()
        info["z_positive"] = bool(q[2] > 0.0)
        if q[2] <= 0.0:
            behind.append(line.camera_id)
    if behind and solution.status == "published":
        solution.status = "inconsistent"
        solution.rationale = (
            "solution has non-positive depth in camera(s) "
            + ", ".join(sorted(behind))
            + "; retained as geometric inconsistency, observations not removed"
        )
    return solution
