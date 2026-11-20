"""End-to-end pipeline: generate, build graph, publish, ingest, run, verify."""

from __future__ import annotations

import json
import os

from .graph import (
    CalibrationGraphService,
    GraphError,
    CameraIntrinsics,
    save_product,
)
from .scene import build_all, write_scenes
from .warehouse import TrajectoryWarehouse

DEFAULT_TOL = 1e-9


def build_graph_from_scene(scene, db_path: str | None = None, tolerance: float = DEFAULT_TOL) -> CalibrationGraphService:
    svc = CalibrationGraphService(tolerance=tolerance, db_path=db_path)
    frames = [
        (
            fid,
            CameraIntrinsics(
                fx=scene.frames[fid]["intrinsics"]["fx"],
                fy=scene.frames[fid]["intrinsics"]["fy"],
                cx=scene.frames[fid]["intrinsics"]["cx"],
                cy=scene.frames[fid]["intrinsics"]["cy"],
            )
            if scene.frames[fid]["intrinsics"]
            else None,
        )
        for fid in sorted(scene.frames)
    ]
    svc.add_frames(frames)
    for e in scene.edges:
        svc.add_edge(e["edge_id"], e["src"], e["dst"], e["transform"])
    return svc


def _product_paths(out_dir: str, scene_name: str) -> tuple[str, str]:
    base = os.path.join(out_dir, scene_name, "products")
    os.makedirs(base, exist_ok=True)
    return (
        os.path.join(base, "calibration_v1.json"),
        os.path.join(base, "calibration_v2.json"),
    )


def run_main_pipeline(workspace: str, min_eigen_ratio: float = 1e-3, max_gap: int = 1) -> dict:
    out_dir = os.path.join(workspace, "out")
    db_dir = os.path.join(workspace, "db")
    os.makedirs(db_dir, exist_ok=True)
    scenes = build_all()
    paths = write_scenes(out_dir, scenes)
    main = scenes["main"]

    # ---- graph + product v1 (with the stale cam_03 edge) -----------------
    graph_db = os.path.join(db_dir, "calibration_graph.sqlite")
    if os.path.exists(graph_db):
        os.remove(graph_db)
    graph = build_graph_from_scene(main, db_path=graph_db)
    report = graph.validate()
    if not report.ok:
        raise GraphError("main graph failed consistency validation")

    p1_path, p2_path = _product_paths(out_dir, "main")
    product_v1 = graph.publish("main-v1", world_root=main.world_root)
    save_product(product_v1, p1_path)

    # ---- warehouse: ingest observations once -----------------------------
    wh_path = os.path.join(db_dir, "trajectory_warehouse.sqlite")
    if os.path.exists(wh_path):
        os.remove(wh_path)
    wh = TrajectoryWarehouse(wh_path)
    # The warehouse is fed the *external product file*, never the live graph
    # object, so it demonstrably cannot read graph edges.
    product_v1 = wh.register_product_file(p1_path)
    dataset_id = wh.ingest_jsonl(paths["main"]["observations"])
    run1 = wh.run(dataset_id, product_v1.product_id, min_eigen_ratio=min_eigen_ratio, max_gap=max_gap)

    # ---- apply correction from external bundle ---------------------------
    with open(paths["main"]["corrections"], "r", encoding="utf-8") as fh:
        corrections = json.load(fh)["operations"]
    graph.apply_batch(corrections, note="apply external correction bundle")
    product_v2 = graph.publish("main-v2", world_root=main.world_root)
    save_product(product_v2, p2_path)
    wh.register_product_file(p2_path)
    run2 = wh.run(dataset_id, product_v2.product_id, min_eigen_ratio=min_eigen_ratio, max_gap=max_gap)

    # ---- fixtures ---------------------------------------------------------
    fixture_results: dict[str, dict] = {}
    for name in ("parallel", "near", "behind", "unreachable", "loop_conflict"):
        fixture_results[name] = _run_fixture(workspace, scenes[name], out_dir, db_dir, min_eigen_ratio, max_gap)

    wh.close()

    summary = {
        "dataset_id": dataset_id,
        "graph_db": graph_db,
        "warehouse_db": wh_path,
        "graph_versions": graph.state.version,
        "product_v1": {"id": product_v1.product_id, "path": p1_path, "run": run1.as_dict()},
        "product_v2": {"id": product_v2.product_id, "path": p2_path, "run": run2.as_dict()},
        "fixtures": fixture_results,
        "world_root": main.world_root,
        "min_eigen_ratio": min_eigen_ratio,
        "max_gap": max_gap,
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "pipeline_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")
    _write_run_report(out_dir, summary, wh_path)
    return summary


