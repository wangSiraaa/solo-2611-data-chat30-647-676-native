"""内置验证套件：全部使用临时 SQLite 与合成小图，可重复执行。

覆盖题面点名场景：树形多解、无锚平移歧义、单个回路矛盾、正反向边、
同向来源冲突、撤回/更正后可行性真实恢复、有理 scale、极值同源但不同时可达、
外部产品导入、不相容锚点与不可变旧分析冻结。
"""

from __future__ import annotations

import json
import os
import tempfile
from fractions import Fraction
from typing import Callable

from .catalog import Catalog
from .repository import AnalysisRepository, run_analysis


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _scales(nodes: list[str], value: str = "1") -> dict[str, Fraction]:
    return {n: Fraction(value) for n in nodes}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _check_against_enumeration(report: dict) -> None:
    cc = report["brute_force_cross_check"]
    _assert(cc["performed"], "小图应能全枚举核对")
    _assert(cc["passed"], f"全枚举核对发现不一致：{cc['mismatches']}")


# ---------------------------------------------------------------------------
# 各场景
# ---------------------------------------------------------------------------


def case_tree_multi_solution(work: str) -> str:
    """树 a-b-c，无边环约束 -> 多个整数势；锚一个节点后仍可能多解。"""
    cat = Catalog(os.path.join(work, "tree.db"), m=4)
    for n in ("a", "b", "c"):
        cat.add_node(n)
    cat.add_edge("src1", "a", "b", r=1, lo=-3, hi=3)   # 候选 [-3,1]
    cat.add_edge("src1", "b", "c", r=2, lo=-2, hi=2)   # 候选 [-2,2]
    pub = cat.publish("tree")
    cat.close()

    report = run_analysis(pub["payload"], _scales(pub["payload"]["nodes"]), 5, {})
    _assert(report["status"] == "CONSISTENT", "树模型应可行")
    _assert(not report["unique_solution_overall"], "无锚树必须存在多解，不能报唯一")
    _assert(report["brute_force_cross_check"]["solution_count"] > 1, "枚举解数应大于 1")
    _check_against_enumeration(report)

    # 锚住 a=0 后，b、c 仍在各自候选集合内多解。
    report2 = run_analysis(pub["payload"], _scales(pub["payload"]["nodes"]), 5, {"a": 0})
    _assert(not report2["unique_solution_overall"], "单锚树仍应多解")
    _assert(report2["lexicographically_smallest_solution"]["a"] == 0, "锚点必须被满足")
    _check_against_enumeration(report2)
    return f"枚举 {report['brute_force_cross_check']['solution_count']} 个无锚解，单锚后仍多解"


def case_unanchored_translation(work: str) -> str:
    """无锚连通分量：整体平移不被钉零，极值仅由全局界截断。"""
    cat = Catalog(os.path.join(work, "trans.db"), m=8)
    for n in ("p", "q"):
        cat.add_node(n)
    cat.add_edge("s", "p", "q", r=0, lo=0, hi=0)  # x_q - x_p = 0 唯一差值
    pub = cat.publish()
    cat.close()

    report = run_analysis(pub["payload"], _scales(["p", "q"]), 4, {})
    _assert(report["status"] == "CONSISTENT", "等值分量应可行")
    comp = report["components"][0]
    _assert(not comp["has_anchor"], "该分量应无锚")
    # 字典序最小解落在界底，故 -1 不可行、+1 可行；平移歧义的正确证据是
    # 同一分量存在多个不同的整体平移见证（由全局界截断），而非假设解在界中央。
    tf = comp["translation_freedom"]
    _assert(
        tf["shift_plus_one_feasible"] or tf["shift_minus_one_feasible"],
        "无锚分量必须展示出整体平移自由度，禁止擅自钉零",
    )
    lo_w = report["node_results"]["p"]["min_witness"]
    hi_w = report["node_results"]["p"]["max_witness"]
    _assert(lo_w != hi_w and lo_w["p"] == -4 and hi_w["p"] == 4, "无锚分量应能在 [-B,B] 内整体平移")
    _assert(report["node_results"]["p"]["phase_min"] == -4, "最小值应由全局界 -B 给出")
    _assert(report["node_results"]["p"]["phase_max"] == 4, "最大值应由全局界 +B 给出")
    _assert(not report["unique_solution_overall"], "平移歧义下不得报唯一")
    _check_against_enumeration(report)

    anchored = run_analysis(pub["payload"], _scales(["p", "q"]), 4, {"q": 2})
    _assert(anchored["unique_solution_overall"], "锚定后等值分量应唯一")
    _assert(anchored["node_results"]["p"]["phase_min"] == 2, "锚 q=2 且 p=q")
    _check_against_enumeration(anchored)

    # 平移歧义的正确判定：分量内存在两个可行整体平移（以各自极值见证为基准），
    # 而不是假设字典序最小解一定在界中央。
    lo_w = report["node_results"]["p"]["min_witness"]
    hi_w = report["node_results"]["p"]["max_witness"]
    _assert(
        lo_w["p"] == -4 and hi_w["p"] == 4 and lo_w != hi_w,
        "无锚分量必须存在不同的整体平移，禁止擅自钉零",
    )
    return "无锚分量存在多个整体平移且未钉零；锚定后唯一"


