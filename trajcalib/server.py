"""Local HTTP API for the calibration graph service and trajectory warehouse.

Run with ``python -m trajcalib serve``. Only the Python standard library is
used. Every mutation is validated atomically by ``CalibrationGraphService``;
rejected requests return HTTP 409/422 with the reason and leave the graph
version unchanged.

The warehouse endpoints *only* accept product ids registered from external
product JSON; they never read graph edges.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .graph import (
    CalibrationGraphService,
    CameraIntrinsics,
    GraphError,
    load_product,
    save_product,
)
from .observations import parse_observation_rows
from .scene import build_all
from .warehouse import TrajectoryWarehouse, WarehouseError


class ServiceState:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        db_dir = os.path.join(workspace, "db")
        os.makedirs(db_dir, exist_ok=True)
        out_dir = os.path.join(workspace, "out")
        scenes = build_all()
        self.scene = scenes["main"]
        graph_path = os.path.join(db_dir, "service_graph.sqlite")
        self.graph = CalibrationGraphService(tolerance=1e-9, db_path=graph_path)
        if not self.graph.state.frames:
            self.graph.add_frames(
                [
                    (
                        fid,
                        CameraIntrinsics(
                            fx=self.scene.frames[fid]["intrinsics"]["fx"],
                            fy=self.scene.frames[fid]["intrinsics"]["fy"],
                            cx=self.scene.frames[fid]["intrinsics"]["cx"],
                            cy=self.scene.frames[fid]["intrinsics"]["cy"],
                        )
                        if self.scene.frames[fid]["intrinsics"]
                        else None,
                    )
                    for fid in sorted(self.scene.frames)
                ]
            )
            for e in self.scene.edges:
                self.graph.add_edge(e["edge_id"], e["src"], e["dst"], e["transform"])
        self.warehouse = TrajectoryWarehouse(os.path.join(db_dir, "service_warehouse.sqlite"))
        self.products_dir = os.path.join(out_dir, "service_products")
        os.makedirs(self.products_dir, exist_ok=True)
        self.lock = threading.Lock()

    def publish(self, product_id: str, world_root: str) -> dict:
        product = self.graph.publish(product_id, world_root=world_root)
        path = os.path.join(self.products_dir, f"{product_id}.json")
        save_product(product, path)
        self.warehouse.register_product(product)
        return {"product_id": product_id, "path": path, **product.as_dict()}


def _json_response(handler, code: int, body: dict) -> None:
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def make_handler(state: ServiceState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "trajcalib/1.0"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _read_json(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self):
            parsed = urlparse(self.path)
            qs = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            path = parsed.path.rstrip("/") or "/"
            try:
                with state.lock:
                    body = self._get(path, qs)
                if body is None:
                    _json_response(self, 404, {"error": f"no route {path}"})
                else:
                    _json_response(self, 200, body)
            except (GraphError, WarehouseError, KeyError, ValueError) as exc:
                _json_response(self, 422, {"error": str(exc)})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                body = self._read_json()
                with state.lock:
                    result = self._post(path, body)
                _json_response(self, 200, result)
            except (GraphError, WarehouseError, KeyError, ValueError) as exc:
                _json_response(self, 409 if "edge" in path or "graph" in path else 422, {"error": str(exc)})

        # ---- routes ---------------------------------------------------
        def _get(self, path, qs):
            if path == "/health":
                return {"ok": True, "graph_version": state.graph.state.version}
            if path == "/frames":
                return {
                    "frames": {
                        fid: {
                            "intrinsics": f.intrinsics.as_dict() if f.intrinsics else None,
                            "is_camera": f.is_camera,
                        }
                        for fid, f in sorted(state.graph.state.frames.items())
                    },
                    "graph_version": state.graph.state.version,
                }
            if path == "/graph/components":
                report = state.graph.validate()
                return {
                    "consistent": report.ok,
                    "components": report.components,
                    "conflicts": report.conflicts,
                    "graph_version": state.graph.state.version,
                }
            if path == "/graph/edges":
                return {
                    "edges": {eid: e.as_dict() for eid, e in sorted(state.graph.state.edges.items())},
                    "graph_version": state.graph.state.version,
                }
            if path == "/transform":
                T = state.graph.transform(qs["src"], qs["dst"])
                return {"src": qs["src"], "dst": qs["dst"], "transform": T.as_dict(),
                        "graph_version": state.graph.state.version}
            if path == "/products":
                return {"products": state.warehouse.list_products()}
            if path == "/warehouse/runs":
                return {"runs": state.warehouse.list_runs()}
            if path == "/warehouse/point":
                return state.warehouse.get_point(qs["run_id"], qs["track_id"], int(qs["t"]))
            if path == "/warehouse/trace":
                return state.warehouse.trace_point(qs["run_id"], qs["track_id"], int(qs["t"]))
            if path == "/warehouse/trajectory":
                return state.warehouse.trajectory(qs["run_id"], qs["track_id"])
            if path == "/warehouse/interp":
                return state.warehouse.interpolate(qs["run_id"], qs["track_id"], float(qs["t"]))
            if path == "/warehouse/tracks":
                return {"tracks": state.warehouse.list_tracks(qs["run_id"])}
            return None

        def _post(self, path, body):
            if path == "/graph/edges":
                ops = body.get("ops", [body])
                version = state.graph.apply_batch(ops, note="http")
                return {"graph_version": version}
            if path == "/publish":
                return state.publish(body["product_id"], body.get("world_root") or state.scene.world_root)
            if path == "/warehouse/register-product":
                product = load_product(body["path"])
                state.warehouse.register_product(product)
                return {"product_id": product.product_id, "integrity": product.integrity}
            if path == "/warehouse/ingest":
                rows = body.get("observations")
                if rows is None and body.get("path"):
                    from .observations import load_observations_jsonl

                    observations = load_observations_jsonl(body["path"])
                else:
                    observations = parse_observation_rows(rows or [])
                dataset_id = state.warehouse.ingest_dataset(observations, path=body.get("path"))
                return {"dataset_id": dataset_id, "n_observations": len(observations)}
            if path == "/warehouse/run":
                run = state.warehouse.run(
                    body["dataset_id"],
                    body["product_id"],
                    min_eigen_ratio=float(body.get("min_eigen_ratio", 1e-3)),
                    max_gap=int(body.get("max_gap", 1)),
                )
                return run.as_dict()
            raise ValueError(f"no route {path}")

    return Handler


def serve(workspace: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    state = ServiceState(workspace)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"trajcalib service listening on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        state.warehouse.close()