def _write_run_report(out_dir: str, summary: dict, wh_path: str) -> None:
    """Human-readable evidence report of the two runs and boundary tracks."""
    from .warehouse import TrajectoryWarehouse

    wh = TrajectoryWarehouse(wh_path)
    try:
        report = {
            "dataset_id": summary["dataset_id"],
            "runs": wh.list_runs(),
            "products": wh.list_products(),
            "boundary_tracks": {},
        }
        for run_key, run_id in (
            ("v1", summary["product_v1"]["run"]["run_id"]),
            ("v2", summary["product_v2"]["run"]["run_id"]),
        ):
            entries = {}
            for tid in ("track_gap", "track_single", "track_000"):
                data = wh.trajectory(run_id, tid)
                entries[tid] = {
                    "points": [
                        {"t": p["t"], "status": p["status"], "X": p.get("X"), "rank": p["rank"], "eigen_ratio": p["eigen_ratio"]}
                        for p in data["points"]
                    ],
                    "segments": data["segments"],
                }
            report["boundary_tracks"][run_key] = entries
        with open(os.path.join(out_dir, "run_report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")
    finally:
        wh.close()


def _run_fixture(
    workspace: str,
    scene,
    out_dir: str,
    db_dir: str,
    min_eigen_ratio: float,
    max_gap: int,
) -> dict:
    name = scene.name
    base = os.path.join(out_dir, name)
    obs_path = os.path.join(base, "observations.jsonl")
    graph_db = os.path.join(db_dir, f"graph_{name}.sqlite")
    if os.path.exists(graph_db):
        os.remove(graph_db)
    graph = build_graph_from_scene(scene, db_path=graph_db)
    report = graph.validate()
    result: dict = {"graph_consistent": report.ok, "components": report.components}

    product = graph.publish(f"{name}-v1", world_root=scene.world_root)
    prod_path = os.path.join(base, "products", "calibration_v1.json")
    os.makedirs(os.path.dirname(prod_path), exist_ok=True)
    save_product(product, prod_path)
    result["product"] = {
        "id": product.product_id,
        "path": prod_path,
        "reachable_frames": product.reachable_frames,
        "unreachable_cameras": product.unreachable_cameras,
        "n_calibrated_cameras": len(product.cam_to_world),
    }

    wh_path = os.path.join(db_dir, f"warehouse_{name}.sqlite")
    if os.path.exists(wh_path):
        os.remove(wh_path)
    wh = TrajectoryWarehouse(wh_path)
    wh.register_product_file(prod_path)
    dataset_id = wh.ingest_jsonl(obs_path)
    run = wh.run(dataset_id, product.product_id, min_eigen_ratio=min_eigen_ratio, max_gap=max_gap)
    result["run"] = run.as_dict()
    result["warehouse_db"] = wh_path

    tracks = wh.list_tracks(run.run_id)
    result["tracks"] = {tr["track_id"]: tr for tr in tracks}

    for tid in scene.track_truth:
        det = []
        for p in wh.trajectory(run.run_id, tid)["points"]:
            det.append(
                {
                    "t": p["t"],
                    "status": p["status"],
                    "X": p.get("X"),
                    "rank": p["rank"],
                    "eigen_ratio": p["eigen_ratio"],
                    "min_eigen_ratio_threshold": p["min_eigen_ratio_threshold"],
                    "n_cameras": p["n_cameras"],
                    "rationale": p["rationale"],
                }
            )
        result.setdefault("point_status", {})[tid] = det

    # rejected mutation evidence (loop fixture)
    if scene.extras.get("rejected_mutations"):
        rejected = []
        version_before = graph.state.version
        for case in scene.extras["rejected_mutations"]:
            try:
                graph.apply_batch(case["ops"], note=f"rejected {case['label']}")
                rejected.append({"label": case["label"], "rejected": False, "error": None})
            except GraphError as exc:
                rejected.append(
                    {
                        "label": case["label"],
                        "rejected": True,
                        "error": str(exc),
                        "graph_version_unchanged": graph.state.version == version_before,
                    }
                )
        result["rejected_mutations"] = rejected
        result["graph_version_after_rejections"] = graph.state.version

    wh.close()
    return result