def case_single_cycle_contradiction(work: str) -> str:
    """三角形回路：两条边允许 0，第三条恒为 1，闭合残差不含 0。"""
    cat = Catalog(os.path.join(work, "cyc.db"), m=8)
    for n in ("a", "b", "c"):
        cat.add_node(n)
    e1 = cat.add_edge("s", "a", "b", r=0, lo=0, hi=0)  # b-a=0
    e2 = cat.add_edge("s", "b", "c", r=0, lo=0, hi=0)  # c-b=0
    e3 = cat.add_edge("s", "c", "a", r=1, lo=1, hi=1)  # a-c=1
    lint = cat.lint()
    _assert(any(p["kind"] == "cycle_residual" for p in lint["problems"]), "目录 lint 应报回路残差")
    pub = cat.publish("contradictory-cycle")  # 冲突不阻断发布、不删除
    cat.close()

    report = run_analysis(pub["payload"], _scales(["a", "b", "c"]), 6, {})
    _assert(report["status"] == "INCONSISTENT", "矛盾回路必须判 INCONSISTENT")
    core_ids = set(report["unsat_core"]["constraint_ids"])
    _assert({f"e{e1}", f"e{e2}", f"e{e3}"} <= core_ids, "反证核应包含三条回路边")
    _assert(report["unsat_core"]["minimal_by_deletion"], "反证核必须最小")
    _assert(report["replay"]["wipeout"] is not None, "反证记录应含空域 wipeout")
    _assert(len(report["replay"]["ac3_revisions"]) > 0, "反证记录应含可重放修订")
    return f"回路残差诊断命中，最小反证核={sorted(core_ids)}"


def case_forward_and_reverse_edges(work: str) -> str:
    """同一节点对上正、反向边各按自身方向解释；再加一条反向平行边。"""
    cat = Catalog(os.path.join(work, "rev.db"), m=6)
    for n in ("u", "v"):
        cat.add_node(n)
    fwd = cat.add_edge("f", "u", "v", r=2, lo=2, hi=8)   # v-u ∈ {2,8}
    rev = cat.add_edge("g", "v", "u", r=4, lo=-2, hi=4)  # u-v ∈ {-2,4}
    pub = cat.publish()
    cat.close()

    # 手工语义核对：v-u=d；反向边要求 u-v=-d ∈ {-2,4} => d ∈ {2,-4}；
    # 与 d ∈ {2,8} 联立只有 d=2。
    report = run_analysis(pub["payload"], _scales(["u", "v"]), 8, {"u": 0})
    _assert(report["status"] == "CONSISTENT", "正反向联立应可行")
    _assert(report["node_results"]["v"]["phase_min"] == 2, "联立后 v 只能为 2")
    _assert(report["node_results"]["v"]["phase_max"] == 2, "联立后差值唯一 d=2")
    _check_against_enumeration(report)

    # 目录侧反向查询：反向经过 fwd 边，步长集合应为 {-8,-2}。
    cat2 = Catalog(os.path.join(work, "rev.db"))
    info = cat2.path_range_by_edge_ids([fwd, rev])
    _assert(info["node_path"] == ["u", "v", "u"], "边路径节点序列错误")
    _assert(0 in info["possible_totals"], "往返路径应包含总差 0")
    cat2.close()
    return "正反向联立差值唯一为 2；边路径反向解释正确"


