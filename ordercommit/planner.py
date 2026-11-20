"""采购计划优化器：小数量穷举 + 逐 SKU 子问题缓存。

目标字典（严格按顺序）：
    1. 可行：每个 SKU 的采购总量覆盖全部门店需求，且不超过逐 SKU 超买上限；
    2. 最小化真实供应商总报价（排序阶段用规则试算价，执行阶段以不可变报价/承诺为准）；
    3. 最小化超买件数；
    4. 按 (供应商ID, SKU) 排序的数量向量字典序定序，保证结果确定。

取舍说明（见模块文档字符串末尾 explain_tradeoffs）：
    规模为 3 供应商 × 5 SKU × 每 SKU ≤30 件，合法采购量是包装倍数的稀疏网格，
    逐 SKU 候选分配数很小；对 SKU 子问题做缓存后再做跨 SKU 笛卡尔积即可全量搜索，
    无需引入 MILP 求解器。串行优化下结果可完整复现、可解释。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from itertools import product
from typing import Any

from .errors import InfeasiblePlan
from .pricing import SkuRule


@dataclass(frozen=True)
class DemandLine:
    demand_id: str
    sku: str
    qty: int
    priority: int
    due_date: str


@dataclass
class SupplierCatalogEntry:
    supplier_id: str
    version_id: str
    rules: dict[str, SkuRule]
    remaining: dict[str, int]  # 计划时刻各 SKU 剩余额度（接受时仍会重新核对）


@dataclass
class Plan:
    quantities: dict[tuple[str, str], int]          # (supplier_id, sku) -> qty
    needed: dict[str, int]
    estimated_cost: int
    overbuy: dict[str, int]
    supplier_subtotals: dict[str, int]
    gaps: dict[str, dict[str, Any]] = field(default_factory=dict)
    actual_cost: int | None = None                  # 真实报价接受后回填
    commitment_nos: list[str] = field(default_factory=list)

    def vector(self) -> tuple[int, ...]:
        return tuple(self.quantities[k] for k in sorted(self.quantities))

    def supplier_items(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for (supplier_id, sku), qty in sorted(self.quantities.items()):
            out.setdefault(supplier_id, []).append({"sku": sku, "qty": qty})
        return out


class PurchasePlanner:
    """对给定供应商目录与需求向量做全量穷举。"""

    def __init__(self, catalog: list[SupplierCatalogEntry],
                 overbuy_caps: dict[str, int]) -> None:
        self.catalog = catalog
        self.suppliers = sorted(c.supplier_id for c in catalog)
        self._entry = {c.supplier_id: c for c in catalog}
        self.overbuy_caps = {s: int(c) for s, c in overbuy_caps.items()}

    # ------------------------------------------------------------------ 子问题
    @lru_cache(maxsize=None)
    def _feasible_quantities(self, supplier_id: str, sku: str,
                             upper: int) -> tuple[int, ...]:
        """单供应商单 SKU 合法量网格，上限取 min(规则上限, 当前剩余额度, 搜索上界)。"""
        entry = self._entry[supplier_id]
        rule = entry.rules.get(sku)
        if rule is None:
            return (0,)
        cap = min(rule.capacity, entry.remaining.get(sku, rule.capacity), upper)
        return tuple(rule.feasible_quantities(limit=cap))

    @lru_cache(maxsize=None)
    def sku_allocations(self, sku: str, needed: int,
                        overbuy_cap: int) -> tuple[tuple[tuple[str, int], ...], ...]:
        """枚举一个 SKU 覆盖 needed 的全部跨供应商分配（子问题缓存）。

        返回 ((supplier_id, qty), ...) 的元组，供应商顺序固定；含零量供应商，
        以便最终向量按 (供应商, SKU) 对齐定序。
        """
        upper = needed + overbuy_cap
        per_supplier = [
            self._feasible_quantities(sid, sku, upper) for sid in self.suppliers
        ]
        results: list[tuple[tuple[str, int], ...]] = []
        for combo in product(*per_supplier):
            total = sum(combo)
            if needed <= total <= upper:
                results.append(tuple(zip(self.suppliers, combo)))
        return tuple(results)

    def _allocation_cost(self, sku: str,
                         alloc: tuple[tuple[str, int], ...]) -> int:
        total = 0
        for supplier_id, qty in alloc:
            rule = self._entry[supplier_id].rules.get(sku)
            total += rule.line_price(qty) if rule is not None and qty else 0
        return total

    # ------------------------------------------------------------------ 主优化
    def plan(self, demands: list[DemandLine]) -> Plan:
        needed: dict[str, int] = {}
        for d in demands:
            if d.qty <= 0:
                raise InfeasiblePlan("需求数量必须为正", details={"demand_id": d.demand_id})
            needed[d.sku] = needed.get(d.sku, 0) + d.qty

        sku_options: dict[str, tuple[tuple[tuple[str, int], ...], ...]] = {}
        gaps: dict[str, dict[str, Any]] = {}
        for sku, qty_needed in sorted(needed.items()):
            cap = self.overbuy_caps.get(sku, 0)
            allocs = self.sku_allocations(sku, qty_needed, cap)
            if not allocs:
                max_attainable = 0
                detail = {}
                for sid in self.suppliers:
                    qs = self._feasible_quantities(sid, sku, qty_needed + cap)
                    detail[sid] = {"max": max(qs), "options": list(qs)}
                    max_attainable += max(qs)
                gaps[sku] = {
                    "needed": qty_needed,
                    "overbuy_cap": cap,
                    "max_attainable": max_attainable,
                    "shortage": qty_needed - max_attainable,
                    "per_supplier": detail,
                }
            else:
                sku_options[sku] = allocs

        if gaps:
            raise InfeasiblePlan(
                "存在无法满足的 SKU 缺口（包装/最低量/上限/超买限制下无可行采购量）",
                details={"gaps": gaps, "needed": needed},
            )

        best: Plan | None = None
        best_key: tuple | None = None
        sku_names = sorted(sku_options)
        for combo in product(*(sku_options[s] for s in sku_names)):
            quantities: dict[tuple[str, str], int] = {}
            cost = 0
            overbuy_total = 0
            overbuy: dict[str, int] = {}
            subtotals: dict[str, int] = {sid: 0 for sid in self.suppliers}
            for sku, alloc in zip(sku_names, combo):
                line_cost = self._allocation_cost(sku, alloc)
                cost += line_cost
                bought = sum(q for _, q in alloc)
                overbuy[sku] = bought - needed[sku]
                overbuy_total += overbuy[sku]
                for supplier_id, qty in alloc:
                    quantities[(supplier_id, sku)] = qty
                    if qty:
                        rule = self._entry[supplier_id].rules[sku]
                        subtotals[supplier_id] += rule.line_price(qty)
            vec = tuple(quantities[k] for k in sorted(quantities))
            key = (cost, overbuy_total, vec)
            if best_key is None or key < best_key:
                best_key = key
                best = Plan(
                    quantities=quantities,
                    needed=dict(needed),
                    estimated_cost=cost,
                    overbuy=overbuy,
                    supplier_subtotals=subtotals,
                )
        assert best is not None and best_key is not None
        return best

    # ------------------------------------------------------------------ 解释
    def explain_plan(self, plan: Plan) -> dict[str, Any]:
        lines = []
        for sku in sorted(plan.needed):
            chosen = []
            for sid in self.suppliers:
                qty = plan.quantities.get((sid, sku), 0)
                rule = self._entry[sid].rules.get(sku)
                chosen.append({
                    "supplier_id": sid,
                    "qty": qty,
                    "unit_price": rule.applicable_unit_price(qty) if rule and qty else 0,
                    "line_price": rule.line_price(qty) if rule and qty else 0,
                    "pack_multiple": rule.pack_multiple if rule else None,
                })
            lines.append({
                "sku": sku,
                "needed": plan.needed[sku],
                "bought": sum(c["qty"] for c in chosen),
                "overbuy": plan.overbuy[sku],
                "suppliers": chosen,
            })
        return {
            "objective_order": ["可行性(覆盖需求且不超超买上限)", "总报价最小",
                                "超买件数最小", "(供应商ID,SKU)向量字典序"],
            "estimated_cost": plan.estimated_cost,
            "actual_cost": plan.actual_cost,
            "overbuy_total": sum(plan.overbuy.values()),
            "lines": lines,
        }


TRADEOFF_NOTES = {
    "exhaustive_search": (
        "全量搜索：合法采购量=包装倍数网格（每 SKU ≤30 件时网格点极少），"
        "先枚举单 SKU 跨供应商分配并按 (SKU, needed, overbuy_cap) 缓存子问题，"
        "再做跨 SKU 笛卡尔积；目标键 (成本, 超买, 向量序) 一次比较完成三层优选。"
        "代价是候选数随供应商数指数增长，仅适合题面小规格；更大规模应换 MILP/DP。"
    ),
    "subproblem_cache": (
        "子问题缓存：同一 SKU 在不同超买上界/需求组合下复用 lru_cache 结果，"
        "避免重复生成网格与笛卡尔组合；缓存键只依赖不可变目录快照。"
    ),
    "serial_txn": (
        "库存串行事务：SQLite 单库多连接下写事务一律 BEGIN IMMEDIATE 取写锁，"
        "配合台账条件更新 (allocated_qty + n <= demand.qty) 做版本条件更新，"
        "两个连接争同一库存时后者阻塞后读到已提交状态，天然不会超发；"
        "吞吐让位于正确性与可审计性，发放记录只追加、从不改写。"
    ),
}
