import json
import os
import sqlite3

import numpy as np
import pytest

from trajcalib.geometry import RigidTransform, validate_transform, GeometryError
from trajcalib.graph import CalibrationGraphService, CameraIntrinsics, GraphError
from trajcalib.observations import ObservationError, parse_observation_rows
from trajcalib.pipeline import run_main_pipeline
from trajcalib.scene import build_all, build_behind_fixture, build_near_fixture, build_parallel_fixture
from trajcalib.warehouse import TrajectoryWarehouse


def _cam(fx=800.0, fy=800.0, cx=640.0, cy=360.0):
    return CameraIntrinsics(fx=fx, fy=fy, cx=cx, cy=cy)


# ----------------------------------------------------------------------
# geometry / graph
# ----------------------------------------------------------------------
def test_validate_rejects_bad_matrices():
    tol = 1e-9
    with pytest.raises(GeometryError):
        validate_transform(RigidTransform(np.eye(3) * 2.0, np.zeros(3)), tol)
    R_mirror = np.diag([1.0, 1.0, -1.0])
    with pytest.raises(GeometryError, match="det"):
        validate_transform(RigidTransform(R_mirror, np.zeros(3)), tol)
    with pytest.raises(GeometryError):
        validate_transform(RigidTransform(np.eye(3), np.array([np.nan, 0, 0])), tol)


def test_add_remove_replace_and_queries():
    g = CalibrationGraphService(tolerance=1e-9)
    g.add_frames([("world", None), ("cam", _cam())])
    T = RigidTransform.from_axis_angle([0, 0, 1], 0.3, [1.0, 2.0, 3.0])
    g.add_edge("e1", "cam", "world", T)
    got = g.transform("cam", "world")
    assert np.allclose(got.R, T.R) and np.allclose(got.t, T.t)
    T2 = RigidTransform.from_axis_angle([0, 1, 0], 0.1, [0.0, 0.0, 1.0])
    g.replace_edge("e1", T2)
    assert np.allclose(g.transform("cam", "world").t, T2.t)
    g.remove_edge("e1")
    with pytest.raises(GraphError, match="not connected"):
        g.transform("cam", "world")


def test_inconsistent_cycle_rejected_atomically():
    g = CalibrationGraphService(tolerance=1e-9)
    g.add_frames([("a", None), ("b", None), ("c", None)])
    g.add_edge("e1", "a", "b", RigidTransform.identity())
    g.add_edge("e2", "b", "c", RigidTransform.identity())
    v = g.state.version
    bad = RigidTransform.from_axis_angle([0, 0, 1], 0.2, [0.0, 0.0, 0.0])
    with pytest.raises(GraphError, match="inconsistent cycle"):
        g.add_edge("e3", "a", "c", bad)
    assert g.state.version == v
    assert "e3" not in g.state.edges


def test_duplicate_ids_rejected():
    g = CalibrationGraphService()
    g.add_frame("a")
    with pytest.raises(GraphError, match="duplicate"):
        g.add_frame("a")


def test_publish_lists_unreachable_without_identity():
    scene = build_all()["unreachable"]
    from trajcalib.pipeline import build_graph_from_scene

    g = build_graph_from_scene(scene)
    p = g.publish("p", world_root="world")
    assert "cam_iso" in p.unreachable_cameras
    assert "cam_iso" not in p.cam_to_world
    assert p.verify()


def test_parallel_paths_agree_for_any_pair():
    scene = build_all()["main"]
    from trajcalib.pipeline import build_graph_from_scene

    g = build_graph_from_scene(scene)
    Tab = g.transform("link_01", "link_09")
    Tbc = g.transform("link_09", "cam_04")
    Tac = g.transform("link_01", "cam_04")
    comp = Tbc.compose(Tab)
    assert np.allclose(comp.R, Tac.R, atol=1e-9)
    assert np.allclose(comp.t, Tac.t, atol=1e-9)