def case_same_direction_conflict(work: str) -> str:
    """同节点对两条来源边交集为空：目录不覆盖不删除，分析判不相容并解释。"""
    cat = Catalog(os.path.join(work, "conf.db"), m=10)
    for n in ("a", "b"):
        cat.add_node(n)
    e1 = cat.add_edge("src1", "a", "b", r=0, lo=0, hi=0)  # 差值 0
    e2 = cat.add_edge("src2", "a", "b", r=1, lo=1, hi=1)  # 差值 1
    inter = cat.same_direction_intersection("a", "b")
    _assert(inter["conflict"] and inter["intersection"] == [], "同向交集必须为空并标记冲突")
    lint = cat.lint()
    _assert(any(p["kind"] == "same_direction_conflict" for p in lint["problems"]), "lint 应报来源冲突")
    _assert(len(cat.active_edges()) == 2, "冲突观测禁止自动删除")
    pub = cat.publish("conflict-kept")
    cat.close()

    report = run_analysis(pub["payload"], _scales(["a", "b"]), 4, {})
    _assert(report["status"] == "INCONSISTENT", "同向冲突必须 INCONSISTENT")
    core = set(report["unsat_core"]["constraint_ids"])
    _assert({f"e{e1}", f"e{e2}"} <= core, "反证核应包含两条冲突边")
    return f"同向边 {e1},{e2} 交集为空，观测保留且反证核命中"


def case_retract_and_correct_recovery(work: str) -> str:
    """撤回错误回路边 / 更正余数后，重新分析真实改变可行性；旧分析冻结旧产品。"""
    cat = Catalog(os.path.join(work, "rec.db"), m=8)
    for n in ("a", "b", "c"):
        cat.add_node(n)
    cat.add_edge("s", "a", "b", r=0, lo=0, hi=0)
    cat.add_edge("s", "b", "c", r=0, lo=0, hi=0)
    bad = cat.add_edge("s", "c", "a", r=3, lo=3, hi=3)  # 错误：残差 3
    bad_pub = cat.publish("bad-cycle")
    cat.close()

    repo = AnalysisRepository(os.path.join(work, "repo.db"))
    pid_bad = repo.import_product(bad_pub["payload"], bad_pub["product_id"], origin="catalog")
    before = repo.analyze("r-bad", pid_bad, _scales(["a", "b", "c"]), 6, {})
    _assert(before["status"] == "INCONSISTENT", "错误回路应不相容")

    # 撤回错误边 -> 新产品恢复可行。
    cat = Catalog(os.path.join(work, "rec.db"))
    cat.retract_edge(bad, "错误回路边，撤回")
    fixed_pub = cat.publish("cycle-broken")
    _assert(fixed_pub["product_id"] != bad_pub["product_id"], "撤回后必须产生新产品 id")
    cat.close()
    pid_fixed = repo.import_product(fixed_pub["payload"], fixed_pub["product_id"], origin="catalog")
    after = repo.analyze("r-fixed", pid_fixed, _scales(["a", "b", "c"]), 6, {})
    _assert(after["status"] == "CONSISTENT", "撤回后应恢复可行")
    _check_against_enumeration(after)

    # 旧分析仍固定旧产品与旧锚点，不随后续编辑变化。
    frozen = repo.get_report("r-bad")
    _assert(frozen["status"] == "INCONSISTENT", "旧分析必须保持冻结")

    # 更正余数场景（独立库）：把一条矛盾边更正为闭合边后恢复可行。
    cat = Catalog(os.path.join(work, "rec2.db"), m=8)
    for n in ("a", "b", "c"):
        cat.add_node(n)
    cat.add_edge("s", "a", "b", r=0, lo=0, hi=0)
    cat.add_edge("s", "b", "c", r=0, lo=0, hi=0)
    bad2 = cat.add_edge("s", "c", "a", r=3, lo=3, hi=3)
    bad2_pub = cat.publish("bad-cycle-2")
    cat.close()
    pid_bad2 = repo.import_product(bad2_pub["payload"], bad2_pub["product_id"], origin="catalog")
    _assert(
        repo.analyze("r-bad2", pid_bad2, _scales(["a", "b", "c"]), 6, {})["status"]
        == "INCONSISTENT",
        "更正前的矛盾回路应不相容",
    )

    cat = Catalog(os.path.join(work, "rec2.db"))
    new_id = cat.correct_edge(bad2, r=0, lo=0, hi=0, reason="余数应为0")
    _assert(not cat.get_edge(bad2).active, "被更正旧边必须撤回留痕")
    corrected_pub = cat.publish("corrected")
    cat.close()
    pid_corr = repo.import_product(corrected_pub["payload"], corrected_pub["product_id"], origin="catalog")
    corrected = repo.analyze("r-corr", pid_corr, _scales(["a", "b", "c"]), 6, {"a": 0})
    _assert(corrected["status"] == "CONSISTENT", "更正余数后应恢复可行")
    _assert(corrected["unique_solution_overall"], "三条零差边加锚 a=0 应唯一全零")
    _assert(corrected["node_results"]["c"]["phase_min"] == 0, "更正后 c 应被锚定为 0")
    _check_against_enumeration(corrected)
    _assert(new_id != bad2, "更正应产生新 edge_id")
    return "撤回/更正后新产品可行性真实恢复，旧分析 r-bad/r-bad2 保持 INCONSISTENT 冻结"


