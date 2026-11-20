"""Deterministic synthetic scenarios.

Main scenario (exactly the requested scale)
-------------------------------------------
* 20 coordinate frames = ``world`` + 11 ``link_*`` frames + 8 ``cam_*`` cameras
* 100 targets, each observed at 100 integer times
* noise-free pinhole projections from the known camera poses/intrinsics
* a few extra tracks reuse the same cameras for boundary cases (time gap,
  single camera, missing observations)
* one camera (``cam_03``) starts with a stale extrinsic in the graph; the
  correction bundle repairs it without changing the observations, producing a
  second run for the same dataset

Fixtures (separate small graphs each with their own product)
------------------------------------------------------------
* ``parallel``      two cameras with identical pose / identical rays (rank 1)
* ``near``          two cameras with a 1e-4 baseline (vanishing parallax)
* ``behind``        a camera with the target behind it (inconsistent solution)
* ``unreachable``   an isolated camera island that cannot reach the world root
* ``loop_conflict`` a small consistent graph plus rejected mutation bundles
                    (conflicting cycle, non-orthogonal R, det=-1, NaN) used by
                    the verification suite

All values are generated deterministically from ``seed``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np

from .geometry import RigidTransform, look_at_pose, random_transform

ROOT = "world"
N_FRAMES = 20
N_CAMERAS = 8
N_TARGETS = 100
N_TIMES = 100


@dataclass
class SyntheticScene:
    name: str
    rng_seed: int
    world_root: str
    frames: dict[str, dict]
    edges: list[dict]
    intrinsics: dict[str, dict]
    cam_to_world: dict[str, RigidTransform]
    track_truth: dict[str, list[dict]]
    observations: list[dict]
    scenario_meta: dict[str, dict]
    ground_truth: dict
    corrections: list[dict] = field(default_factory=list)
    extras: dict = field(default_factory=dict)


def _intrinsics(rng: np.random.Generator, width: int = 1280, height: int = 720) -> dict:
    fx = float(rng.uniform(700.0, 1200.0))
    fy = fx * float(rng.uniform(0.95, 1.05))
    return {
        "fx": fx,
        "fy": fy,
        "cx": width / 2.0 + float(rng.uniform(-15.0, 15.0)),
        "cy": height / 2.0 + float(rng.uniform(-15.0, 15.0)),
        "width": width,
        "height": height,
    }


def _project(X, intr, T_cw):
    """Project strictly in front; return (u, v, q) or None if outside/behind."""
    q = T_cw.R.T @ (np.asarray(X, dtype=np.float64) - T_cw.t)
    if q[2] <= 1e-6:
        return None
    u = intr["fx"] * q[0] / q[2] + intr["cx"]
    v = intr["fy"] * q[1] / q[2] + intr["cy"]
    if u < 0 or v < 0 or u >= intr["width"] or v >= intr["height"]:
        return None
    return float(u), float(v), q


def _pixel_any_depth(X, intr, T_cw):
    q = T_cw.R.T @ (np.asarray(X, dtype=np.float64) - T_cw.t)
    if abs(float(q[2])) < 1e-9:
        return None
    return (
        float(intr["fx"] * q[0] / q[2] + intr["cx"]),
        float(intr["fy"] * q[1] / q[2] + intr["cy"]),
    )


# ----------------------------------------------------------------------
# main scene
# ----------------------------------------------------------------------
def build_main_scene(seed: int = 20261118) -> SyntheticScene:
    rng = np.random.default_rng(seed)

    n_links = N_FRAMES - 1 - N_CAMERAS  # 11
    frames: dict[str, dict] = {}
    edges: list[dict] = []
    truth: dict[str, RigidTransform] = {ROOT: RigidTransform.identity()}
    frames[ROOT] = {"frame_id": ROOT, "intrinsics": None}
    link_names = [f"link_{i:02d}" for i in range(n_links)]

    prev = ROOT
    for i, name in enumerate(link_names):
        T = random_transform(rng, max_angle_deg=15.0, max_trans=2.0)
        truth[name] = T
        frames[name] = {"frame_id": name, "intrinsics": None}
        E = T.inverse().compose(truth[prev])
        edges.append({"edge_id": f"edge_link_{i:02d}", "src": prev, "dst": name, "transform": E.as_dict()})
        if i >= 2 and i % 3 == 0:
            a, b = link_names[i - 2], name
            E_ab = truth[b].inverse().compose(truth[a])
            edges.append({"edge_id": f"edge_loop_{i:02d}", "src": a, "dst": b, "transform": E_ab.as_dict()})
        prev = name

    intrinsics: dict[str, dict] = {}
    cam_to_world: dict[str, RigidTransform] = {}
    camera_names = [f"cam_{i:02d}" for i in range(N_CAMERAS)]
    stale_cam = "cam_03"
    for i, cam in enumerate(camera_names):
        angle = 2.0 * np.pi * i / N_CAMERAS + 0.15
        radius = float(rng.uniform(9.0, 12.0))
        center = np.array([radius * np.cos(angle), radius * np.sin(angle), float(rng.uniform(2.5, 4.5))])
        target = np.array([float(rng.uniform(-1.0, 1.0)), float(rng.uniform(-1.0, 1.0)), 1.2])
        T_cw = look_at_pose(center, target)
        cam_to_world[cam] = T_cw
        intrinsics[cam] = _intrinsics(rng)
        parent = link_names[i % n_links]
        T_stored = T_cw
        if cam == stale_cam:
            err = RigidTransform.from_axis_angle(
                [0.2, 1.0, 0.1], np.radians(2.5), [0.25, -0.18, 0.12]
            )
            T_stored = T_cw.compose(err)
        T_parent_to_cam = T_stored.inverse().compose(truth[parent])
        edges.append(
            {"edge_id": f"edge_{cam}", "src": parent, "dst": cam, "transform": T_parent_to_cam.as_dict()}
        )
        frames[cam] = {"frame_id": cam, "intrinsics": intrinsics[cam]}

    corrections = [
        {
            "op": "replace_edge",
            "edge_id": f"edge_{stale_cam}",
            "transform": cam_to_world[stale_cam].inverse().compose(truth[link_names[3 % n_links]]).as_dict(),
            "reason": "refined extrinsic; source graph corrected",
        }
    ]

    observations: list[dict] = []
    track_truth: dict[str, list[dict]] = {}
    scenario_meta: dict[str, dict] = {}

    def add_obs(track_id: str, t: int, cam: str, u: float, v: float, weight: float = 1.0) -> None:
        observations.append(
            {
                "obs_id": f"{track_id}@{t:03d}@{cam}",
                "track_id": track_id,
                "t": int(t),
                "camera_id": cam,
                "u": float(u),
                "v": float(v),
                "weight": float(weight),
            }
        )

    def weight_for(k: int, t: int, cam_idx: int) -> float:
        return round(0.5 + 0.5 * (1.0 + np.sin(0.37 * t + 0.9 * k + 0.2 * cam_idx)), 6)

    for k in range(N_TARGETS):
        track_id = f"track_{k:03d}"
        p0 = np.array([rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5), rng.uniform(0.8, 2.2)])
        vel = np.array([rng.uniform(-0.02, 0.02), rng.uniform(-0.02, 0.02), rng.uniform(-0.008, 0.008)])
        wobble = rng.uniform(0.05, 0.2)
        truth_track: list[dict] = []
        for t in range(N_TIMES):
            X = p0 + vel * t + wobble * np.array(
                [np.sin(0.13 * t + k), np.cos(0.11 * t + 0.5 * k), 0.2 * np.sin(0.07 * t)]
            )
            truth_track.append({"t": t, "X": [float(x) for x in X]})
            for ci, cam in enumerate(camera_names):
                pr = _project(X, intrinsics[cam], cam_to_world[cam])
                if pr is None:
                    continue
                add_obs(track_id, t, cam, pr[0], pr[1], weight_for(k, t, ci))
        track_truth[track_id] = truth_track
        scenario_meta[track_id] = {"kind": "basic"}

    # boundary tracks reusing the same cameras/frames
    gap_track = "track_gap"
    gap_truth: list[dict] = []
    for t in (0, 1, 2, 40, 41):
        X = np.array([-0.8 + 0.005 * t, 0.7, 2.0])
        gap_truth.append({"t": t, "X": [float(x) for x in X]})
        for ci, cam in ((4, "cam_04"), (5, "cam_05"), (6, "cam_06")):
            pr = _project(X, intrinsics[cam], cam_to_world[cam])
            if pr is not None:
                add_obs(gap_track, t, cam, pr[0], pr[1], weight_for(97, t, ci))
    track_truth[gap_track] = gap_truth
    scenario_meta[gap_track] = {"kind": "time_gap", "description": "gap t=2..40 > max_gap"}

    single_track = "track_single"
    single_truth: list[dict] = []
    for t in range(5):
        X = np.array([0.4, 0.3, 1.5 + 0.02 * t])
        single_truth.append({"t": t, "X": [float(x) for x in X]})
        pr = _project(X, intrinsics["cam_07"], cam_to_world["cam_07"])
        if pr is not None:
            add_obs(single_track, t, "cam_07", pr[0], pr[1], weight_for(98, t, 7))
    track_truth[single_track] = single_truth
    scenario_meta[single_track] = {"kind": "single_camera", "description": "only one camera; no depth"}

    # ground truth
    gt_tracks = {
        tid: {"points": pts, "scenario": scenario_meta[tid]}
        for tid, pts in track_truth.items()
    }
    camera_truth = {
        cam: {
            "center": cam_to_world[cam].t.tolist(),
            "R_cw": cam_to_world[cam].R.tolist(),
            "intrinsics": intrinsics[cam],
        }
        for cam in sorted(cam_to_world)
    }
    ground_truth = {
        "scale": {"frames": N_FRAMES, "cameras": N_CAMERAS, "targets": N_TARGETS, "times": N_TIMES},
        "tracks": gt_tracks,
        "camera_truth": camera_truth,
        "stale_camera": stale_cam,
    }

    return SyntheticScene(
        name="main",
        rng_seed=seed,
        world_root=ROOT,
        frames=frames,
        edges=edges,
        intrinsics=intrinsics,
        cam_to_world=cam_to_world,
        track_truth=track_truth,
        observations=observations,
        scenario_meta=scenario_meta,
        ground_truth=ground_truth,
        corrections=corrections,
        extras={"stale_camera": stale_cam},
    )


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------
def _simple_fixture(
    name: str,
    seed: int,
    cam_specs: list[dict],
    tracks: list[dict],
    extra_frames: list[dict] | None = None,
    extra_edges: list[dict] | None = None,
    corrections: list[dict] | None = None,
    extras: dict | None = None,
) -> SyntheticScene:
    """Build a small scene.

    ``cam_specs``: list of {name, parent, pose: RigidTransform, intrinsics}
    ``tracks``: list of {track_id, points: [X...], times: [...], cameras: [names], behind: bool}
    """
    frames: dict[str, dict] = {"world": {"frame_id": "world", "intrinsics": None}}
    edges: list[dict] = []
    intrinsics: dict[str, dict] = {}
    cam_to_world: dict[str, RigidTransform] = {}
    truth_parents: dict[str, RigidTransform] = {}

    for ef in extra_frames or []:
        frames[ef["frame_id"]] = {"frame_id": ef["frame_id"], "intrinsics": None}
        truth_parents[ef["frame_id"]] = RigidTransform.from_dict(ef["pose"])
        E = truth_parents[ef["frame_id"]].inverse()
        edges.append({"edge_id": f"edge_{ef['frame_id']}", "src": "world", "dst": ef["frame_id"], "transform": E.as_dict()})

    for spec in cam_specs:
        cam = spec["name"]
        pose = spec["pose"]
        intr = spec["intrinsics"]
        intrinsics[cam] = intr
        cam_to_world[cam] = pose
        parent = spec.get("parent", "world")
        # Parent pose; if parent is a camera or extra frame use its pose.
        if parent == "world":
            T_parent = RigidTransform.identity()
        elif parent in cam_to_world:
            T_parent = cam_to_world[parent]
        elif parent in truth_parents:
            T_parent = truth_parents[parent]
        else:
            T_parent = RigidTransform.identity()
        E = pose.inverse().compose(T_parent)
        edges.append({"edge_id": f"edge_{cam}", "src": parent, "dst": cam, "transform": E.as_dict()})
        frames[cam] = {"frame_id": cam, "intrinsics": intr}

    for ee in extra_edges or []:
        edges.append(ee)

    observations: list[dict] = []
    track_truth: dict[str, list[dict]] = {}
    scenario_meta: dict[str, dict] = {}
    for spec in tracks:
        tid = spec["track_id"]
        pts: list[dict] = []
        for t, X in zip(spec["times"], spec["points"]):
            X = np.asarray(X, dtype=np.float64)
            pts.append({"t": int(t), "X": [float(x) for x in X]})
            for cam in spec["cameras"]:
                if spec.get("behind"):
                    uv = _pixel_any_depth(X, intrinsics[cam], cam_to_world[cam])
                else:
                    pr = _project(X, intrinsics[cam], cam_to_world[cam])
                    uv = None if pr is None else (pr[0], pr[1])
                if uv is None:
                    continue
                observations.append(
                    {
                        "obs_id": f"{tid}@{int(t):03d}@{cam}",
                        "track_id": tid,
                        "t": int(t),
                        "camera_id": cam,
                        "u": float(uv[0]),
                        "v": float(uv[1]),
                        "weight": 1.0,
                    }
                )
        track_truth[tid] = pts
        scenario_meta[tid] = {"kind": spec.get("kind", name), "description": spec.get("description", "")}

    ground_truth = {
        "tracks": {tid: {"points": pts, "scenario": scenario_meta[tid]} for tid, pts in track_truth.items()},
        "camera_truth": {
            cam: {"center": cam_to_world[cam].t.tolist(), "R_cw": cam_to_world[cam].R.tolist(), "intrinsics": intrinsics[cam]}
            for cam in sorted(cam_to_world)
        },
    }
    return SyntheticScene(
        name=name,
        rng_seed=seed,
        world_root="world",
        frames=frames,
        edges=edges,
        intrinsics=intrinsics,
        cam_to_world=cam_to_world,
        track_truth=track_truth,
        observations=observations,
        scenario_meta=scenario_meta,
        ground_truth=ground_truth,
        corrections=corrections or [],
        extras=extras or {},
    )


def build_parallel_fixture(seed: int = 11) -> SyntheticScene:
    rng = np.random.default_rng(seed)
    pose = look_at_pose(np.array([0.0, -10.0, 3.0]), np.array([0.0, 0.0, 1.0]))
    intr_a = _intrinsics(rng)
    intr_b = dict(intr_a)
    intr_b["cx"] += 7.0  # same geometry, different pixel offset
    times = list(range(5))
    pts = [np.array([0.5 + 0.02 * t, 0.2, 1.5]) for t in times]
    scene = _simple_fixture(
        "parallel",
        seed,
        cam_specs=[
            {"name": "cam_par_a", "pose": pose, "intrinsics": intr_a},
            {"name": "cam_par_b", "pose": RigidTransform(pose.R.copy(), pose.t.copy()), "intrinsics": intr_b},
        ],
        tracks=[{
            "track_id": "track_parallel",
            "points": pts,
            "times": times,
            "cameras": ["cam_par_a", "cam_par_b"],
            "kind": "parallel_rays",
            "description": "identical poses -> identical rays -> normal matrix rank 1",
        }],
        extras={"expect_status": "degenerate", "expect_reason": "parallel"},
    )
    return scene


def build_near_fixture(seed: int = 12) -> SyntheticScene:
    rng = np.random.default_rng(seed)
    pose_a = look_at_pose(np.array([0.0, -10.0, 3.0]), np.array([0.0, 0.0, 1.0]))
    pose_b = RigidTransform(pose_a.R.copy(), pose_a.t + np.array([5e-3, 0.0, 0.0]))
    intr = _intrinsics(rng)
    times = list(range(5))
    pts = [np.array([0.0, 0.0, 1.0]) for _ in times]
    return _simple_fixture(
        "near",
        seed,
        cam_specs=[
            {"name": "cam_near_a", "pose": pose_a, "intrinsics": intr},
            {"name": "cam_near_b", "pose": pose_b, "intrinsics": dict(intr)},
        ],
        tracks=[{
            "track_id": "track_near",
            "points": pts,
            "times": times,
            "cameras": ["cam_near_a", "cam_near_b"],
            "kind": "near_degenerate",
            "description": "1e-4 baseline; eigen ratio below default threshold",
        }],
        extras={"expect_status": "degenerate", "expect_reason": "near"},
    )


def build_behind_fixture(seed: int = 13) -> SyntheticScene:
    rng = np.random.default_rng(seed)
    pose_f = look_at_pose(np.array([0.0, -10.0, 3.0]), np.array([0.0, 0.0, 1.0]))
    pose_g = look_at_pose(np.array([10.0, 0.0, 3.0]), np.array([0.0, 0.0, 1.0]))
    # Looks toward +z from z=4; the target at z~1 is *behind* this camera.
    pose_b = look_at_pose(np.array([0.0, 0.0, 4.0]), np.array([0.0, 0.0, 9.0]))
    times = list(range(5))
    pts = [np.array([0.0, 0.0, 1.0]) + np.array([0.1 * t, 0.05 * t, 0.0]) for t in times]
    intrinsic = _intrinsics(rng)
    return _simple_fixture(
        "behind",
        seed,
        cam_specs=[
            {"name": "cam_f", "pose": pose_f, "intrinsics": intrinsic},
            {"name": "cam_g", "pose": pose_g, "intrinsics": dict(intrinsic)},
            {"name": "cam_behind", "pose": pose_b, "intrinsics": dict(intrinsic)},
        ],
        tracks=[{
            "track_id": "track_behind",
            "points": pts,
            "times": times,
            "cameras": ["cam_f", "cam_g", "cam_behind"],
            "behind": True,
            "kind": "behind_camera",
            "description": "rays meet at a point behind cam_behind; must be flagged inconsistent",
        }],
        extras={
            "expect_status": "inconsistent",
            "expect_behind_camera": "cam_behind",
        },
    )


def build_unreachable_fixture(seed: int = 14) -> SyntheticScene:
    rng = np.random.default_rng(seed)
    pose0 = look_at_pose(np.array([0.0, -10.0, 3.0]), np.array([0.0, 0.0, 1.0]))
    pose1 = look_at_pose(np.array([10.0, 0.0, 3.0]), np.array([0.0, 0.0, 1.0]))
    pose_iso = look_at_pose(np.array([100.0, 100.0, 100.0]), np.array([0.0, 0.0, 0.0]))
    times = list(range(4))
    pts = [np.array([0.2, 0.1, 1.2]) for _ in times]
    scene = _simple_fixture(
        "unreachable",
        seed,
        cam_specs=[
            {"name": "cam_r0", "pose": pose0, "intrinsics": _intrinsics(rng)},
            {"name": "cam_r1", "pose": pose1, "intrinsics": _intrinsics(rng)},
        ],
        extra_frames=None,
        tracks=[{
            "track_id": "track_unreachable_case",
            "points": pts,
            "times": times,
            "cameras": ["cam_r0", "cam_r1"],
            "kind": "reachable_control",
            "description": "control track in the reachable component",
        }],
        extras={"expect_unreachable": ["cam_iso"]},
    )
    # Isolated island: orphan_link -> cam_iso, disconnected from world.
    orphan_link = RigidTransform(np.eye(3), np.array([100.0, 0.0, 0.0]))
    scene.frames["orphan_link"] = {"frame_id": "orphan_link", "intrinsics": None}
    intr_iso = _intrinsics(rng)
    scene.intrinsics["cam_iso"] = intr_iso
    scene.cam_to_world["cam_iso"] = pose_iso
    scene.frames["cam_iso"] = {"frame_id": "cam_iso", "intrinsics": intr_iso}
    scene.edges.append(
        {
            "edge_id": "edge_cam_iso",
            "src": "orphan_link",
            "dst": "cam_iso",
            "transform": pose_iso.inverse().compose(orphan_link).as_dict(),
        }
    )
    scene.ground_truth["camera_truth"]["cam_iso"] = {
        "center": pose_iso.t.tolist(),
        "R_cw": pose_iso.R.tolist(),
        "intrinsics": intr_iso,
        "reachable": False,
    }
    return scene


def build_loop_conflict_fixture(seed: int = 15) -> SyntheticScene:
    rng = np.random.default_rng(seed)
    pose0 = look_at_pose(np.array([0.0, -10.0, 3.0]), np.array([0.0, 0.0, 1.0]))
    pose1 = look_at_pose(np.array([10.0, 0.0, 3.0]), np.array([0.0, 0.0, 1.0]))
    T_link = RigidTransform.from_axis_angle([0.0, 0.0, 1.0], np.radians(20.0), [1.0, 2.0, 0.5])
    times = list(range(3))
    pts = [np.array([0.0, 0.0, 1.0]) for _ in times]
    scene = _simple_fixture(
        "loop_conflict",
        seed,
        cam_specs=[
            {"name": "cam_a", "pose": pose0, "intrinsics": _intrinsics(rng)},
            {"name": "cam_b", "pose": pose1, "intrinsics": _intrinsics(rng)},
        ],
        extra_frames=[{"frame_id": "link_x", "pose": T_link}],
        tracks=[{
            "track_id": "track_loop_ok",
            "points": pts,
            "times": times,
            "cameras": ["cam_a", "cam_b"],
            "kind": "basic",
            "description": "valid observations on the consistent base graph",
        }],
    )
    # Candidate conflicting cycle: link_x -> world stated with a transform that
    # disagrees with the chain world -> link_x.
    bad_cycle = RigidTransform.from_axis_angle([1.0, 0.0, 0.0], np.radians(5.0), [0.5, 0.0, 0.0])
    R_nonorth = np.array([[1.0, 0.1, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    R_mirror = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]])
    R_nan = np.array([[np.nan, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    R_inf = np.array([[np.inf, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    scene.extras["rejected_mutations"] = [
        {
            "label": "conflicting_cycle",
            "ops": [{"op": "add_edge", "edge_id": "edge_bad_cycle", "src": "link_x", "dst": "world", "transform": bad_cycle.as_dict()}],
            "expect_error": "inconsistent cycle",
        },
        {
            "label": "non_orthogonal_R",
            "ops": [{"op": "add_edge", "edge_id": "edge_nonorth", "src": "world", "dst": "link_x", "transform": {"R": R_nonorth.tolist(), "t": [0.0, 0.0, 0.0]}}],
            "expect_error": "not orthogonal",
        },
        {
            "label": "det_minus_one",
            "ops": [{"op": "add_edge", "edge_id": "edge_mirror", "src": "world", "dst": "link_x", "transform": {"R": R_mirror.tolist(), "t": [0.0, 0.0, 0.0]}}],
            "expect_error": "det(R)",
        },
        {
            "label": "nan_matrix",
            "ops": [{"op": "add_edge", "edge_id": "edge_nan", "src": "world", "dst": "link_x", "transform": {"R": R_nan.tolist(), "t": [0.0, 0.0, 0.0]}}],
            "expect_error": "non-finite",
        },
        {
            "label": "inf_translation",
            "ops": [{"op": "add_edge", "edge_id": "edge_inf", "src": "cam_a", "dst": "world", "transform": {"R": R_inf.tolist(), "t": [0.0, 0.0, 0.0]}}],
            "expect_error": "non-finite",
        },
    ]
    return scene


def build_all(seed: int = 20261118) -> dict[str, SyntheticScene]:
    return {
        "main": build_main_scene(seed),
        "parallel": build_parallel_fixture(),
        "near": build_near_fixture(),
        "behind": build_behind_fixture(),
        "unreachable": build_unreachable_fixture(),
        "loop_conflict": build_loop_conflict_fixture(),
    }


# ----------------------------------------------------------------------
# serialization
# ----------------------------------------------------------------------
def _write_scene_files(scene: SyntheticScene, out_dir: str) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    paths: dict[str, str] = {}

    paths["frames"] = os.path.join(out_dir, "frames.json")
    with open(paths["frames"], "w", encoding="utf-8") as fh:
        json.dump(
            {
                "world_root": scene.world_root,
                "frames": {
                    fid: {"frame_id": fid, "intrinsics": scene.frames[fid]["intrinsics"]}
                    for fid in sorted(scene.frames)
                },
            },
            fh,
            indent=2,
            sort_keys=True,
        )
        fh.write("\n")

    paths["graph_events"] = os.path.join(out_dir, "graph_events.json")
    with open(paths["graph_events"], "w", encoding="utf-8") as fh:
        json.dump({"world_root": scene.world_root, "edges": scene.edges}, fh, indent=2, sort_keys=True)
        fh.write("\n")

    paths["observations"] = os.path.join(out_dir, "observations.jsonl")
    with open(paths["observations"], "w", encoding="utf-8") as fh:
        for obs in sorted(scene.observations, key=lambda o: (o["track_id"], o["t"], o["camera_id"])):
            fh.write(json.dumps(obs, sort_keys=True) + "\n")

    paths["ground_truth"] = os.path.join(out_dir, "ground_truth.json")
    with open(paths["ground_truth"], "w", encoding="utf-8") as fh:
        json.dump(scene.ground_truth, fh, indent=2, sort_keys=True)
        fh.write("\n")

    paths["scenarios"] = os.path.join(out_dir, "scenarios.json")
    with open(paths["scenarios"], "w", encoding="utf-8") as fh:
        json.dump(
            {"scenarios": scene.scenario_meta, "extras": _sanitize(scene.extras)},
            fh,
            indent=2,
            sort_keys=True,
        )
        fh.write("\n")

    paths["corrections"] = os.path.join(out_dir, "corrections.json")
    with open(paths["corrections"], "w", encoding="utf-8") as fh:
        json.dump({"operations": scene.corrections}, fh, indent=2, sort_keys=True)
        fh.write("\n")

    return paths


def _sanitize(obj):
    """Make NaN/Inf JSON-safe for the fixtures report."""
    if isinstance(obj, float):
        if obj != obj:
            return "NaN"
        if obj in (float("inf"), float("-inf")):
            return "Infinity" if obj > 0 else "-Infinity"
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def write_scenes(out_dir: str, scenes: dict[str, SyntheticScene] | None = None) -> dict[str, dict[str, str]]:
    scenes = scenes or build_all()
    result: dict[str, dict[str, str]] = {}
    for name, scene in scenes.items():
        result[name] = _write_scene_files(scene, os.path.join(out_dir, name))
    return result
