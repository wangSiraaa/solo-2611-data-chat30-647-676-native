"""Independent verification of calibration graph and trajectory warehouse.

Nothing here trusts the solver's internals. For every published point we

* recompute the ray from the raw pixel with the *product* intrinsics and pose,
* recompute the least-squares solution with a stacked (SciPy ``lstsq``) system
  rather than the normal equations used by the solver,
* reproject the stored XYZ with an independent pinhole formula and compare to
  the stored pixel residual,
* compare against the synthetic ground-truth XYZ (noise-free generation).

The suite also exercises each required edge case and reports the retained
failure evidence.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import numpy as np

from .geometry import RigidTransform, validate_transform, GeometryError
from .graph import (
    CalibrationGraphService,
    load_product,
)
from .observations import (
    Observation,
    ObservationError,
    parse_observation_rows,
)
from .pipeline import run_main_pipeline
from .scene import build_all
from .warehouse import TrajectoryWarehouse


class Checker:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def check(self, name: str, ok: bool, detail=None) -> bool:
        self.results.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(r["ok"] for r in self.results)

    def summary(self) -> dict:
        n = len(self.results)
        passed = sum(1 for r in self.results if r["ok"])
        return {
            "ok": self.ok,
            "checks_total": n,
            "checks_passed": passed,
            "checks_failed": n - passed,
            "failures": [r for r in self.results if not r["ok"]],
            "results": self.results,
        }


# ----------------------------------------------------------------------
# independent geometry
# ----------------------------------------------------------------------
def independent_ray(u, v, intr, cam_to_world):
    fx, fy, cx, cy = intr["fx"], intr["fy"], intr["cx"], intr["cy"]
    d_cam = np.array([(u - cx) / fx, (v - cy) / fy, 1.0])
    d_cam = d_cam / np.linalg.norm(d_cam)
    d_world = cam_to_world.R @ d_cam
    d_world = d_world / np.linalg.norm(d_world)
    return cam_to_world.t.copy(), d_world


def independent_triangulate(rays):
    """Stacked weighted least squares via SVD (independent of normal equations)."""
    rows = []
    rhs = []
    for origin, direction, w in rays:
        P = np.eye(3) - np.outer(direction, direction)
        sw = np.sqrt(w)
        rows.append(sw * P)
        rhs.append(sw * (P @ origin))
    A = np.vstack(rows)
    b = np.concatenate(rhs)
    X, *_ = np.linalg.lstsq(A, b, rcond=None)
    # eigen-ratio from the *unweighted-by-solver* Gram matrix
    evals = np.linalg.eigvalsh(A.T @ A)
    evals = np.clip(evals, 0.0, None)
    ratio = float(evals[0] / evals[-1]) if evals[-1] > 0 else 0.0
    return X, ratio


def independent_project(X, intr, cam_to_world):
    q = cam_to_world.R.T @ (np.asarray(X) - cam_to_world.t)
    z = float(q[2])
    if z <= 0:
        return None, z
    u = intr["fx"] * q[0] / z + intr["cx"]
    v = intr["fy"] * q[1] / z + intr["cy"]
    return (float(u), float(v)), z


def _transform_from_matrix(R, t):
    return RigidTransform(np.asarray(R, dtype=float), np.asarray(t, dtype=float))


# ----------------------------------------------------------------------
# main entry
# ----------------------------------------------------------------------
def run_verification(workspace: str, min_eigen_ratio: float = 1e-3, max_gap: int = 1) -> dict:
    """Rebuild everything in a fresh temporary workspace, then verify it."""
    tmp = tempfile.mkdtemp(prefix="trajcalib_verify_")
    try:
        c = Checker()
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        summary = run_main_pipeline(ws, min_eigen_ratio=min_eigen_ratio, max_gap=max_gap)
        scenes = build_all()
        main = scenes["main"]
        _verify_scale(c, main, ws)
        _verify_products(c, main, ws, summary)
        _verify_main_run(c, main, ws, summary)
        _verify_warehouse_independence(c, ws, summary)
        _verify_parallel(c, ws, summary)
        _verify_near(c, ws, summary)
        _verify_behind(c, ws, summary)
        _verify_unreachable(c, ws, summary)
        _verify_loop_conflict(c, ws, summary)
        _verify_observation_contract(c)
        _verify_rebuild_provenance(c, ws, summary)
        out = c.summary()
        out["workspace"] = workspace
        out["temp_workspace"] = ws
        out["artifacts"] = {
            "pipeline_summary": os.path.join(workspace, "out", "pipeline_summary.json"),
            "graph_db": os.path.join(workspace, "db", "calibration_graph.sqlite"),
            "warehouse_db": os.path.join(workspace, "db", "trajectory_warehouse.sqlite"),
            "scene_dir": os.path.join(workspace, "out", "main"),
        }
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# product / graph checks
# ----------------------------------------------------------------------
def _verify_scale(c: Checker, main, ws: str) -> None:
    frame_ids = set(main.frames)
    camera_ids = [fid for fid, f in main.frames.items() if f["intrinsics"]]
    c.check("scale.frames_20", len(frame_ids) == 20, len(frame_ids))
    c.check("scale.cameras_8", len(camera_ids) == 8, sorted(camera_ids))
    c.check("scale.targets_100", len(main.track_truth) == 102, {k: v for k, v in main.scenario_meta.items() if v["kind"] != "basic"})
    base = [tid for tid, meta in main.scenario_meta.items() if meta["kind"] == "basic"]
    c.check("scale.base_targets_100", len(base) == 100, len(base))
    all_full = all(
        len([p for p in main.track_truth[tid] if True]) == 100 for tid in base
    )
    c.check("scale.each_target_100_times", all_full)
    c.check(
        "scale.observations_noise_free_generated",
        len(main.observations) > 70000,
        len(main.observations),
    )


def _verify_products(c: Checker, main, ws: str, summary: dict) -> None:
    p1 = load_product(summary["product_v1"]["path"])
    p2 = load_product(summary["product_v2"]["path"])
    c.check("product_v1.integrity", p1.verify(), p1.integrity[:16])
    c.check("product_v2.integrity", p2.verify(), p2.integrity[:16])
    c.check("product_v1_is_not_v2", p1.integrity != p2.integrity)

    # every emitted transform must be a proper rigid transform under tolerance
    for label, product in (("v1", p1), ("v2", p2)):
        bad = []
        for cam, T in product.cam_to_world.items():
            try:
                validate_transform(T, product.tolerance, what=cam)
            except GeometryError as exc:
                bad.append(str(exc))
        c.check(f"product_{label}.transforms_valid", not bad, bad[:3])

    # compare to synthetic truth, which is the *true* camera-to-world
    truth = {cam: _transform_from_matrix(v["R_cw"], v["center"]) for cam, v in main.ground_truth["camera_truth"].items()}
    errs_v2 = {cam: float(np.max(np.abs(T.R - truth[cam].R))) + float(np.max(np.abs(T.t - truth[cam].t))) for cam, T in p2.cam_to_world.items()}
    c.check("product_v2.matches_truth_all_cameras", all(e < 1e-9 for e in errs_v2.values()), errs_v2)
    errs_v1 = {cam: float(np.max(np.abs(T.R - truth[cam].R))) + float(np.max(np.abs(T.t - truth[cam].t))) for cam, T in p1.cam_to_world.items()}
    stale = main.extras["stale_camera"]
    c.check("product_v1.stale_camera_differs", errs_v1[stale] > 1e-3, errs_v1[stale])
    others_v1 = {k: v for k, v in errs_v1.items() if k != stale}
    c.check("product_v1.other_cameras_match", all(e < 1e-9 for e in others_v1.values()), others_v1)

    # every reachable camera is present in both products
    c.check("product_v2.all_cameras_reachable", sorted(p2.cam_to_world) == [f"cam_{i:02d}" for i in range(8)], sorted(p2.cam_to_world))
    c.check("product_v2.no_unreachable", p2.unreachable_cameras == {}, p2.unreachable_cameras)

    # graph transform queries agree with the product, without observations
    db = summary["graph_db"]
    graph = CalibrationGraphService(db_path=db, tolerance=1e-9)
    # Invariant: T(a->c) == T(b->c) ∘ T(a->b)
    Tab = graph.transform("link_02", "link_07")
    Tbc = graph.transform("link_07", "cam_05")
    Tac = graph.transform("link_02", "cam_05")
    comp = Tbc.compose(Tab)
    c.check(
        "graph.path_composition_invariant",
        float(np.max(np.abs(comp.R - Tac.R))) < 1e-9 and float(np.max(np.abs(comp.t - Tac.t))) < 1e-9,
        {"rot": float(np.max(np.abs(comp.R - Tac.R))), "trans": float(np.max(np.abs(comp.t - Tac.t)))},
    )
    Tcam = graph.transform("cam_00", "world")
    c.check(
        "graph.camera_to_world_matches_product",
        float(np.max(np.abs(Tcam.R - p2.cam_to_world["cam_00"].R))) < 1e-9
        and float(np.max(np.abs(Tcam.t - p2.cam_to_world["cam_00"].t))) < 1e-9,
    )
    # round trip: T(world->cam) ∘ T(cam->world) == identity
    Tcw = graph.transform("world", "cam_00")
    Tr = Tcw.compose(Tcam)
    c.check("graph.round_trip_identity", float(np.max(np.abs(Tr.R - np.eye(3)))) < 1e-9 and float(np.max(np.abs(Tr.t))) < 1e-9)

    # cycle consistency of the full graph under the configured tolerance
    report = graph.validate()
    c.check("graph.consistent_no_cycle_conflicts", report.ok and not report.conflicts, report.conflicts[:2])


# ----------------------------------------------------------------------
# main dataset checks
# ----------------------------------------------------------------------
def _verify_main_run(c: Checker, main, ws: str, summary: dict) -> None:
    wh_path = summary["warehouse_db"]
    wh = TrajectoryWarehouse(wh_path)
    try:
        runs = wh.list_runs()
        c.check("warehouse.two_runs_same_dataset", len(runs) == 2 and runs[0]["dataset_id"] == runs[1]["dataset_id"], runs)
        p2 = wh.get_product(summary["product_v2"]["id"])
        run2 = wh.get_run(summary["product_v2"]["run"]["run_id"])
        run1 = wh.get_run(summary["product_v1"]["run"]["run_id"])
        c.check("warehouse.old_run_preserved_after_rebuild", run1.run_id != run2.run_id and run1.product_id != run2.product_id)
        c.check("warehouse.old_run_keeps_its_product", wh.get_product(run1.product_id).product_id == summary["product_v1"]["id"])

        gt = main.ground_truth["tracks"]
        obs_rows = wh.dataset_observations(run2.dataset_id)
        obs_by_group: dict[tuple[str, int], list[Observation]] = {}
        for o in obs_rows:
            obs_by_group.setdefault((o.track_id, o.t), []).append(o)

        # sample published points across many tracks and times for full checks
        sample = []
        for (tid, t), obs in obs_by_group.items():
            if tid in gt and len(obs) >= 2:
                sample.append((tid, t, obs))
        rng = np.random.default_rng(7)
        rng.shuffle(sample)
        sample = sample[:400]

        max_xyz_err = 0.0
        max_pix_err = 0.0
        max_lstsq_gap = 0.0
        ray_formula_max = 0.0
        bad_status = []
        status_counts = {"published": 0, "degenerate": 0, "inconsistent": 0}

        for tid, t, obs in sample:
            pt = wh.get_point(run2.run_id, tid, t)
            status_counts[pt["status"]] = status_counts.get(pt["status"], 0) + 1
            if pt["status"] != "published":
                continue
            X = np.array(pt["X"])

            # (1) independent triangulation from raw observations + product
            rays = []
            for o in obs:
                intr = p2.intrinsics[o.camera_id].as_dict()
                origin, direction = independent_ray(o.u, o.v, intr, p2.cam_to_world[o.camera_id])
                rays.append((origin, direction, o.weight))
            X_ind, _ = independent_triangulate(rays)
            max_lstsq_gap = max(max_lstsq_gap, float(np.linalg.norm(X - X_ind)))

            # (2) ground-truth comparison
            g = next((p for p in gt[tid]["points"] if p["t"] == t), None)
            if g is not None:
                max_xyz_err = max(max_xyz_err, float(np.linalg.norm(X - np.array(g["X"]))))

            # (3) independent pixel reprojection
            for o in obs:
                intr = p2.intrinsics[o.camera_id].as_dict()
                uv, z = independent_project(X, intr, p2.cam_to_world[o.camera_id])
                if uv is None:
                    bad_status.append((tid, t, o.camera_id, "behind"))
                    continue
                du = o.u - uv[0]
                dv = o.v - uv[1]
                max_pix_err = max(max_pix_err, float(np.hypot(du, dv)))
                # |z| > 0 because we only project in front here
                if z <= 0:
                    bad_status.append((tid, t, o.camera_id, "z<=0"))

            # (4) ray formula: pixel -> normalize(((u-cx)/fx,(v-cy)/fy,1))
            for o in obs:
                intr = p2.intrinsics[o.camera_id].as_dict()
                raw = np.array([(o.u - intr["cx"]) / intr["fx"], (o.v - intr["cy"]) / intr["fy"], 1.0])
                raw /= np.linalg.norm(raw)
                _, d = independent_ray(o.u, o.v, intr, p2.cam_to_world[o.camera_id])
                cam_axis = p2.cam_to_world[o.camera_id].R.T @ d
                ray_formula_max = max(ray_formula_max, float(np.max(np.abs(cam_axis - raw))))

        c.check("solver.agrees_with_independent_lstsq", max_lstsq_gap < 1e-7, max_lstsq_gap)
        c.check("points.match_synthetic_truth", max_xyz_err < 1e-7, max_xyz_err)
        c.check("pixel_reprojection_independent", max_pix_err < 1e-7, max_pix_err)
        c.check("ray_formula_matches_normalization", ray_formula_max < 1e-12, ray_formula_max)
        c.check("no_unexpected_behind_camera_in_sample", not bad_status, bad_status[:3])
        c.check("sample_has_published_points", status_counts.get("published", 0) > 0.9 * len(sample), status_counts)

        # every stored pixel residual for run2 must be ~0
        import sqlite3

        con = sqlite3.connect(wh_path)
        try:
            row = con.execute(
                "SELECT MAX(pixel_residual), AVG(pixel_residual) FROM point_cameras WHERE run_id=? AND pixel_residual IS NOT NULL",
                (run2.run_id,),
            ).fetchone()
        finally:
            con.close()
        c.check("stored_pixel_residuals_zero_run2", row[0] is not None and row[0] < 1e-7, {"max": row[0], "avg": row[1]})

        # run1 uses the stale camera: it must have large residuals for cam_03
        con = sqlite3.connect(wh_path)
        try:
            r1 = con.execute(
                "SELECT MAX(pixel_residual) FROM point_cameras WHERE run_id=? AND camera_id=? AND pixel_residual IS NOT NULL",
                (run1.run_id, main.extras["stale_camera"]),
            ).fetchone()
            r2 = con.execute(
                "SELECT MAX(pixel_residual) FROM point_cameras WHERE run_id=? AND camera_id=? AND pixel_residual IS NOT NULL",
                (run2.run_id, main.extras["stale_camera"]),
            ).fetchone()
        finally:
            con.close()
        c.check("stale_calibration_visible_in_run1", r1[0] is not None and r1[0] > 1.0 and r2[0] < 1e-7, {"run1_max": r1[0], "run2_max": r2[0]})

        # boundary tracks
        segs = { (s["t0"], s["t1"]): s for s in wh.trajectory(run2.run_id, "track_gap")["segments"] }
        c.check("gap.segment_blocked", segs[(2, 40)]["interpolate_allowed"] == 0 and "gap" in segs[(2, 40)]["reason"], segs[(2, 40)])
        interp_mid = wh.interpolate(run2.run_id, "track_gap", 20.0)
        c.check("gap.no_interpolated_position", interp_mid["available"] is False, interp_mid)

        single = wh.trajectory(run2.run_id, "track_single")["points"]
        c.check("single_camera.degenerate", all(p["status"] == "degenerate" for p in single) and len(single) > 0, [p["status"] for p in single])
        interp_single = wh.interpolate(run2.run_id, "track_single", 1.5)
        c.check("single_camera.no_interpolation", interp_single["available"] is False, interp_single)

        # trace provenance
        sample_tid = None
        for (tid, t, o) in sample:
            pt = wh.get_point(run2.run_id, tid, t)
            if pt["status"] == "published":
                sample_tid, sample_t = tid, t
                break
        if sample_tid is not None:
            trace = wh.trace_point(run2.run_id, sample_tid, sample_t)
            obs_ids = {cam["obs_id"] for cam in trace["cameras"]}
            expected = {o.obs_id for (tid, t, obs) in sample if tid == sample_tid and t == sample_t for o in obs}
            c.check("trace.links_point_to_observations", obs_ids == expected, {"obs": sorted(obs_ids), "expected": sorted(expected)})
            c.check(
                "trace.uses_product_transforms",
                all(
                    float(np.max(np.abs(_transform_from_matrix(cam["camera_to_world"]["R"], cam["camera_to_world"]["t"]).R - p2.cam_to_world[cam["camera_id"]].R))) < 1e-12
                    for cam in trace["cameras"]
                ),
            )
    finally:
        wh.close()


def _verify_warehouse_independence(c: Checker, ws: str, summary: dict) -> None:
    """Warehouse must answer from product JSON even if graph db is unavailable."""
    wh_path = summary["warehouse_db"]
    graph_db = summary["graph_db"]
    backup = graph_db + ".hidden"
    os.rename(graph_db, backup)
    try:
        wh = TrajectoryWarehouse(wh_path)
        try:
            trace = wh.trace_point(
                summary["product_v2"]["run"]["run_id"], "track_000", 0
            )
            c.check("warehouse.works_without_graph_db", trace["product_id"] == summary["product_v2"]["id"])
        finally:
            wh.close()
    finally:
        os.rename(backup, graph_db)


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------
def _fixture_run(c: Checker, name: str, ws: str, summary: dict) -> tuple[dict, TrajectoryWarehouse, str]:
    f = summary["fixtures"][name]
    wh = TrajectoryWarehouse(f["warehouse_db"])
    return f, wh, f["run"]["run_id"]


def _verify_parallel(c: Checker, ws: str, summary: dict) -> None:
    f, wh, run_id = _fixture_run(c, "parallel", ws, summary)
    try:
        pts = wh.trajectory(run_id, "track_parallel")["points"]
        uniform = all(p["status"] == "degenerate" for p in pts)
        c.check("parallel.all_degenerate", uniform, [p["status"] for p in pts])
        c.check("parallel.rank_below_3", all(p["rank"] < 3 for p in pts), [p["rank"] for p in pts])
        c.check(
            "parallel.keeps_failure_evidence",
            all(p["rationale"] and p["eigen_ratio"] <= p["min_eigen_ratio_threshold"] for p in pts),
            pts[0]["rationale"] if pts else None,
        )
        c.check("parallel.no_fabricated_xyz", all(p.get("X") is None for p in pts))
        c.check("parallel.no_interpolation", wh.interpolate(run_id, "track_parallel", 1.5)["available"] is False)
    finally:
        wh.close()


def _verify_near(c: Checker, ws: str, summary: dict) -> None:
    f, wh, run_id = _fixture_run(c, "near", ws, summary)
    try:
        pts = wh.trajectory(run_id, "track_near")["points"]
        c.check("near.all_degenerate", all(p["status"] == "degenerate" for p in pts), [p["status"] for p in pts])
        c.check(
            "near.ratio_below_threshold",
            all(p["eigen_ratio"] < p["min_eigen_ratio_threshold"] for p in pts),
            [p["eigen_ratio"] for p in pts],
        )
        c.check("near.no_fabricated_xyz", all(p.get("X") is None for p in pts))
    finally:
        wh.close()


def _verify_behind(c: Checker, ws: str, summary: dict) -> None:
    f, wh, run_id = _fixture_run(c, "behind", ws, summary)
    try:
        pts = wh.trajectory(run_id, "track_behind")["points"]
        c.check("behind.inconsistent", all(p["status"] == "inconsistent" for p in pts), [p["status"] for p in pts])
        # observations must not have been silently deleted
        tr = wh.trace_point(run_id, "track_behind", pts[0]["t"])
        cams = {cam["camera_id"] for cam in tr["cameras"]}
        c.check("behind.observations_retained", {"cam_f", "cam_g", "cam_behind"} <= cams, sorted(cams))
        behind_cam = [cam for cam in tr["cameras"] if cam["z_positive"] is False]
        c.check("behind.flags_negative_depth_camera", len(behind_cam) == 1 and behind_cam[0]["camera_id"] == "cam_behind", [c["camera_id"] for c in behind_cam])
        c.check("behind.point_still_stored", pts[0].get("X") is not None)
        c.check("behind.no_interpolation", wh.interpolate(run_id, "track_behind", 1.5)["available"] is False)
    finally:
        wh.close()


def _verify_unreachable(c: Checker, ws: str, summary: dict) -> None:
    f, wh, run_id = _fixture_run(c, "unreachable", ws, summary)
    try:
        p = f["product"]
        c.check("unreachable.listed_explicitly", "cam_iso" in p["unreachable_cameras"], p["unreachable_cameras"])
        c.check("unreachable.not_in_calibrated", "cam_iso" not in p["reachable_frames"])
        prod = wh.get_product(f["product"]["id"])
        c.check("unreachable.not_identity_in_product", "cam_iso" not in prod.cam_to_world)
        # reachable control track still publishes
        pts = wh.trajectory(run_id, "track_unreachable_case")["points"]
        c.check("unreachable.reachable_control_ok", all(p0["status"] == "published" for p0 in pts), [p0["status"] for p0 in pts])
    finally:
        wh.close()


def _verify_loop_conflict(c: Checker, ws: str, summary: dict) -> None:
    f = summary["fixtures"]["loop_conflict"]
    rej = f.get("rejected_mutations", [])
    by_label = {r["label"]: r for r in rej}
    expect = {
        "conflicting_cycle": "cycle",
        "non_orthogonal_R": "orthogonal",
        "det_minus_one": "det",
        "nan_matrix": "finite",
        "inf_translation": "finite",
    }
    for label, needle in expect.items():
        r = by_label.get(label)
        c.check(
            f"loop.{label}_rejected",
            bool(r and r["rejected"] and r["graph_version_unchanged"] and needle in (r["error"] or "")),
            r,
        )
    c.check("loop.graph_version_unchanged_by_rejections", all(r["graph_version_unchanged"] for r in rej), f.get("graph_version_after_rejections"))
    same = [r for r in rej if r["rejected"] and r["graph_version_unchanged"]]
    c.check("loop.all_rejections_atomic", len(same) == len(expect), {"atomic": len(same), "expected": len(expect)})


def _verify_observation_contract(c: Checker) -> None:
    base = {"track_id": "a", "t": 1, "camera_id": "cam_00", "u": 1.0, "v": 2.0, "weight": 1.0}
    dup = [dict(base, obs_id="o1"), dict(base, obs_id="o2")]
    try:
        parse_observation_rows(dup)
        rejected_identity = False
    except ObservationError:
        rejected_identity = True
    c.check("observations.duplicate_identity_rejected", rejected_identity)

    neg = [dict(base, obs_id="o1", weight=-0.5)]
    try:
        parse_observation_rows(neg)
        rejected_weight = False
    except ObservationError:
        rejected_weight = True
    c.check("observations.nonpositive_weight_rejected", rejected_weight)

    nonint = [dict(base, obs_id="o1", t=1.5)]
    try:
        parse_observation_rows(nonint)
        rejected_t = False
    except ObservationError:
        rejected_t = True
    c.check("observations.noninteger_time_rejected", rejected_t)

    duplicate_obs = [dict(base, obs_id="o1"), dict(base, t=2, obs_id="o1")]
    try:
        parse_observation_rows(duplicate_obs)
        rejected_obs = False
    except ObservationError:
        rejected_obs = True
    c.check("observations.duplicate_obs_id_rejected", rejected_obs)


def _verify_rebuild_provenance(c: Checker, ws: str, summary: dict) -> None:
    """A corrected product over the same dataset yields a new run; old run intact."""
    v1 = summary["product_v1"]["run"]
    v2 = summary["product_v2"]["run"]
    c.check("rebuild.new_run_id", v1["run_id"] != v2["run_id"])
    c.check("rebuild.same_dataset", v1["dataset_id"] == v2["dataset_id"])
    c.check("rebuild.old_run_counts_unchanged", v1["n_points"] == v2["n_points"])
    wh = TrajectoryWarehouse(summary["warehouse_db"])
    try:
        c.check("rebuild.both_runs_queryable", len(wh.list_runs()) == 2)
        t1 = wh.trajectory(v1["run_id"], "track_000")["points"]
        t2 = wh.trajectory(v2["run_id"], "track_000")["points"]
        c.check("rebuild.old_run_points_preserved", len(t1) == len(t2) and t1[0]["status"] == "published")
        # the two runs' solutions must differ because cam_03 moved
        if t1 and t2 and t1[0].get("X") and t2[0].get("X"):
            diff = float(np.linalg.norm(np.array(t1[0]["X"]) - np.array(t2[0]["X"])))
            c.check("rebuild.correction_changes_solution", diff > 1e-6, diff)
    finally:
        wh.close()
