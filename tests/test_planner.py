"""穷举优化器测试：用独立朴素枚举交叉验证最优解与定序、超买、缺口。"""

import unittest
from itertools import product

from ordercommit.errors import InfeasiblePlan
from ordercommit.planner import (
    DemandLine,
    PurchasePlanner,
    SupplierCatalogEntry,
)
from ordercommit.pricing import SkuRule


def catalog() -> list[SupplierCatalogEntry]:
    return [
        SupplierCatalogEntry("S1", "V1",
                             {"X": SkuRule(2, 0, 12, [(0, 10), (8, 7)]),
                              "Y": SkuRule(5, 0, 10, [(0, 4)])},
                             {"X": 12, "Y": 10}),
        SupplierCatalogEntry("S2", "V2",
                             {"X": SkuRule(3, 0, 9, [(0, 9)])},
                             {"X": 9}),
    ]


def demands() -> list[DemandLine]:
    return [DemandLine("d1", "X", 5, 5, "2026-11-20"),
            DemandLine("d2", "Y", 4, 3, "2026-11-21")]


class PlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = PurchasePlanner(catalog(), overbuy_caps={"X": 3, "Y": 2})

    def _brute_force_best(self, needed: dict[str, int],
                          caps: dict[str, int]) -> tuple[int, int, tuple]:
        """不依赖被测代码的朴素全枚举，返回 (成本, 超买, 向量) 最优键。"""
        suppliers = self.planner.suppliers
        best = None
        skus = sorted(needed)
        grids = {}
        for sku in skus:
            grids[sku] = []
            for sid in suppliers:
                entry = self.planner._entry[sid]
                rule = entry.rules.get(sku)
                if rule is None:
                    grids[sku].append([(sid, 0)])
                else:
                    cap = min(rule.capacity, entry.remaining.get(sku, rule.capacity),
                              needed[sku] + caps[sku])
                    grids[sku].append([(sid, q) for q in rule.feasible_quantities(cap)])
        for combo in product(*[product(*grids[s]) for s in skus]):
            quantities = {}
            cost = over = 0
            for sku, alloc in zip(skus, combo):
                total = sum(q for _, q in alloc)
                if not (needed[sku] <= total <= needed[sku] + caps[sku]):
                    break
                over += total - needed[sku]
                for sid, q in alloc:
                    quantities[(sid, sku)] = q
                    rule = self.planner._entry[sid].rules.get(sku)
                    if rule and q:
                        cost += rule.line_price(q)
            else:
                vec = tuple(quantities[k] for k in sorted(quantities))
                key = (cost, over, vec)
                if best is None or key < best:
                    best = key
        assert best is not None
        return best

    def test_matches_independent_brute_force(self) -> None:
        plan = self.planner.plan(demands())
        needed = {"X": 5, "Y": 4}
        expected = self._brute_force_best(needed, {"X": 3, "Y": 2})
        actual = (plan.estimated_cost, sum(plan.overbuy.values()), plan.vector())
        self.assertEqual(actual, expected)

    def test_demand_covered_and_overbuy_respected(self) -> None:
        plan = self.planner.plan(demands())
        bought = {}
        for (_, sku), q in plan.quantities.items():
            bought[sku] = bought.get(sku, 0) + q
        self.assertGreaterEqual(bought["X"], 5)
        self.assertLessEqual(bought["X"], 5 + 3)
        self.assertGreaterEqual(bought["Y"], 4)
        self.assertLessEqual(bought["Y"], 4 + 2)

    def test_infeasible_reports_explicit_gap(self) -> None:
        # Y 只在 S1 有售：包装 5、上限 10；需求 12 且超买上限 0 → 无法恰好满足
        cat = [
            SupplierCatalogEntry("S1", "V1",
                                 {"Y": SkuRule(5, 0, 10, [(0, 4)])}, {"Y": 10}),
            SupplierCatalogEntry("S2", "V2", {}, {}),
        ]
        tight = PurchasePlanner(cat, overbuy_caps={"Y": 0})
        with self.assertRaises(InfeasiblePlan) as ctx:
            tight.plan([DemandLine("d1", "Y", 12, 5, "2026-11-20")])
        gap = ctx.exception.details["gaps"]["Y"]
        self.assertGreater(gap["shortage"], 0)
        self.assertEqual(gap["max_attainable"], 10)

    def test_tie_break_by_vector_ordering(self) -> None:
        # 两个供应商同价同包装时，(供应商,SKU) 向量取字典序最小，结果确定
        cat = [
            SupplierCatalogEntry("S1", "V1",
                                 {"Z": SkuRule(2, 0, 10, [(0, 5)])}, {"Z": 10}),
            SupplierCatalogEntry("S2", "V2",
                                 {"Z": SkuRule(2, 0, 10, [(0, 5)])}, {"Z": 10}),
        ]
        p = PurchasePlanner(cat, {"Z": 0})
        plan = p.plan([DemandLine("d", "Z", 4, 1, "2026-11-20")])
        vec = plan.vector()
        # 重跑结果完全一致（确定性），且向量字典序最小：靠前的 S1 位尽量小
        self.assertEqual(plan.vector(), PurchasePlanner(cat, {"Z": 0}).plan(
            [DemandLine("d", "Z", 4, 1, "2026-11-20")]).vector())
        self.assertEqual(vec, min([(4, 0), (0, 4), (2, 2)]))
        self.assertEqual(sum(vec), 4)

    def test_subproblem_cache_reused(self) -> None:
        self.planner.sku_allocations("X", 5, 3)
        info1 = self.planner.sku_allocations.cache_info()
        self.planner.sku_allocations("X", 5, 3)
        info2 = self.planner.sku_allocations.cache_info()
        self.assertEqual(info2.hits, info1.hits + 1)

    def test_overbuy_cheaper_taken_when_it_lowers_total(self) -> None:
        # X 买 5 件：5*10=50；买 8 件进低价档：8*7=56 → 更贵，不应超买
        plan = self.planner.plan([DemandLine("d", "X", 5, 1, "t")])
        self.assertEqual(plan.overbuy["X"], 0)
        # 调到需要 7 件：7*10=70；8*7=56，多买 1 件更便宜 → 应超买
        plan2 = self.planner.plan([DemandLine("d", "X", 7, 1, "t")])
        self.assertEqual(plan2.overbuy["X"], 1)
        self.assertEqual(plan2.estimated_cost, 56)


if __name__ == "__main__":
    unittest.main()