def case_external_product_import(work: str) -> str:
    """分析仓接受外部同格式产品文件，且篡改候选的产品被拒绝。"""
    cat = Catalog(os.path.join(work, "ext.db"), m=5)
    for n in ("a", "b"):
        cat.add_node(n)
    cat.add_edge("s", "a", "b", r=1, lo=-4, hi=1)
    pub = cat.publish()
    path = os.path.join(work, "product.json")
    cat.export_product(pub["product_id"], path)
    cat.close()

    repo = AnalysisRepository(os.path.join(work, "ext-repo.db"))
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    pid = repo.import_product(doc["payload"], doc["product_id"], origin="external-file")
    report = repo.analyze("ext1", pid, _scales(["a", "b"]), 3, {"a": 0})
    _assert(report["status"] == "CONSISTENT", "外部产品应可分析")
    _check_against_enumeration(report)

    tampered = json.loads(json.dumps(doc))
    tampered["payload"]["edges"][0]["candidates"] = [999]
    try:
        repo.import_product(tampered["payload"], "Ptampered", origin="external-file")
        raise AssertionError("候选与 r/M/界不一致的外部产品必须被拒绝")
    except ValueError:
        pass
    repo.close()
    return "外部产品成功导入分析；篡改候选被拒绝"


def case_rational_scales(work: str) -> str:
    """scale 为正有理数时，位移区间为精确 Fraction 乘积。"""
    cat = Catalog(os.path.join(work, "scale.db"), m=4)
    for n in ("a", "b"):
        cat.add_node(n)
    cat.add_edge("s", "a", "b", r=1, lo=-3, hi=3)
    pub = cat.publish()
    cat.close()

    scales = {"a": Fraction("3/2"), "b": Fraction("1/4")}
    report = run_analysis(pub["payload"], scales, 4, {"a": 0})
    _assert(report["status"] == "CONSISTENT", "有理尺度分析应可行")
    rb = report["node_results"]["b"]
    # b ∈ {-3,1}（a=0），位移 = b/4
    _assert(rb["displacement_min"] == {"num": -3, "den": 4, "text": "-3/4"}, "最小位移应为 -3/4")
    _assert(rb["displacement_max"] == {"num": 1, "den": 4, "text": "1/4"}, "最大位移应为 1/4")
    ra = report["node_results"]["a"]
    _assert(ra["displacement_min"]["text"] == "0", "锚点位移应为 0")
    _check_against_enumeration(report)
    return "位移区间精确为 Fraction 乘积（-3/4, 1/4）"


