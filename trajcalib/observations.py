"""Observation ingestion and pinhole/ray geometry.

An observation is a pixel measurement of one track in one camera at an
integer time::

    {"obs_id": "...", "track_id": "...", "t": 12, "camera_id": "cam_01",
     "u": 640.0, "v": 360.0, "weight": 1.0}

The batch contract is strict: identity duplicates ``(track_id, t, camera_id)``
or duplicate ``obs_id`` reject the whole batch. Weights must be finite and
positive. No lens distortion is modelled.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

from .geometry import GeometryError, RigidTransform, normalize


class ObservationError(ValueError):
    """Raised when an observation batch violates the contract."""


@dataclass(frozen=True)
class Observation:
    obs_id: str
    track_id: str
    t: int
    camera_id: str
    u: float
    v: float
    weight: float

    def as_dict(self) -> dict:
        return {
            "obs_id": self.obs_id,
            "track_id": self.track_id,
            "t": self.t,
            "camera_id": self.camera_id,
            "u": self.u,
            "v": self.v,
            "weight": self.weight,
        }


REQUIRED_FIELDS = ("track_id", "t", "camera_id", "u", "v", "weight")


def parse_observation_rows(rows: list[dict]) -> list[Observation]:
    """Validate and normalize a batch; raises on the first contract violation."""
    if not isinstance(rows, list):
        raise ObservationError("batch must be a JSON array")
    seen_identity: dict[tuple[str, int, str], str] = {}
    seen_obs: set[str] = set()
    out: list[Observation] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ObservationError(f"row {idx}: not an object")
        for field in REQUIRED_FIELDS:
            if field not in row:
                raise ObservationError(f"row {idx}: missing field {field!r}")
        track_id = row["track_id"]
        camera_id = row["camera_id"]
        if not isinstance(track_id, str) or not track_id:
            raise ObservationError(f"row {idx}: track_id must be a non-empty string")
        if not isinstance(camera_id, str) or not camera_id:
            raise ObservationError(f"row {idx}: camera_id must be a non-empty string")
        t = row["t"]
        if isinstance(t, bool) or not isinstance(t, int):
            raise ObservationError(f"row {idx}: t must be an integer")
        try:
            u = float(row["u"])
            v = float(row["v"])
            weight = float(row["weight"])
        except (TypeError, ValueError) as exc:
            raise ObservationError(f"row {idx}: u/v/weight must be numeric") from exc
        if not all(np.isfinite(x) for x in (u, v, weight)):
            raise ObservationError(f"row {idx}: u/v/weight must be finite")
        if weight <= 0:
            raise ObservationError(f"row {idx}: weight must be positive, got {weight}")
        identity = (track_id, t, camera_id)
        if identity in seen_identity:
            raise ObservationError(
                f"duplicate identity {identity}: also claimed by {seen_identity[identity]!r}; "
                "batch rejected"
            )
        seen_identity[identity] = str(row.get("obs_id", f"row{idx}"))
        obs_id = str(row.get("obs_id", f"{track_id}@{t}:{camera_id}"))
        if obs_id in seen_obs:
            raise ObservationError(f"duplicate obs_id {obs_id!r}; batch rejected")
        seen_obs.add(obs_id)
        out.append(Observation(obs_id, track_id, t, camera_id, u, v, weight))
    return out


def load_observations_jsonl(path: str) -> list[Observation]:
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ObservationError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
    return parse_observation_rows(rows)


def observations_sha256(observations: list[Observation]) -> str:
    payload = [o.as_dict() for o in sorted(observations, key=lambda o: o.obs_id)]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# pinhole model
# ----------------------------------------------------------------------
def camera_ray_direction(u: float, v: float, intrinsics: dict) -> np.ndarray:
    """Normalized viewing direction in camera coordinates (z forward)."""
    fx, fy = float(intrinsics["fx"]), float(intrinsics["fy"])
    cx, cy = float(intrinsics["cx"]), float(intrinsics["cy"])
    if not (fx > 0 and fy > 0):
        raise GeometryError("fx, fy must be positive")
    return normalize(np.array([(u - cx) / fx, (v - cy) / fy, 1.0]))


def world_ray(
    u: float, v: float, intrinsics: dict, cam_to_world: RigidTransform
) -> tuple[np.ndarray, np.ndarray]:
    """Return (origin, unit direction) of the pixel ray in world coordinates."""
    d_cam = camera_ray_direction(u, v, intrinsics)
    origin = cam_to_world.t.copy()
    direction = normalize(cam_to_world.rotated(d_cam))
    return origin, direction


def project_world_point(
    X: np.ndarray, intrinsics: dict, cam_to_world: RigidTransform
) -> dict:
    """Independent pinhole projection check.

    ``X`` is transformed to camera coordinates with the product's
    camera-to-world pose, then projected with z > 0. Returns the pixel, the
    camera-frame point and whether the point is strictly in front.
    """
    R = cam_to_world.R
    q = R.T @ (np.asarray(X, dtype=np.float64) - cam_to_world.t)
    fx, fy = float(intrinsics["fx"]), float(intrinsics["fy"])
    cx, cy = float(intrinsics["cx"]), float(intrinsics["cy"])
    z = float(q[2])
    in_front = z > 0.0
    if in_front:
        u_hat = fx * q[0] / z + cx
        v_hat = fy * q[1] / z + cy
    else:
        u_hat = float("nan")
        v_hat = float("nan")
    return {"u": u_hat, "v": v_hat, "z": z, "in_front": in_front, "camera_point": q.tolist()}


def point_line_distance(X: np.ndarray, origin: np.ndarray, direction: np.ndarray) -> float:
    w = normalize(direction)
    d = np.asarray(X, dtype=np.float64) - origin
    perp = d - np.dot(d, w) * w
    return float(np.linalg.norm(perp))
