"""端到端测试：六类规定案例 + 手算断言 + 目录隔离 + 重开复读 + 缓存行为。

运行：python -m tests.test_service（在仓库根目录）。
"""

import shutil
import tempfile
from fractions import Fraction

from budget_solver.compiler import var_name
from budget_solver.errors import (
    NotFoundError,
    ReferenceRejectedError,
    ValidationError,
)
from budget_solver.service import BudgetService

from . import scenarios


PASS = []


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    PASS.append(label)


def fresh_service():
    tmp = tempfile.mkdtemp(prefix="budget-test-")
    return BudgetService(tmp), tmp


def publish_and_compile(service, payload, label=None):
    """put-model -> 固定快照 -> 编译，返回 (artifact, put_result)。"""
    put = service.put_model(payload)
    snap = service.snapshot(payload["model_id"], label=label)
    artifact, _ = service.compile(snap["snap_id"], label=label)
    return artifact, put, snap


# ----------------------------------------------------------- 案例 1：唯一循环
def test_unique_cycle():
    service, tmp = fresh_service()
    try:
        artifact, _, _ = publish_and_compile(service, scenarios.two_cell_unique_cycle())
        comps = [c for c in artifact["components"] if c["cyclic"]]
        check(len(comps) == 1 and set(comps[0]["variables"]) ==
              {"M_UNIQUE::x", "M_UNIQUE::y"}, "唯一循环被识别为单个循环 SCC")
        report = service.solve(artifact)
        qx = service.query(report["solve_id"], "M_UNIQUE::x")
        qy = service.query(report["solve_id"], "M_UNIQUE::y")
        check(qx["status"] == "determined" and qx["value"] == "2/3",
              "唯一循环 x 手算值 2/3")
        check(qy["status"] == "determined" and qy["value"] == "2/3",
              "唯一循环 y 手算值 2/3")
        check(set(qx["actual_dependencies"]) == {"M_UNIQUE::x", "M_UNIQUE::y"},
              "实际依赖包含循环两格")
        verdict = service.verify_solve(report["solve_id"])
        check(verdict["ok"], "唯一循环报告独立验证通过")
        check(service.verify_compile(artifact)["ok"], "唯一循环编译产物独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ----------------------------------------------------- 案例 2：冗余一致循环
def test_redundant_cycle():
    service, tmp = fresh_service()
    try:
        artifact, _, _ = publish_and_compile(service, scenarios.redundant_consistent_cycle())
        report = service.solve(artifact)
        qa = service.query(report["solve_id"], "M_REDUNDANT::a")
        qr = service.query(report["solve_id"], "M_REDUNDANT::r")
        check(qa["status"] == "parametric", "冗余循环 a 保留为自由参数")
        check(qa["expression"]["constant"] == "0"
              and len(qa["expression"]["param_terms"]) == 1,
              "自由格给可核查参数表达式")
        check(qr["status"] == "determined" and qr["value"] == "5",
              "自由系统中 r 仍被唯一确定为 5（手算）")
        verdict = service.verify_solve(report["solve_id"])
        check(verdict["ok"], "冗余循环参数表达式符号代回验证通过")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------------------- 案例 3：矛盾循环
def test_contradiction_cycle():
    service, tmp = fresh_service()
    try:
        artifact, _, _ = publish_and_compile(service, scenarios.contradictory_cycle())
        report = service.solve(artifact)
        qa = service.query(report["solve_id"], "M_CONTRA::a")
        check(qa["status"] == "inconsistent", "矛盾循环输出标记为失败而非返回数值")
        explanation = service.explain(report["solve_id"], "M_CONTRA::a")
        cert = explanation["contradictions"][0]
        check(cert["residual_lhs"] == "0" and cert["residual_rhs"] != "0",
              "矛盾证书形如 0 = 非零")
        # 手算：方程 a-b=1 与 b-a=1 相加 => 0=2
        combo = {item["equation_variable"]: Fraction(item["lambda"])
                 for item in cert["equation_combination"]}
        residual = sum(
            lam * Fraction(artifact["equations"][v]["rhs"])
            for v, lam in combo.items()
        )
        check(residual == Fraction(cert["residual_rhs"]) and residual != 0,
              "λ 组合手算复现矛盾右端")
        check("failure_source" in explanation and explanation["failure_source"],
              "失败输出携带方程来源")
        verdict = service.verify_solve(report["solve_id"])
        check(verdict["ok"], "矛盾证书独立重算验证通过")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 案例 4：自由变量但输出被固定
def test_free_but_fixed():
    service, tmp = fresh_service()
    try:
        artifact, _, _ = publish_and_compile(service, scenarios.free_but_output_fixed())
        report = service.solve(artifact)
        qu = service.query(report["solve_id"], "M_FREEFIX::u")
        qt = service.query(report["solve_id"], "M_FREEFIX::t")
        check(qu["status"] == "parametric", "u 是自由参数")
        check(qt["status"] == "determined" and qt["value"] == "2/7",
              "同一系统里 t 被唯一确定为 2/7（手算）")
        check(service.verify_solve(report["solve_id"])["ok"],
              "确定/不确定分类独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------- 案例 5：跨模型引用 + 改名身份
def test_cross_model_rename():
    service, tmp = fresh_service()
    try:
        put_a = service.put_model(scenarios.model_a_simple())
        put_b = service.put_model(scenarios.model_b_referencing_a())
        check(put_b["external_refs"]["A_FIN"]["head_rev"] == put_a["rev_id"],
              "跨模型引用登记外部输入版本")
        snap_b = service.snapshot("B_BUD", label="v1")
        artifact1, _ = service.compile(snap_b["snap_id"])
        check(set(artifact1["scope_snapshots"]) == {"A_FIN", "B_BUD"},
              "编译作用域自动包含被引用模型的钉住快照")
        report1 = service.solve(artifact1)
        qf = service.query(report1["solve_id"], "B_BUD::f")
        qg = service.query(report1["solve_id"], "B_BUD::g")
        check(qf["value"] == "12013/36", "跨模型 f 手算 12013/36")
        check(qg["value"] == "-2993/36", "跨模型 g 手算 -2993/36")

        # 上游改名：永久 id 不变，旧快照与旧报告保持不变
        renamed = scenarios.model_a_simple()
        renamed["name"] = "改名后的上游财务模型"
        put_a2 = service.put_model(renamed)
        check(put_a2["model_id"] == "A_FIN" and put_a2["rev_id"] != put_a["rev_id"],
              "改名不改变永久 model_id，只产生新修订")
        meta = service.get_model("A_FIN")
        check(meta["name"] == "改名后的上游财务模型", "目录显示名已更新")
        report1_reloaded = service.get_report(report1["solve_id"])
        check(report1_reloaded["report_hash"] == report1["report_hash"]
              and report1_reloaded["outputs"]["B_BUD::f"]["value"] == "12013/36",
              "改名后旧报告内容哈希与数值不变")
        snap_old_a = service.get_snapshot(
            artifact1["scope_snapshots"]["A_FIN"]["snap_id"])
        check(snap_old_a["name"] == "上游财务模型",
              "旧快照保留发布时的完整公式与名称")

        # 用新 HEAD 重新固定/编译：拓扑与矩阵必须重新识别，而不是复用旧的
        snap_b2 = service.snapshot("B_BUD", label="v2")
        artifact2, _ = service.compile(snap_b2["snap_id"])
        check(artifact2["artifact_hash"] != artifact1["artifact_hash"]
              and artifact2["scope_snapshots"]["A_FIN"]["snap_id"]
              != artifact1["scope_snapshots"]["A_FIN"]["snap_id"],
              "重新编译生成新产物并钉住新外部版本")
        report2 = service.solve(artifact2)
        check(service.query(report2["solve_id"], "B_BUD::f")["value"] == "12013/36",
              "改名不改公式时新报告数值一致但报告相互独立")
        check(service.verify_solve(report1["solve_id"])["ok"]
              and service.verify_solve(report2["solve_id"])["ok"],
              "新旧两份报告分别独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 案例 6：输入更正驱动重算
def test_input_correction():
    service, tmp = fresh_service()
    try:
        payload = scenarios.input_cycle_model()
        artifact1, _, snap1 = publish_and_compile(service, payload, label="旧输入")
        report1 = service.solve(artifact1)
        check(service.query(report1["solve_id"], "M_INPUT_CYCLE::p")["value"] == "8",
              "旧输入 inp=6 时 p=8（手算 4k/3）")
        check(service.query(report1["solve_id"], "M_INPUT_CYCLE::q")["value"] == "4",
              "旧输入 inp=6 时 q=4（手算 2k/3）")

        corrected = scenarios.input_cycle_model()
        corrected["cells"]["inp"]["value"] = "12"
        service.put_model(corrected)
        snap2 = service.snapshot("M_INPUT_CYCLE", label="更正输入")
        artifact2, _ = service.compile(snap2["snap_id"])
        report2 = service.solve(artifact2)
        check(service.query(report2["solve_id"], "M_INPUT_CYCLE::p")["value"] == "16",
              "输入更正后 p=16（受影响结果重新求解）")
        check(service.query(report2["solve_id"], "M_INPUT_CYCLE::q")["value"] == "8",
              "输入更正后 q=8")
        check(service.get_report(report1["solve_id"])["outputs"]["M_INPUT_CYCLE::p"][
                  "value"] == "8",
              "旧快照报告保持旧值，不被重算覆盖")
        # 同一报告再解一次：闭包指纹相同 → 缓存命中
        report3 = service.solve(artifact2)
        check(report3["cache"]["fingerprints_hit"], "相同闭包重解命中内容缓存")
        check(service.verify_solve(report1["solve_id"])["ok"]
              and service.verify_solve(report2["solve_id"])["ok"],
              "输入更正前后报告均独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 目录健壮性：坏模型隔离
def test_bad_model_isolation_and_rejection():
    service, tmp = fresh_service()
    try:
        # 整次修订拒绝：引用不存在的格
        try:
            service.put_model(scenarios.bad_model_good_neighbor())
            raise AssertionError("坏模型必须被拒绝")
        except ReferenceRejectedError as exc:
            check("ghost" in exc.errors[0], "拒绝明细指出悬挂引用")
        check(service.list_models() == [], "被拒绝修订不留任何模型")

        # 好模型正常可用
        service.put_model(scenarios.two_cell_unique_cycle())
        # 再次尝试一个坏模型（跨模型指向不存在的模型）
        bad_cross = {
            "model_id": "M_BAD2",
            "name": "坏模型2",
            "cells": {
                "z": {"kind": "formula", "constant": "0",
                      "terms": [{"coef": "1", "model_id": "NOPE", "cell_id": "x"}]}
            },
            "outputs": ["z"],
        }
        try:
            service.put_model(bad_cross)
            raise AssertionError("跨模型悬挂引用必须被拒绝")
        except ReferenceRejectedError:
            pass
        snap = service.snapshot("M_UNIQUE")
        artifact, _ = service.compile(snap["snap_id"])
        report = service.solve(artifact)
        check(service.query(report["solve_id"], "M_UNIQUE::x")["value"] == "2/3",
              "不相关坏模型不影响好模型的编译与解算")

        # 入向引用保护：修订上游删除被依赖格 → 整次拒绝
        upstream = scenarios.model_a_simple()
        downstream = scenarios.model_b_referencing_a()
        service.put_model(upstream)
        service.put_model(downstream)
        broken = scenarios.model_a_simple()
        del broken["cells"]["e"]
        broken["outputs"] = ["d"]
        try:
            service.put_model(broken)
            raise AssertionError("删除被依赖格的修订必须被拒绝")
        except ReferenceRejectedError as exc:
            check("B_BUD" in exc.errors[0], "入向引用保护指出依赖方")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 输入校验：无浮点/无未知格相乘
def test_input_validation():
    service, tmp = fresh_service()
    try:
        bad_float = scenarios.two_cell_unique_cycle()
        bad_float["cells"]["x"]["constant"] = 0.5
        try:
            service.put_model(bad_float)
            raise AssertionError("float 必须被拒绝")
        except ValidationError:
            pass

        bad_big = scenarios.two_cell_unique_cycle()
        bad_big["cells"]["x"]["constant"] = "1234567890123/1"  # 13 位分子
        try:
            service.put_model(bad_big)
            raise AssertionError("超 12 位输入必须被拒绝")
        except ValidationError:
            pass

        bad_denom = scenarios.two_cell_unique_cycle()
        bad_denom["cells"]["x"]["constant"] = "1/-3"
        try:
            service.put_model(bad_denom)
            raise AssertionError("负分母必须被拒绝")
        except ValidationError:
            pass

        # 项形状只允许 系数×单格：嵌套/缺 cell_id 拒绝
        bad_term = scenarios.two_cell_unique_cycle()
        bad_term["cells"]["x"]["terms"] = [{"coef": "1/2"}]
        try:
            service.put_model(bad_term)
            raise AssertionError("缺 cell_id 的项必须被拒绝")
        except ValidationError:
            pass
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 重开：新进程视角复读
def test_reopen_reports():
    tmp = tempfile.mkdtemp(prefix="budget-reopen-")
    try:
        service = BudgetService(tmp)
        artifact, _, _ = publish_and_compile(service, scenarios.two_cell_unique_cycle())
        report = service.solve(artifact)
        solve_id = report["solve_id"]
        report_hash = report["report_hash"]

        # 全新服务实例 = 重开数据目录
        reopened = BudgetService(tmp)
        report2 = reopened.get_report(solve_id)
        check(report2["report_hash"] == report_hash, "重开后报告哈希一致")
        check(reopened.query(solve_id, "M_UNIQUE::x")["value"] == "2/3",
              "重开后查询同一确定值")
        check(reopened.verify_compile(reopened.load_artifact(report2["artifact_hash"]))[
                  "ok"], "重开后编译产物仍可独立验证")
        check(reopened.verify_solve(solve_id)["ok"], "重开后解算报告仍可独立验证")
        # 缓存也被重开加载：再解一次命中
        report3 = reopened.solve(reopened.load_artifact(report2["artifact_hash"]))
        check(report3["cache"]["fingerprints_hit"], "重开后磁盘缓存继续生效")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 不收敛≠无解：唯一循环正常解出
def test_no_floating_point_anywhere():
    service, tmp = fresh_service()
    try:
        artifact, _, _ = publish_and_compile(service, scenarios.two_cell_unique_cycle())
        report = service.solve(artifact)
        for out in report["outputs"].values():
            if out["status"] == "determined":
                # 规范分数形式，分子分母都是整数
                value = out["value"]
                Fraction(value)
                check("/" in value or value.lstrip("-").isdigit(),
                      f"确定值 {value} 是规范有理数而非浮点")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 自引用（自环）格
def test_self_reference_cell():
    """单格自环：z = 1/3 + (1/4)·z。手算 (3/4)z = 1/3 => z = 4/9。

    回归测试：编译器构造方程时自指系数必须与单位项相加（1 - 1/4 = 3/4），
    不能被覆盖成 -1/4。
    """
    service, tmp = fresh_service()
    try:
        payload = {
            "model_id": "M_SELF",
            "name": "自环模型",
            "cells": {
                "z": {
                    "kind": "formula",
                    "constant": "1/3",
                    "terms": [{"coef": "1/4", "cell_id": "z"}],
                }
            },
            "outputs": ["z"],
        }
        artifact, _, _ = publish_and_compile(service, payload)
        comp = next(c for c in artifact["components"] if c["variables"] == ["M_SELF::z"])
        check(comp["cyclic"], "自环单格被标记为循环 SCC")
        # 方程系数：(1 - 1/4) z = 1/3
        check(artifact["equations"]["M_SELF::z"]["coeffs"]["M_SELF::z"] == "3/4",
              "自环方程系数为 1 - 1/4 = 3/4 而非被覆盖")
        report = service.solve(artifact)
        check(service.query(report["solve_id"], "M_SELF::z")["value"] == "4/9",
              "自环格手算解 z = 4/9")
        check(service.verify_solve(report["solve_id"])["ok"],
              "自环报告独立代回验证通过")
    finally:
        shutil.rmtree(tmp)


def run_all():
    tests = [
        test_unique_cycle,
        test_redundant_cycle,
        test_contradiction_cycle,
        test_free_but_fixed,
        test_cross_model_rename,
        test_input_correction,
        test_bad_model_isolation_and_rejection,
        test_input_validation,
        test_reopen_reports,
        test_no_floating_point_anywhere,
        test_self_reference_cell,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n全部 {len(PASS)} 项断言通过。")


if __name__ == "__main__":
    run_all()