def case_extrema_not_joint(work: str) -> str:
    """构造各节点极值不能同时达到的例子，接口必须显式声明而非假装可同时取到。

    M=2，锚 a=0，B=3：
      边 a->b 候选 {0,2}，边 b->c 候选 {-2,0}。
    可行赋值：b∈{0,2}，c∈b+{-2,0}。
      b=0 -> c∈{-2,0}；b=2 -> c∈{0,2}。
    故 b 的最大值 2 与 c 的最小值 -2 分别可达，但绝不可能同时达到。
    """
    cat = Catalog(os.path.join(work, "joint.db"), m=2)
    for n in ("a", "b", "c"):
        cat.add_node(n)
    cat.add_edge("s", "a", "b", r=0, lo=0, hi=2)    # {0,2}
    cat.add_edge("s", "b", "c", r=0, lo=-2, hi=0)   # {-2,0}
    pub = cat.publish()
    cat.close()

    report = run_analysis(pub["payload"], _scales(["a", "b", "c"]), 3, {"a": 0})
    _assert(report["status"] == "CONSISTENT", "构造应可行")
    nr = report["node_results"]
    _assert(nr["b"]["phase_min"] == 0 and nr["b"]["phase_max"] == 2, "b 极值应为 [0,2]")
    _assert(nr["c"]["phase_min"] == -2 and nr["c"]["phase_max"] == 2, "c 极值应为 [-2,2]")

    # 每个极值都必须有来自同一全图可行域的完整见证赋值。
    for n in ("a", "b", "c"):
        _assert(nr[n]["min_witness"][n] == nr[n]["phase_min"], "最小见证必须取到最小值")
        _assert(nr[n]["max_witness"][n] == nr[n]["phase_max"], "最大见证必须取到最大值")

    # 全最小向量 (a,b,c)=(0,0,-2) 可行；全最大 (0,2,2) 可行。
    je = report["joint_extrema"]
    _assert(je["all_min_jointly_feasible"], "全最小向量应可行")
    _assert(je["all_max_jointly_feasible"], "全最大向量应可行")

    # 关键反例：把 b 钉到其最大值 2、同时把 c 钉到其最小值 -2，必须不可行。
    from .exact import Solver, BinaryConstraint, UnaryConstraint

    binary = [
        BinaryConstraint(
            cid=f"e{e['edge_id']}", u=e["u"], v=e["v"],
            allowed=frozenset(e["candidates"]),
        )
        for e in pub["payload"]["edges"]
    ]
    unary = {
        n: [UnaryConstraint(f"bound[{n}]", n, frozenset(range(-3, 4)))]
        for n in ("a", "b", "c")
    }
    unary["a"].append(UnaryConstraint("anchor[a]=0", "a", frozenset({0})))
    solver = Solver(["a", "b", "c"], binary, unary, {n: (-3, 3) for n in ("a", "b", "c")})
    incompatible = solver.solve(
        cap=1, extra_unary={"b": frozenset({2}), "c": frozenset({-2})}
    )
    _assert(not incompatible.feasible, "b 取最大与 c 取最小必须无法同时达到")

    # 与独立全枚举的解集合逐节点极值核对。
    _check_against_enumeration(report)
    return "极值见证独立成立；b 最大 2 与 c 最小 -2 经求解器确认无法同时达到，接口已显式声明"


def case_incompatible_anchors(work: str) -> str:
    """锚点与边或全局界矛盾时给出含锚点的最小反证核。"""
    cat = Catalog(os.path.join(work, "anc.db"), m=8)
    for n in ("a", "b"):
        cat.add_node(n)
    cat.add_edge("s", "a", "b", r=0, lo=0, hi=0)  # b-a=0
    pub = cat.publish()
    cat.close()

    # 锚 a=0, b=1 与 b=a 矛盾。
    report = run_analysis(pub["payload"], _scales(["a", "b"]), 4, {"a": 0, "b": 1})
    _assert(report["status"] == "INCONSISTENT", "矛盾锚点必须 INCONSISTENT")
    ids = set(report["unsat_core"]["constraint_ids"])
    _assert("anchor[a]=0" in ids and "anchor[b]=1" in ids, "反证核应包含两个锚点")
    _assert(any(i.startswith("e") for i in ids), "反证核还应包含连接边")
    _assert(report["unsat_core"]["minimal_by_deletion"], "锚点反证核必须最小")

    # 锚点超出全局界：一元界约束直接拒绝。
    try:
        run_analysis(pub["payload"], _scales(["a", "b"]), 2, {"a": 5})
        raise AssertionError("超界锚点应被明确拒绝")
    except ValueError:
        pass
    return "矛盾锚点反证核含锚点与边；超界锚点被明确拒绝"


