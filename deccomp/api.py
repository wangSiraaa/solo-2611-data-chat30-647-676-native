"""对外门面：compile / run / explain / verify。

* compile：AST + schema -> 可保存的计划对象（JSON 序列化）。
* run：读取真实保存的计划，按批执行（行式或列式引擎），输出 Parquet 与
  独立错误清单，并回读核对；不同批次复用同一计划文件。
* explain：解释已保存计划。
* verify：运行内置测试套件（见 tests/）。
"""

import json
import os

from .errors import CompileError
from .explain import explain_plan
from .output import write_batch_output, read_back_and_verify
from .planner import Plan, compile_plan
from .row_engine import RowEngine
from .runtime import Batch
from .vector_engine import VectorEngine

# 对外重新导出，调用方可用 deccomp.api.CompileError 捕获计划级错误。
__all__ = ["CompileError"]

ENGINES = {"row": RowEngine, "vector": VectorEngine}


def compile(ast, schema, plan_path=None):
    plan = compile_plan(ast, schema)
    if plan_path is not None:
        plan.save(plan_path)
    return plan


def explain(plan_or_path) -> str:
    plan = _load_plan(plan_or_path)
    return explain_plan(plan)


def _load_plan(plan_or_path):
    if isinstance(plan_or_path, Plan):
        return plan_or_path
    return Plan.load(plan_or_path)


def run(plan_or_path, batches, out_dir, engine="vector",
        write_files=True):
    """执行一批或多批输入。

    ``batches`` 为 list[list[dict]]（每 dict 一行，列值为十进制文本或 None），
    或 list[str]（JSONL 文件路径）。返回每批的执行与回读核对摘要。
    """
    plan = _load_plan(plan_or_path)
    if engine not in ENGINES:
        raise ValueError(f"未知引擎 {engine}，可选 {sorted(ENGINES)}")
    executor = ENGINES[engine](plan)

    if write_files:
        os.makedirs(out_dir, exist_ok=True)
        # 执行前把计划真实落盘，证明执行器只依赖保存后的计划文件。
        plan_path = os.path.join(out_dir, "plan.json")
        plan.save(plan_path)

    reports = []
    if isinstance(batches, (list, tuple)) and batches and \
            isinstance(batches[0], dict):
        batches = [batches]

    for bi, item in enumerate(batches):
        if isinstance(item, str):
            batch = Batch.from_jsonl(item, plan)
        else:
            batch = Batch.from_rows(item, plan)

        results, errors = executor.execute(batch)

        report = {
            "batch_index": bi,
            "engine": engine,
            "nrows": batch.nrows,
            "n_ok": batch.nrows - len(errors),
            "n_errors": len(errors),
            "global_row_ids": [None] * batch.nrows,
        }
        if write_files:
            pq_path = os.path.join(out_dir, f"batch_{bi:04d}.parquet")
            err_path = os.path.join(out_dir, f"batch_{bi:04d}.errors.json")
            manifest = write_batch_output(
                pq_path, err_path, plan, batch, results, errors,
                engine, bi)
            ok, details = read_back_and_verify(
                pq_path, plan, results, errors)
            report["parquet"] = pq_path
            report["errors_file"] = err_path
            report["readback_ok"] = ok
            report["readback"] = details
            report["error_codes"] = [e["code"] for e in manifest["errors"]]
        else:
            report["results"] = results
            report["errors"] = errors
            report["error_codes"] = [exc.code for exc in errors.values()]
        reports.append(report)
    return reports


def verify(verbose=True):
    """运行内置 unittest 测试套件，返回是否全部通过。"""
    import unittest
    from .tests import suite as suite_pkg
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1)
    result = runner.run(suite_pkg.suite())
    return result.wasSuccessful()
