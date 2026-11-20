"""位移分析仓（消费侧）。

消费目录发布的**不可变公开产品**（或外部同格式产品），不读目录编辑表。
给定每节点有理转换比例 scale>0、全局整数相位界 [-B,B] 与若干锚点，
精确判定是否存在整数节点势 x 同时满足：

1. 全部有向边的模 M 缠绕约束（x_v - x_u 落在边候选集合内）；
2. 全局界 -B <= x_v <= B；
3. 锚点 x_v = value。

每个连通分量可以没有锚点；无锚分量不会被擅自钉零，而是如实报告
平移歧义与由全局界决定的有限极值。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from fractions import Fraction
from typing import Optional

from .catalog import MAX_EDGES, MAX_NODES, validate_product
from .exact import (
    BinaryConstraint,
    UnaryConstraint,
    Solver,
    brute_force_enumerate,
    connected_components,
    cycle_basis,
    minkowski_sum,
    orient_cycle,
    signed_step_set,
)

MAX_B = 32
MAX_REQUESTS = 10
BRUTE_FORCE_CAP = 2_000_000


# ---------------------------------------------------------------------------
# 有理工具
# ---------------------------------------------------------------------------


def parse_rational(text: str) -> Fraction:
    """接受 '3'、'3/2'、'1.5' 形式的有理数，绝不使用浮点余数。"""
    return Fraction(text)


def frac_to_dict(q: Fraction) -> dict[str, object]:
    return {"num": q.numerator, "den": q.denominator, "text": str(q)}


# ---------------------------------------------------------------------------
# 单次分析（纯函数式核心，便于测试与复跑）
# ---------------------------------------------------------------------------


def run_analysis(
    product: dict[str, object],
    scales: dict[str, Fraction],
    bound_b: int,
    anchors: dict[str, int],
    *,
    record: bool = True,
    cross_check: bool = True,
) -> dict[str, object]:
    """执行一次完整分析并返回报告字典。"""
    validate_product(product)
    if not isinstance(bound_b, int) or not (0 <= bound_b <= MAX_B):
        raise ValueError(f"B 必须为 0..{MAX_B} 的整数")
    nodes: list[str] = list(product["nodes"])
    node_set = set(nodes)
    m: int = product["m"]

    for n in nodes:
        if n not in scales:
            raise ValueError(f"节点 {n!r} 缺少 scale")
        if not isinstance(scales[n], Fraction) or scales[n] <= 0:
            raise ValueError(f"节点 {n!r} 的 scale 必须为正有理数")
    for n in anchors:
        if n not in node_set:
            raise ValueError(f"锚点引用未知节点 {n!r}")
        if not isinstance(anchors[n], int):
            raise ValueError(f"锚点 {n!r} 的值必须为整数")
        if not (-bound_b <= anchors[n] <= bound_b):
            raise ValueError(
                f"锚点 {n}={anchors[n]} 超出全局整数相位界 [-{bound_b},{bound_b}]，明确拒绝"
            )

    edges_payload = product["edges"]
    binary = [
        BinaryConstraint(
            cid=f"e{e['edge_id']}",
            u=e["u"],
            v=e["v"],
            allowed=frozenset(e["candidates"]),
        )
        for e in edges_payload
    ]
    edge_by_cid = {c.cid: e for c, e in zip(binary, edges_payload)}

    # 一元约束：全局界 + 锚点。
    unary: dict[str, list[UnaryConstraint]] = {n: [] for n in nodes}
    for n in nodes:
        unary[n].append(
            UnaryConstraint(f"bound[{n}]", n, frozenset(range(-bound_b, bound_b + 1)))
        )
    for n, value in anchors.items():
        unary[n].append(UnaryConstraint(f"anchor[{n}]={value}", n, frozenset({value})))

    domains = {n: (-bound_b, bound_b) for n in nodes}
    components = connected_components(nodes, binary)
    anchored_component = [
        any(n in anchors for n in comp) for comp in components
    ]

    diagnostics = _structural_diagnostics(nodes, binary, edge_by_cid, anchors)

    solver = Solver(nodes, binary, unary, domains)
    first = solver.solve(cap=2, record=record)

    if not first.feasible:
        core = _unsat_core(nodes, binary, unary, domains, bound_b, anchors)
        report: dict[str, object] = {
            "status": "INCONSISTENT",
            "m": m,
            "bound_b": bound_b,
            "nodes": nodes,
            "anchors": dict(anchors),
            "components": [
                {
                    "nodes": comp,
                    "has_anchor": anchored_component[i],
                    "note": "无锚分量不会被钉零" if not anchored_component[i] else "",
                }
                for i, comp in enumerate(components)
            ],
            "diagnostics": diagnostics,
            "unsat_core": core,
            "replay": {
                "method": "AC-3 弧一致性 + MRV 回溯（整数运算）",
                "initial_domains": {n: [-bound_b, bound_b] for n in nodes},
                "constraints": _constraint_inventory(binary, unary, anchors),
                "ac3_revisions": first.revisions,
                "wipeout": first.wipeout,
                "note": "按记录顺序可完整重放该反证搜索；修订即被删除的值",
            },
        }
        return report

    # ---- 可行：逐节点极值（同一全图可行域上的独立优化） ----
    lex_smallest = first.solutions[0]
    unique_solution = not first.truncated and len(first.solutions) == 1

    node_reports: dict[str, dict[str, object]] = {}
    min_vector: dict[str, int] = {}
    max_vector: dict[str, int] = {}
    for n in nodes:
        ext = solver.extrema(n)
        assert ext is not None
        lo_v, hi_v, lo_witness, hi_witness = ext
        min_vector[n] = lo_v
        max_vector[n] = hi_v
        s = scales[n]
        node_reports[n] = {
            "phase_min": lo_v,
            "phase_max": hi_v,
            "unique_phase": lo_v == hi_v,
            "min_witness": lo_witness,
            "max_witness": hi_witness,
            "displacement_min": frac_to_dict(Fraction(lo_v) * s),
            "displacement_max": frac_to_dict(Fraction(hi_v) * s),
        }

    # 不同节点的极值不保证同时达到：显式验证两个极值向量本身是否可行。
    joint_min_feasible = solver.solve(
        cap=1, extra_unary={n: frozenset({v}) for n, v in min_vector.items()}
    ).feasible
    joint_max_feasible = solver.solve(
        cap=1, extra_unary={n: frozenset({v}) for n, v in max_vector.items()}
    ).feasible

    # ---- 无锚分量平移歧义 ----
    component_info = []
    for i, comp in enumerate(components):
        info: dict[str, object] = {
            "nodes": comp,
            "has_anchor": anchored_component[i],
        }
        if not anchored_component[i]:
            shift_plus = solver.solve(
                cap=1,
                extra_unary={
                    n: frozenset({lex_smallest[n] + 1}) for n in comp
                },
            ).feasible
            shift_minus = solver.solve(
                cap=1,
                extra_unary={
                    n: frozenset({lex_smallest[n] - 1}) for n in comp
                },
            ).feasible
            info["translation_freedom"] = {
                "shift_plus_one_feasible": shift_plus,
                "shift_minus_one_feasible": shift_minus,
                "note": "该分量无锚点，整体平移不被约束；极值仅由全局界 [-B,B] 截断",
            }
        component_info.append(info)

    cross = None
    if cross_check:
        cross = _brute_force_cross_check(
            nodes, binary, unary, domains, lex_smallest, unique_solution, node_reports
        )

    return {
        "status": "CONSISTENT",
        "m": m,
        "bound_b": bound_b,
        "nodes": nodes,
        "anchors": dict(anchors),
        "unique_solution_overall": unique_solution,
        "lexicographically_smallest_solution": lex_smallest,
        "example_note": "仅为按节点 ID 字典序最小的示例解；多解时不代表唯一解",
        "joint_extrema": {
            "all_min_vector": min_vector,
            "all_max_vector": max_vector,
            "all_min_jointly_feasible": joint_min_feasible,
            "all_max_jointly_feasible": joint_max_feasible,
            "note": "各节点极值是同一可行域上的独立优化，彼此不保证可同时达到",
        },
        "components": component_info,
        "node_results": node_reports,
        "diagnostics": diagnostics,
        "brute_force_cross_check": cross,
    }


# ---------------------------------------------------------------------------
# 结构诊断：同向来源冲突 + 回路残差 + 锚点说明
# ---------------------------------------------------------------------------


def _structural_diagnostics(
    nodes: list[str],
    binary: list[BinaryConstraint],
    edge_by_cid: dict[str, dict[str, object]],
    anchors: dict[str, int],
) -> dict[str, object]:
    problems: list[dict[str, object]] = []

    pair_map: dict[tuple[str, str], list[BinaryConstraint]] = {}
    for c in binary:
        pair_map.setdefault((c.u, c.v), []).append(c)
    for (u, v), cs in sorted(pair_map.items()):
        if len(cs) < 2:
            continue
        inter: Optional[frozenset[int]] = None
        per_edge = {}
        for c in cs:
            per_edge[c.cid] = sorted(c.allowed)
            inter = c.allowed if inter is None else inter & c.allowed
        assert inter is not None
        if not inter:
            problems.append(
                {
                    "kind": "same_direction_conflict",
                    "u": u,
                    "v": v,
                    "edge_ids": [edge_by_cid[c.cid]["edge_id"] for c in cs],
                    "per_edge": per_edge,
                    "message": f"同向边 {[c.cid for c in cs]} 候选交集为空，必须同时满足故不相容",
                }
            )

    for ci, cyc in enumerate(cycle_basis(nodes, binary)):
        oriented = orient_cycle(cyc)
        if oriented is None:
            continue
        totals = minkowski_sum(
            signed_step_set(e, forward) for e, forward in oriented
        )
        if 0 not in totals:
            problems.append(
                {
                    "kind": "cycle_residual",
                    "cycle_index": ci,
                    "edge_ids": [edge_by_cid[e.cid]["edge_id"] for e, _ in oriented],
                    "constraint_ids": [e.cid for e, _ in oriented],
                    "residual_values": sorted(totals),
                    "message": (
                        f"回路闭合残差候选 {sorted(totals)} 不含 0，"
                        "累加树结果不满足闭合回路"
                    ),
                }
            )

    anchor_notes = [
        {"node": n, "value": v, "message": f"锚点 x_{n} = {v} 作为一元约束参与求解"}
        for n, v in sorted(anchors.items())
    ]
    return {"problems": problems, "anchors": anchor_notes, "ok": not problems}


# ---------------------------------------------------------------------------
# 不可满足核心：逐个尝试移除可移除约束，保留所有仍为 UNSAT 所必需者
# ---------------------------------------------------------------------------


def _unsat_core(
    nodes: list[str],
    binary: list[BinaryConstraint],
    unary: dict[str, list[UnaryConstraint]],
    domains: dict[str, tuple[int, int]],
    bound_b: int,
    anchors: dict[str, int],
) -> dict[str, object]:
    removable: list[str] = [c.cid for c in binary] + [
        uc.cid for n in nodes for uc in unary[n] if uc.cid.startswith("anchor[")
    ]
    core: list[str] = list(removable)

    def feasible_without(drop: set[str]) -> bool:
        b2 = [c for c in binary if c.cid not in drop]
        u2 = {
            n: [uc for uc in unary[n] if uc.cid not in drop] for n in nodes
        }
        trial = Solver(nodes, b2, u2, domains)
        return trial.solve(cap=1).feasible

    # 全局界本身不可移除；先确认界与空约束系统是否相容（B>=0 恒真）。
    for cid in list(core):
        trial_drop = set(core) - {cid}
        if not feasible_without(trial_drop):
            # 去掉 cid 后仍不相容：cid 不是必需的
            core = [c for c in core if c != cid]

    detail = []
    for cid in core:
        if cid.startswith("e"):
            detail.append({"constraint_id": cid, "kind": "edge"})
        elif cid.startswith("anchor["):
            detail.append({"constraint_id": cid, "kind": "anchor"})
        else:
            detail.append({"constraint_id": cid, "kind": "bound"})
    # 复核：完整问题确为 UNSAT；核心最小（移除核心中任一约束后恢复可满足）。
    assert not feasible_without(set())
    minimal = all(
        feasible_without(set(core) - {cid}) for cid in core
    )
    return {
        "constraint_ids": core,
        "items": detail,
        "minimal_by_deletion": minimal,
        "message": "该子集本身不相容，且删除其中任一约束后恢复可满足",
    }


def _constraint_inventory(
    binary: list[BinaryConstraint],
    unary: dict[str, list[UnaryConstraint]],
    anchors: dict[str, int],
) -> dict[str, object]:
    return {
        "binary": [
            {
                "id": c.cid,
                "u": c.u,
                "v": c.v,
                "allowed_differences": sorted(c.allowed),
            }
            for c in binary
        ],
        "unary": [
            {"id": uc.cid, "node": n, "allowed": sorted(uc.allowed)}
            for n in sorted(unary)
            for uc in unary[n]
        ],
    }


# ---------------------------------------------------------------------------
# 独立全枚举交叉核对（独立代码路径，不共享 AC-3 状态）
# ---------------------------------------------------------------------------


def _brute_force_cross_check(
    nodes: list[str],
    binary: list[BinaryConstraint],
    unary: dict[str, list[UnaryConstraint]],
    domains: dict[str, tuple[int, int]],
    lex_smallest: dict[str, int],
    unique_solution: bool,
    node_reports: dict[str, dict[str, object]],
) -> dict[str, object]:
    all_solutions = brute_force_enumerate(
        sorted(nodes), binary, unary, domains, cap=BRUTE_FORCE_CAP
    )
    if all_solutions is None:
        return {
            "performed": False,
            "reason": f"组合空间超过 {BRUTE_FORCE_CAP}，跳过全枚举核对",
        }
    ordered = sorted(all_solutions, key=lambda a: tuple(a[n] for n in sorted(nodes)))
    mismatches: list[str] = []
    if not ordered:
        mismatches.append("枚举无解但主求解器报告有解")
    else:
        if ordered[0] != lex_smallest:
            mismatches.append("字典序最小解与枚举结果不一致")
        if unique_solution != (len(ordered) == 1):
            mismatches.append("唯一解标记与枚举解数不一致")
        for n in sorted(nodes):
            vals = [a[n] for a in ordered]
            if min(vals) != node_reports[n]["phase_min"]:
                mismatches.append(f"节点 {n} 最小值与枚举不一致")
            if max(vals) != node_reports[n]["phase_max"]:
                mismatches.append(f"节点 {n} 最大值与枚举不一致")
    return {
        "performed": True,
        "solution_count": len(ordered),
        "mismatches": mismatches,
        "passed": not mismatches,
    }


# ---------------------------------------------------------------------------
# 分析仓（SQLite 持久化，消费公开产品）
# ---------------------------------------------------------------------------


class AnalysisRepository:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS products(
                product_id TEXT PRIMARY KEY,
                imported_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                origin TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests(
                request_id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                params TEXT NOT NULL,
                report TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def count_requests(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

    def import_product(
        self,
        payload: dict[str, object],
        product_id: Optional[str] = None,
        origin: str = "external",
    ) -> str:
        """导入公开产品；只看产品内容，不访问目录编辑表。"""
        validate_product(payload)
        if len(payload["nodes"]) > MAX_NODES or len(payload["edges"]) > MAX_EDGES:
            raise ValueError("产品超出规模上限")
        if product_id is None:
            import hashlib

            body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            product_id = "P" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        existing = self.conn.execute(
            "SELECT payload FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        if existing is not None:
            if json.loads(existing[0]) != payload:
                raise ValueError(f"product_id {product_id} 已绑定不同产品，拒绝覆盖")
            return product_id
        self.conn.execute(
            "INSERT INTO products(product_id, imported_at, payload, origin) VALUES(?,?,?,?)",
            (product_id, _now(), json.dumps(payload, ensure_ascii=False), origin),
        )
        self.conn.commit()
        return product_id

    def list_products(self) -> list[dict[str, object]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT product_id, imported_at, origin FROM products ORDER BY imported_at"
            ).fetchall()
        ]

    def get_product(self, product_id: str) -> dict[str, object]:
        row = self.conn.execute(
            "SELECT payload FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"仓库中无产品 {product_id}")
        return json.loads(row[0])

    def analyze(
        self,
        request_id: str,
        product_id: str,
        scales: dict[str, Fraction],
        bound_b: int,
        anchors: Optional[dict[str, int]] = None,
    ) -> dict[str, object]:
        """对固定产品+固定锚点执行分析并冻结报告；请求数受上限约束。"""
        if self.conn.execute(
            "SELECT 1 FROM requests WHERE request_id=?", (request_id,)
        ).fetchone():
            raise ValueError(f"request_id {request_id} 已存在；旧分析不可覆盖")
        if self.count_requests() >= MAX_REQUESTS:
            raise ValueError(f"分析请求数超过上限 {MAX_REQUESTS}")
        product = self.get_product(product_id)
        anchors = anchors or {}
        report = run_analysis(product, scales, bound_b, anchors)
        params = {
            "scales": {n: str(q) for n, q in scales.items()},
            "bound_b": bound_b,
            "anchors": dict(anchors),
        }
        self.conn.execute(
            """INSERT INTO requests(request_id, product_id, created_at, params, report)
               VALUES(?,?,?,?,?)""",
            (
                request_id,
                product_id,
                _now(),
                json.dumps(params, ensure_ascii=False),
                json.dumps(report, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return report

    def get_report(self, request_id: str) -> dict[str, object]:
        row = self.conn.execute(
            "SELECT report FROM requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"未知分析请求 {request_id}")
        return json.loads(row[0])

    def list_requests(self) -> list[dict[str, object]]:
        rows = self.conn.execute(
            "SELECT request_id, product_id, created_at FROM requests ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