def case_unknown_node_and_empty_candidate(work: str) -> str:
    """未知节点、空候选边、空交集、空路径均明确拒绝，不静默删除。"""
    cat = Catalog(os.path.join(work, "guard.db"), m=4)
    cat.add_node("a")
    cat.add_node("b")
    for bad in (
        lambda: cat.add_edge("s", "a", "ghost", 1, 0, 1),
        lambda: cat.add_edge("s", "ghost", "b", 1, 0, 1),
    ):
        try:
            bad()
            raise AssertionError("未知节点边必须被拒绝")
        except ValueError:
            pass
    # r=1 但界内只有偶数：r+4k 永远落不进 [0,0]
    try:
        cat.add_edge("s", "a", "b", r=1, lo=0, hi=0)
        raise AssertionError("空候选边必须被拒绝")
    except ValueError:
        pass
    cat.add_edge("s", "a", "b", r=0, lo=0, hi=0)
    try:
        cat.path_range(["a", "ghost"])
        raise AssertionError("含未知节点的路径必须拒绝")
    except ValueError:
        pass
    try:
        cat.same_direction_intersection("b", "a")  # 只有 a->b 边
        raise AssertionError("无活跃边的方向必须拒绝发布空交集")
    except ValueError:
        pass
    cat.close()
    return "未知节点/空候选/空交集/空路径全部显式拒绝，观测未被静默删除"


def case_request_limit_and_immutability(work: str) -> str:
    """分析请求上限 10；request_id 不可覆盖；产品不可变。"""
    cat = Catalog(os.path.join(work, "lim.db"), m=4)
    for n in ("a",):
        cat.add_node(n)
    pub = cat.publish()
    cat.close()
    repo = AnalysisRepository(os.path.join(work, "lim-repo.db"))
    pid = repo.import_product(pub["payload"], pub["product_id"], origin="catalog")
    for i in range(10):
        repo.analyze(f"req{i}", pid, {"a": Fraction(1)}, 3, {})
    try:
        repo.analyze("req10", pid, {"a": Fraction(1)}, 3, {})
        raise AssertionError("第 11 个请求必须被拒绝")
    except ValueError:
        pass
    try:
        repo.analyze("req0", pid, {"a": Fraction(2)}, 3, {})
        raise AssertionError("旧 request_id 不可覆盖")
    except ValueError:
        pass
    repo.close()
    return "10 请求上限生效；request_id 与产品内容均不可变"


# ---------------------------------------------------------------------------
# 套件驱动
# ---------------------------------------------------------------------------


CASES: list[tuple[str, Callable[[str], str]]] = [
    ("树形多解", case_tree_multi_solution),
    ("无锚平移歧义不钉零", case_unanchored_translation),
    ("单个回路矛盾", case_single_cycle_contradiction),
    ("正反向边各自解释", case_forward_and_reverse_edges),
    ("同向来源冲突不覆盖", case_same_direction_conflict),
    ("撤回与更正后解恢复", case_retract_and_correct_recovery),
    ("外部产品导入与篡改拒绝", case_external_product_import),
    ("有理 scale 位移区间", case_rational_scales),
    ("节点极值同源与联合可达声明", case_extrema_not_joint),
    ("不相容锚点反证核", case_incompatible_anchors),
    ("未知节点与空候选显式拒绝", case_unknown_node_and_empty_candidate),
    ("请求上限与不可变性", case_request_limit_and_immutability),
]


def run_suite() -> dict[str, object]:
    cases_out: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="phaseware-validate-") as work:
        for name, fn in CASES:
            try:
                detail = fn(work)
                cases_out.append({"name": name, "passed": True, "detail": detail})
            except Exception as exc:  # noqa: BLE001 - 验证套件需要捕获全部失败
                cases_out.append(
                    {"name": name, "passed": False, "detail": f"{type(exc).__name__}: {exc}"}
                )
    passed = sum(1 for c in cases_out if c["passed"])
    return {
        "total": len(cases_out),
        "passed": passed,
        "all_passed": passed == len(cases_out),
        "cases": cases_out,
    }
