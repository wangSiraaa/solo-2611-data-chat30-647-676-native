"""可复跑的 CLI 端到端演示：在临时数据目录上走完全部入口。

运行：python -m tests.demo_cli
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

from . import scenarios


def run_cli(data_root, *args, stdin_payload=None, expect_fail=False):
    cmd = [sys.executable, "-m", "budget_solver.cli", "--data-root", data_root, *args]
    payload = json.dumps(stdin_payload, ensure_ascii=False) if stdin_payload is not None else None
    proc = subprocess.run(
        cmd, input=payload, capture_output=True, text=True, check=False,
    )
    if expect_fail:
        assert proc.returncode != 0, f"本应失败却成功：{args}"
        return json.loads(proc.stderr)
    assert proc.returncode == 0, f"命令失败 {args}\nSTDERR={proc.stderr}"
    return json.loads(proc.stdout)


def main():
    data_root = tempfile.mkdtemp(prefix="budget-demo-")
    try:
        # 1. put-model：两个独立模型 + 一个跨模型下游
        run_cli(data_root, "put-model", "-", stdin_payload=scenarios.two_cell_unique_cycle())
        run_cli(data_root, "put-model", "-", stdin_payload=scenarios.model_a_simple())
        run_cli(data_root, "put-model", "-", stdin_payload=scenarios.model_b_referencing_a())

        # 非法引用整次修订拒绝（CLI 返回非零 + 结构化错误）
        rejected = run_cli(
            data_root, "put-model", "-",
            stdin_payload=scenarios.bad_model_good_neighbor(), expect_fail=True,
        )
        assert rejected["error"] == "ReferenceRejectedError"
        print("1. put-model 正常发布 3 个模型；非法引用被整次拒绝：", rejected["details"])

        # 目录面独立查询版本
        models = run_cli(data_root, "list-models")
        assert len(models) == 3
        print("2. 目录独立查询：", [(m["model_id"], m["head_rev"]) for m in models])

        # 2. snapshot + 3. compile
        snap = run_cli(data_root, "snapshot", "B_BUD", "--label", "demo")
        compiled = run_cli(data_root, "compile", snap["snap_id"])
        assert compiled["variable_count"] == 4
        cyclic = [c for c in compiled["components"] if c["cyclic"]]
        print("3. 编译产物：", compiled["variable_count"], "变量、",
              len(compiled["components"]), "个 SCC（循环 SCC",
              len(cyclic), "个），引用与方程已独立核对")

        # 编译产物可独立检查
        verify_c = run_cli(data_root, "verify-compile", compiled["artifact_hash"])
        assert verify_c["ok"]

        # 4. solve
        solved = run_cli(data_root, "solve", compiled["artifact_hash"])
        solve_id = solved["solve_id"]
        print("4. 解算报告", solve_id, "分区：",
              {k: len(v) for k, v in solved["sections"].items()})

        # 5. query
        q = run_cli(data_root, "query", solve_id, "B_BUD::f")
        print("5. query B_BUD::f =", q["value"], "；实际依赖", len(q["actual_dependencies"]), "格")

        # 6. explain
        ex = run_cli(data_root, "explain", solve_id, "B_BUD::g")
        print("6. explain B_BUD::g =", ex["value"], "；闭包方程", len(ex["closure_equations"]), "条")

        # 7. verify-solve（独立代回）
        vs = run_cli(data_root, "verify-solve", solve_id)
        print("7. 独立验证解算报告：", vs["check_count"], "项检查通过")

        # 矛盾模型：失败输出给来源，不影响其他模型
        run_cli(data_root, "put-model", "-", stdin_payload=scenarios.contradictory_cycle())
        csnap = run_cli(data_root, "snapshot", "M_CONTRA")
        ccomp = run_cli(data_root, "compile", csnap["snap_id"])
        csolved = run_cli(data_root, "solve", ccomp["artifact_hash"])
        cq = run_cli(data_root, "query", csolved["solve_id"], "M_CONTRA::a")
        assert cq["status"] == "inconsistent"
        cex = run_cli(data_root, "explain", csolved["solve_id"], "M_CONTRA::a")
        print("8. 矛盾输出：", cq["status"],
              "；证书", cex["contradictions"][0]["residual_lhs"], "=",
              cex["contradictions"][0]["residual_rhs"], "；携带",
              len(cex["failure_source"]["equations"]), "条来源方程")
        run_cli(data_root, "verify-solve", csolved["solve_id"])

        # 重开：新进程读同一数据目录，报告哈希不变
        reports = run_cli(data_root, "list-reports")
        reopened = run_cli(data_root, "get-report", solve_id)
        assert reopened["report_hash"] == solved["report_hash"]
        print("9. 重开复读：报告", reports, "哈希一致，缓存与报告均持久化")

        print("\nCLI 端到端演示全部通过。数据目录：", data_root)
    finally:
        shutil.rmtree(data_root)


if __name__ == "__main__":
    main()
