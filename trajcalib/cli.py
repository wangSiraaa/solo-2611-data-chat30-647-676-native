"""Command-line interface for the calibration graph and trajectory warehouse.

Commands
--------
generate   write all synthetic scenes under out/
build      build a graph for a named scene, publish a product, run warehouse
publish    publish a product from a graph database at a chosen world root
query      transform query between two frames using a product
trace      trace a stored point back to observations and product transforms
rebuild    recompute a run for a dataset under a (corrected) product
interp     ask for an interpolated position (or a refusal)
verify     run the independent verification suite
serve      run the local HTTP API
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .graph import (
    CalibrationGraphService,
    GraphError,
    load_product,
    save_product,
)
from .pipeline import build_graph_from_scene, run_main_pipeline
from .scene import build_all, write_scenes
from .warehouse import TrajectoryWarehouse, WarehouseError


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _get_scene(name: str):
    scenes = build_all()
    if name not in scenes:
        raise SystemExit(f"unknown scene {name!r}; choose from {sorted(scenes)}")
    return scenes[name]


def cmd_generate(args) -> None:
    paths = write_scenes(args.out)
    _print({name: {k: os.path.relpath(v, args.out) for k, v in p.items()} for name, p in paths.items()})


def cmd_build(args) -> None:
    summary = run_main_pipeline(args.workspace, args.min_eigen_ratio, args.max_gap)
    _print(summary)


def _graph_from_db(scene_name: str, db_path: str) -> CalibrationGraphService:
    scene = _get_scene(scene_name)
    return build_graph_from_scene(scene, db_path=db_path)


def cmd_publish(args) -> None:
    scene = _get_scene(args.scene)
    db_path = args.graph_db
    if db_path and os.path.exists(db_path):
        graph = CalibrationGraphService(db_path=db_path, tolerance=args.tolerance)
    else:
        graph = build_graph_from_scene(scene, db_path=db_path, tolerance=args.tolerance)
    product = graph.publish(args.product_id, world_root=args.world_root or scene.world_root)
    save_product(product, args.out)
    _print(
        {
            "product": args.out,
            "product_id": product.product_id,
            "graph_version": product.graph_version,
            "world_root": product.world_root,
            "calibrated_cameras": sorted(product.cam_to_world),
            "unreachable_cameras": product.unreachable_cameras,
            "integrity": product.integrity,
        }
    )


def cmd_query(args) -> None:
    if args.graph_db:
        graph = CalibrationGraphService(db_path=args.graph_db, tolerance=args.tolerance)
        T = graph.transform(args.src, args.dst)
        out = {
            "src": args.src,
            "dst": args.dst,
            "transform": T.as_dict(),
            "graph_version": graph.state.version,
            "source": "graph",
        }
        if args.point:
            x = [float(v) for v in args.point.split(",")]
            out["from_point"] = x
            out["to_point"] = [float(v) for v in T.apply(x)]
        _print(out)
        return

    if not args.product:
        raise SystemExit("query needs --product or --graph-db")
    product = load_product(args.product)
    # A product can only represent cameras and the world root directly; it
    # contains no link-frame poses. Reachability still decides validity.
    if args.src not in product.reachable_frames:
        raise SystemExit(f"frame {args.src!r} is not reachable in this product")
    if args.dst not in product.reachable_frames:
        raise SystemExit(f"frame {args.dst!r} is not reachable in this product")
    T_src_world = _frame_to_world(product, args.src)
    T_dst_world = _frame_to_world(product, args.dst)
    T = T_dst_world.inverse().compose(T_src_world)
    out = {
        "src": args.src,
        "dst": args.dst,
        "transform": T.as_dict(),
        "product_id": product.product_id,
        "world_root": product.world_root,
    }
    if args.point:
        x = [float(v) for v in args.point.split(",")]
        out["from_point"] = x
        out["to_point"] = [float(v) for v in T.apply(x)]
    _print(out)


def _frame_to_world(product, frame):
    from .geometry import RigidTransform

    if frame == product.world_root:
        return RigidTransform.identity()
    if frame in product.cam_to_world:
        return product.cam_to_world[frame]
    raise GraphError(
        f"frame {frame!r} has no stored transform in the product; "
        "only cameras and the world root are representable from a product alone"
    )


def cmd_trace(args) -> None:
    wh = TrajectoryWarehouse(args.warehouse)
    try:
        _print(wh.trace_point(args.run_id, args.track_id, args.t))
    finally:
        wh.close()


def cmd_interp(args) -> None:
    wh = TrajectoryWarehouse(args.warehouse)
    try:
        _print(wh.interpolate(args.run_id, args.track_id, args.t))
    finally:
        wh.close()


def cmd_rebuild(args) -> None:
    wh = TrajectoryWarehouse(args.warehouse)
    try:
        wh.register_product_file(args.product)
        product = load_product(args.product)
        run = wh.run(
            args.dataset_id,
            product.product_id,
            min_eigen_ratio=args.min_eigen_ratio,
            max_gap=args.max_gap,
        )
        _print({"run": run.as_dict(), "runs": wh.list_runs()})
    finally:
        wh.close()


def cmd_verify(args) -> None:
    from .verify import run_verification

    result = run_verification(args.workspace, min_eigen_ratio=args.min_eigen_ratio, max_gap=args.max_gap)
    _print(result)
    sys.exit(0 if result["ok"] else 1)


def cmd_serve(args) -> None:
    from .server import serve

    serve(args.workspace, host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trajcalib", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write synthetic scenes")
    g.add_argument("--out", default="out")
    g.set_defaults(func=cmd_generate)

    b = sub.add_parser("build", help="run the main end-to-end pipeline")
    b.add_argument("--workspace", default=".")
    b.add_argument("--min-eigen-ratio", type=float, default=1e-3)
    b.add_argument("--max-gap", type=int, default=1)
    b.set_defaults(func=cmd_build)

    pub = sub.add_parser("publish", help="publish a calibration product")
    pub.add_argument("--scene", default="main")
    pub.add_argument("--graph-db", default=None)
    pub.add_argument("--world-root", default=None)
    pub.add_argument("--product-id", required=True)
    pub.add_argument("--out", required=True)
    pub.add_argument("--tolerance", type=float, default=1e-9)
    pub.set_defaults(func=cmd_publish)

    q = sub.add_parser("query", help="query a transform from a product or graph")
    q.add_argument("--product", default=None, help="product JSON (cameras + world root)")
    q.add_argument("--graph-db", default=None, help="query any reachable frame pair from the graph")
    q.add_argument("--tolerance", type=float, default=1e-9)
    q.add_argument("--src", required=True)
    q.add_argument("--dst", required=True)
    q.add_argument("--point", default=None, help="x,y,z to transform")
    q.set_defaults(func=cmd_query)

    t = sub.add_parser("trace", help="trace a point to observations and transforms")
    t.add_argument("--warehouse", required=True)
    t.add_argument("--run-id", required=True)
    t.add_argument("--track-id", required=True)
    t.add_argument("--t", type=int, required=True)
    t.set_defaults(func=cmd_trace)

    i = sub.add_parser("interp", help="interpolate a trajectory position or refuse")
    i.add_argument("--warehouse", required=True)
    i.add_argument("--run-id", required=True)
    i.add_argument("--track-id", required=True)
    i.add_argument("--t", type=float, required=True)
    i.set_defaults(func=cmd_interp)

    r = sub.add_parser("rebuild", help="recompute a run under a new product")
    r.add_argument("--warehouse", required=True)
    r.add_argument("--dataset-id", required=True)
    r.add_argument("--product", required=True)
    r.add_argument("--min-eigen-ratio", type=float, default=1e-3)
    r.add_argument("--max-gap", type=int, default=1)
    r.set_defaults(func=cmd_rebuild)

    v = sub.add_parser("verify", help="run the independent verification suite")
    v.add_argument("--workspace", default=".")
    v.add_argument("--min-eigen-ratio", type=float, default=1e-3)
    v.add_argument("--max-gap", type=int, default=1)
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("serve", help="serve the local HTTP API")
    s.add_argument("--workspace", default=".")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (GraphError, WarehouseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
