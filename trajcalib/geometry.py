"""Rigid-transform algebra and validated calibration graph primitives.

Conventions
-----------
An edge ``from -> to`` stores ``(R, t)`` such that

    x_to = R @ x_from + t

with ``R`` a proper rotation (orthogonal, det = +1) and finite ``t``.
All validation uses one caller-supplied absolute tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

EPS = 1e-15


class GeometryError(ValueError):
    """Base error for malformed geometry."""


@dataclass(frozen=True)
class RigidTransform:
    R: np.ndarray
    t: np.ndarray

    def __post_init__(self) -> None:
        object.__setattr__(self, "R", np.asarray(self.R, dtype=np.float64))
        object.__setattr__(self, "t", np.asarray(self.t, dtype=np.float64).reshape(3))

    # ---- algebra -----------------------------------------------------
    def compose(self, other: "RigidTransform") -> "RigidTransform":
        """Return ``self ∘ other`` (apply *other* first, then *self*)."""
        return RigidTransform(self.R @ other.R, self.R @ other.t + self.t)

    def inverse(self) -> "RigidTransform":
        Rt = self.R.T
        return RigidTransform(Rt, -(Rt @ self.t))

    def apply(self, points: np.ndarray) -> np.ndarray:
        pts = np.asarray(points, dtype=np.float64)
        if pts.shape == (3,):
            return self.R @ pts + self.t
        return pts @ self.R.T + self.t

    def rotated(self, vectors: np.ndarray) -> np.ndarray:
        vec = np.asarray(vectors, dtype=np.float64)
        if vec.shape == (3,):
            return self.R @ vec
        return vec @ self.R.T

    def as_dict(self) -> dict:
        return {"R": self.R.tolist(), "t": self.t.tolist()}

    # ---- representation ---------------------------------------------
    @staticmethod
    def identity() -> "RigidTransform":
        return RigidTransform(np.eye(3), np.zeros(3))

    @staticmethod
    def from_axis_angle(axis: Iterable[float], angle: float, translation: Iterable[float]) -> "RigidTransform":
        axis = np.asarray(list(axis), dtype=np.float64)
        n = np.linalg.norm(axis)
        if not np.isfinite(n) or n <= EPS:
            raise GeometryError("rotation axis must be finite and non-zero")
        k = axis / n
        K = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
        I = np.eye(3)
        R = I + np.sin(angle) * K + (1.0 - np.cos(angle)) * (K @ K)
        return RigidTransform(R, np.asarray(list(translation), dtype=np.float64))

    @staticmethod
    def from_dict(obj) -> "RigidTransform":
        if isinstance(obj, RigidTransform):
            return obj
        if not isinstance(obj, dict):
            raise GeometryError("transform must be an object")
        if "R" not in obj or "t" not in obj:
            raise GeometryError("transform requires R and t")
        R = np.asarray(obj["R"], dtype=np.float64)
        t = np.asarray(obj["t"], dtype=np.float64)
        if R.shape != (3, 3) or t.shape != (3,):
            raise GeometryError(f"bad transform shapes R={R.shape}, t={t.shape}")
        return RigidTransform(R, t)


def rotation_error_deg(a: RigidTransform, b: RigidTransform) -> float:
    """Geodesic rotation difference in degrees (diagnostics only)."""
    R = a.R @ b.R.T
    cos_angle = float(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0))
    return float(np.degrees(np.arccos(cos_angle)))


def validate_transform(T: RigidTransform, tol: float, what: str = "transform") -> None:
    """Validate finiteness, shape, orthogonality and determinant under one tolerance."""
    if not np.isfinite(tol) or tol <= 0:
        raise GeometryError("tolerance must be finite and positive")
    R = np.asarray(T.R, dtype=np.float64)
    t = np.asarray(T.t, dtype=np.float64)
    if R.shape != (3, 3):
        raise GeometryError(f"{what}: R must be 3x3, got {R.shape}")
    if t.shape != (3,):
        raise GeometryError(f"{what}: t must have 3 entries, got {t.shape}")
    if not np.all(np.isfinite(R)):
        raise GeometryError(f"{what}: R contains non-finite entries")
    if not np.all(np.isfinite(t)):
        raise GeometryError(f"{what}: t contains non-finite entries")
    orth = float(np.max(np.abs(R.T @ R - np.eye(3))))
    if orth > tol:
        raise GeometryError(f"{what}: R is not orthogonal under tol={tol:g} (max err {orth:.3e})")
    det = float(np.linalg.det(R))
    if abs(det - 1.0) > tol:
        raise GeometryError(f"{what}: det(R)={det:.12f} differs from 1 by more than tol={tol:g}")
    # A finite projective-looking matrix could pass the two checks above only
    # for tiny matrices; guard the scale as well.
    if float(np.max(np.abs(R))) > 1.0 + tol:
        raise GeometryError(f"{what}: |R| entries exceed 1 under tol={tol:g}")


def transform_close(a: RigidTransform, b: RigidTransform, tol: float) -> bool:
    return (
        float(np.max(np.abs(a.R - b.R))) <= tol
        and float(np.max(np.abs(a.t - b.t))) <= tol
    )


def transform_diff(a: RigidTransform, b: RigidTransform) -> dict:
    return {
        "rotation_max_abs": float(np.max(np.abs(a.R - b.R))),
        "translation_max_abs": float(np.max(np.abs(a.t - b.t))),
        "rotation_deg": rotation_error_deg(a, b),
    }


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n <= EPS:
        raise GeometryError("cannot normalize a zero/non-finite vector")
    return v / n


def look_at_pose(center: np.ndarray, target: np.ndarray, up: np.ndarray | None = None) -> RigidTransform:
    """Camera-to-world pose with x right, y down, z forward (pinhole convention)."""
    C = np.asarray(center, dtype=np.float64).reshape(3)
    f = normalize(np.asarray(target, dtype=np.float64).reshape(3) - C)
    up_w = np.asarray(up if up is not None else [0.0, 0.0, 1.0], dtype=np.float64).reshape(3)
    if abs(float(np.dot(f, normalize(up_w)))) > 1.0 - 1e-9:
        up_w = np.array([0.0, 1.0, 0.0])
    right = normalize(np.cross(f, up_w))
    down = np.cross(f, right)
    R_wc = np.vstack([right, down, f])  # world -> camera
    R_cw = R_wc.T
    return RigidTransform(R_cw, C)


def random_transform(rng: np.random.Generator, max_angle_deg: float = 20.0, max_trans: float = 1.0) -> RigidTransform:
    axis = rng.normal(size=3)
    axis /= np.linalg.norm(axis)
    angle = np.radians(rng.uniform(-max_angle_deg, max_angle_deg))
    K = np.array([[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]], [-axis[1], axis[0], 0.0]])
    R = np.eye(3) + np.sin(angle) * K + (1.0 - np.cos(angle)) * (K @ K)
    t = rng.uniform(-max_trans, max_trans, size=3)
    return RigidTransform(R, t)