# ----------------------------------------------------------------------
# observations
# ----------------------------------------------------------------------
def test_observation_contract():
    row = {"track_id": "x", "t": 0, "camera_id": "c", "u": 1.0, "v": 2.0, "weight": 1.0}
    with pytest.raises(ObservationError):
        parse_observation_rows([row, dict(row, weight=2.0)])  # duplicate identity
    with pytest.raises(ObservationError):
        parse_observation_rows([dict(row, weight=0.0)])
    with pytest.raises(ObservationError):
        parse_observation_rows([dict(row, t=0.5)])
    with pytest.raises(ObservationError):
        parse_observation_rows([dict(row, u=float("nan"))])


# ----------------------------------------------------------------------
# solver behaviour via fixtures
# ----------------------------------------------------------------------
def _run_fixture(scene, tmp_path, min_eigen_ratio=1e-3, max_gap=1):
    from trajcalib.pipeline import build_graph_from_scene

    g = build_graph_from_scene(scene, db_path=str(tmp_path / "g.sqlite"))
    p = g.publish("p", world_root=scene.world_root)
    path = tmp_path / "p.json"
    from trajcalib.graph import save_product

    save_product(p, str(path))
    wh = TrajectoryWarehouse(str(tmp_path / "w.sqlite"))
    wh.register_product(p)
    ds = wh.ingest_dataset(parse_rows(scene.observations))
    run = wh.run(ds, p.product_id, min_eigen_ratio=min_eigen_ratio, max_gap=max_gap)
    return wh, run


def parse_rows(rows):
    return parse_observation_rows(rows)


def test_parallel_fixture_degenerate(tmp_path):
    scene = build_parallel_fixture()
    wh, run = _run_fixture(scene, tmp_path)
    try:
        pts = wh.trajectory(run.run_id, "track_parallel")["points"]
        assert pts and all(p["status"] == "degenerate" for p in pts)
        assert all(p["rank"] < 3 for p in pts)
        assert all(p["X"] is None for p in pts)
    finally:
        wh.close()


def test_near_fixture_degenerate(tmp_path):
    scene = build_near_fixture()
    wh, run = _run_fixture(scene, tmp_path)
    try:
        pts = wh.trajectory(run.run_id, "track_near")["points"]
        assert pts and all(p["status"] == "degenerate" for p in pts)
        assert all(p["eigen_ratio"] < p["min_eigen_ratio_threshold"] for p in pts)
    finally:
        wh.close()


def test_behind_fixture_inconsistent_and_retained(tmp_path):
    scene = build_behind_fixture()
    wh, run = _run_fixture(scene, tmp_path)
    try:
        pts = wh.trajectory(run.run_id, "track_behind")["points"]
        assert pts and all(p["status"] == "inconsistent" for p in pts)
        tr = wh.trace_point(run.run_id, "track_behind", pts[0]["t"])
        assert len(tr["cameras"]) == 3
        behind = [c for c in tr["cameras"] if c["z_positive"] is False]
        assert [c["camera_id"] for c in behind] == ["cam_behind"]
        assert wh.interpolate(run.run_id, "track_behind", 1.5)["available"] is False
    finally:
        wh.close()


def test_threshold_is_configurable(tmp_path):
    """A lower threshold publishes the near-degenerate fixture; evidence differs."""
    scene = build_near_fixture()
    wh, run = _run_fixture(scene, tmp_path, min_eigen_ratio=1e-12)
    try:
        pts = wh.trajectory(run.run_id, "track_near")["points"]
        assert pts and all(p["status"] == "published" for p in pts)
        # and the geometry is still numerically correct at that threshold
        gt = scene.ground_truth["tracks"]["track_near"]["points"]
        for p, g in zip(pts, gt):
            assert np.linalg.norm(np.array(p["X"]) - np.array(g["X"])) < 1e-6
    finally:
        wh.close()


# ----------------------------------------------------------------------
# end-to-end pipeline / warehouse
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    ws = tmp_path_factory.mktemp("ws")
    summary = run_main_pipeline(str(ws))
    return ws, summary


