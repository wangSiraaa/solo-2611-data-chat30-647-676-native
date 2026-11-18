"""应用门面：把目录、编译器、解算器、验证器组装成可重开的数据服务。

外部客户的三个独立面：
  * 目录面 get/list/snapshot —— 不依赖编译与解算即可查询版本；
  * 编译面 compile / load_artifact —— 产物自包含、可独立检查引用与方程；
  * 解算面 solve / query / explain —— 真实消费编译产物与目录快照。
"""

import os

from .catalog import Catalog
from .compiler import Compiler
from .errors import NotFoundError
from .solver import SolverService
from .verifier import verify_compile as _verify_compile
from .verifier import verify_solve as _verify_solve


class BudgetService:
    def __init__(self, data_root):
        self.data_root = os.path.abspath(data_root)
        self.catalog = Catalog(os.path.join(self.data_root, "catalog"))
        self.artifact_dir = os.path.join(self.data_root, "artifacts")
        self.report_dir = os.path.join(self.data_root, "reports")
        self.cache_dir = os.path.join(self.data_root, "cache")
        self.compiler = Compiler(self.catalog, self.artifact_dir)
        self.solver = SolverService(
            self.catalog, self.artifact_dir, self.report_dir, self.cache_dir
        )

    # ------------------------------------------------------------- 目录面
    def put_model(self, payload):
        return self.catalog.put_model(payload)

    def list_models(self):
        return self.catalog.list_models()

    def get_model(self, model_id):
        return self.catalog.get_model_meta(model_id)

    def list_revisions(self, model_id):
        return self.catalog.list_revisions(model_id)

    def snapshot(self, model_id, label=None):
        return self.catalog.take_snapshot(model_id, label=label)

    def list_snapshots(self, model_id):
        return self.catalog.list_snapshots(model_id)

    def get_snapshot(self, snap_id):
        return self.catalog.get_snapshot(snap_id)

    def catalog_descriptor(self):
        return self.catalog.catalog_snapshot_descriptor()

    # ------------------------------------------------------------- 编译面
    def compile(self, root_snap_id, label=None):
        artifact, path = self.compiler.compile_snapshot(root_snap_id, label=label)
        return artifact, path

    def load_artifact(self, path_or_digest):
        if os.path.exists(path_or_digest):
            return Compiler.load_artifact(path_or_digest)
        digest = path_or_digest.split(":", 1)[-1]
        path = os.path.join(self.artifact_dir, f"{digest[:12]}.json")
        if not os.path.exists(path):
            raise NotFoundError(f"编译产物 {path_or_digest} 找不到")
        return Compiler.load_artifact(path)

    # ------------------------------------------------------------- 解算面
    def solve(self, artifact, solve_config=None):
        return self.solver.solve(artifact, solve_config)

    def query(self, solve_id, variable):
        return self.solver.query(solve_id, variable)

    def explain(self, solve_id, variable):
        return self.solver.explain(solve_id, variable)

    def get_report(self, solve_id):
        return self.solver.get_report(solve_id)

    def list_reports(self):
        if not os.path.isdir(self.report_dir):
            return []
        return sorted(
            f[:-5]
            for f in os.listdir(self.report_dir)
            if f.startswith("solve") and f.endswith(".json")
        )

    # ------------------------------------------------------------- 验证面
    def verify_compile(self, artifact):
        return _verify_compile(self.catalog, artifact)

    def verify_solve(self, solve_id):
        report = self.get_report(solve_id)
        artifact = self.load_artifact(report["artifact_hash"])
        return _verify_solve(self.catalog, artifact, report)