def test_end_to_end_two_runs(pipeline):
    ws, summary = pipeline
    assert summary["product_v1"]["run"]["dataset_id"] == summary["product_v2"]["run"]["dataset_id"]
    assert summary["product_v1"]["run"]["run_id"] != summary["product_v2"]["run"]["run_id"]
    wh = TrajectoryWarehouse(summary["warehouse_db"])
    try:
        assert len(wh.list_runs()) == 2
        p1 = wh.get_product(summary["product_v1"]["id"])
        p2 = wh.get_product(summary["product_v2"]["id"])
        assert p1.integrity != p2.integrity
    finally:
        wh.close()


def test_run1_has_larger_residual_than_run2(pipeline):
    ws, summary = pipeline
    con = sqlite3.connect(summary["warehouse_db"])
    try:
        r1 = con.execute(
            "SELECT MAX(pixel_residual) FROM point_cameras WHERE run_id=? AND camera_id='cam_03' AND pixel_residual IS NOT NULL",
            (summary["product_v1"]["run"]["run_id"],),
        ).fetchone()[0]
        r2 = con.execute(
            "SELECT MAX(pixel_residual) FROM point_cameras WHERE run_id=? AND camera_id='cam_03' AND pixel_residual IS NOT NULL",
            (summary["product_v2"]["run"]["run_id"],),
        ).fetchone()[0]
    finally:
        con.close()
    assert r1 > 1.0
    assert r2 < 1e-7


def test_ground_truth_accuracy(pipeline):
    ws, summary = pipeline
    gt = json.load(open(os.path.join(str(ws), "out", "main", "ground_truth.json")))
    wh = TrajectoryWarehouse(summary["warehouse_db"])
    try:
        run = summary["product_v2"]["run"]["run_id"]
        for tid, t in (("track_000", 0), ("track_050", 50), ("track_099", 99)):
            p = wh.get_point(run, tid, t)
            assert p["status"] == "published"
            g = next(q for q in gt["tracks"][tid]["points"] if q["t"] == t)
            assert np.linalg.norm(np.array(p["X"]) - np.array(g["X"])) < 1e-7
    finally:
        wh.close()


def test_time_gap_blocks_interpolation(pipeline):
    ws, summary = pipeline
    wh = TrajectoryWarehouse(summary["warehouse_db"])
    try:
        run = summary["product_v2"]["run"]["run_id"]
        r = wh.interpolate(run, "track_gap", 20.0)
        assert r["available"] is False
        assert "gap" in r["reason"]
        # within a valid stretch interpolation is allowed and matches the line
        ok = wh.interpolate(run, "track_gap", 0.5)
        assert ok["available"] is True
        p0 = wh.get_point(run, "track_gap", 0)["X"]
        p1 = wh.get_point(run, "track_gap", 1)["X"]
        expect = (np.array(p0) + np.array(p1)) / 2.0
        assert np.allclose(ok["X"], expect, atol=1e-9)
    finally:
        wh.close()


def test_warehouse_does_not_read_graph_db(pipeline, tmp_path):
    ws, summary = pipeline
    # copy warehouse db alone to an isolated dir; no graph db present
    import shutil

    isolated = tmp_path / "iso.sqlite"
    shutil.copy(summary["warehouse_db"], isolated)
    wh = TrajectoryWarehouse(str(isolated))
    try:
        trace = wh.trace_point(summary["product_v2"]["run"]["run_id"], "track_010", 3)
        assert trace["product_id"] == summary["product_v2"]["id"]
        assert trace["cameras"]
    finally:
        wh.close()


def test_bad_camera_in_dataset_rejected(tmp_path):
    scene = build_all()["main"]
    from trajcalib.pipeline import build_graph_from_scene

    g = build_graph_from_scene(scene)
    p = g.publish("p", world_root="world")
    wh = TrajectoryWarehouse(str(tmp_path / "w.sqlite"))
    try:
        wh.register_product(p)
        bad = parse_observation_rows([
            {"track_id": "z", "t": 0, "camera_id": "cam_99", "u": 1, "v": 1, "weight": 1.0},
        ])
        ds = wh.ingest_dataset(bad)
        with pytest.raises(Exception):
            wh.run(ds, p.product_id)
    finally:
        wh.close()
