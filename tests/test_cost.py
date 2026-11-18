"""成本仓测试：有理数传递、零价不可计价、一分钱补分、重算与增量对拍。"""

import unittest
from fractions import Fraction

from ledger_cost.cost import CostWarehouse
from ledger_cost.engine import largest_remainder_allocation, round_half_up
from ledger_cost.errors import NotFound, VersionError
from ledger_cost.production import ProductionLedger
from ledger_cost.storage import Storage


def fresh():
    storage = Storage(":memory:")
    ledger = ProductionLedger(storage)
    return storage, ledger, CostWarehouse(storage, ledger)


class TestRounding(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(round_half_up(Fraction(1, 2)), 1)
        self.assertEqual(round_half_up(Fraction(3, 2)), 2)
        self.assertEqual(round_half_up(Fraction(2, 3)), 1)
        self.assertEqual(round_half_up(Fraction(1, 3)), 0)
        self.assertEqual(round_half_up(Fraction(0)), 0)

    def test_largest_remainder_tie_by_lot_id(self):
        # 1 分钱、三路等权重：L7A 字典序最前，补到 1 分
        alloc = largest_remainder_allocation(
            1, [("L7C", Fraction(1)), ("L7A", Fraction(1)), ("L7B", Fraction(1))]
        )
        self.assertEqual(alloc, {"L7A": 1, "L7B": 0, "L7C": 0})
        self.assertEqual(sum(alloc.values()), 1)

    def test_largest_remainder_general(self):
        alloc = largest_remainder_allocation(
            10, [("a", Fraction(1)), ("b", Fraction(1)), ("c", Fraction(1))]
        )
        self.assertEqual(sum(alloc.values()), 10)
        self.assertEqual(alloc["a"], 4)  # 3,3,4 → 余数并列按 id，c 补分
        self.assertEqual(alloc["b"], 3)
        self.assertEqual(alloc["c"], 3)


class TestExactPropagation(unittest.TestCase):
    def test_partial_input_retains_cost(self):
        _, led, wh = fresh()
        led.register_root_lot("R1", 1000)
        led.register_batch("B1", "R1", 600, [("L1A", 400), ("L1B", 180)], 20)
        wh.set_opening_cost("r", "R1", 10000)
        wh.set_processing_fee("f", "B1", 500)
        wh.set_reference_value("v", "L1A", 10)
        wh.set_reference_value("v", "L1B", 10)
        wh.compute_cut("c1", "首切面", "r", "f", "v")

        root = wh.get_product("c1", "R1")
        # 单位精确成本 10 分/g；留存 400g → 留存精确成本 4000 分
        self.assertEqual(root.exact_unit_cost, Fraction(10))
        retained = wh.retained_cost("c1", "R1")
        self.assertEqual(retained["retained_grams"], 400)
        self.assertEqual(retained["exact_retained_cost"], Fraction(4000))

        b1 = wh.get_batch_cost("c1", "B1")
        # 投入成本 6000 + 加工费 500 = 6500，权重 4000:1800
        self.assertEqual(b1.pool_exact, Fraction(6500))
        self.assertEqual(b1.pool_published, 6500)
        pa = wh.get_product("c1", "L1A")
        pb = wh.get_product("c1", "L1B")
        self.assertEqual(pa.exact_total_cost, Fraction(6500) * 4000 / 5800)
        self.assertEqual(pb.exact_total_cost, Fraction(6500) * 1800 / 5800)
        self.assertEqual(pa.published_total_cost + pb.published_total_cost, 6500)
        # 损耗不携带可出售成本：两个产出已分完整个池
        self.assertEqual(
            pa.exact_total_cost + pb.exact_total_cost, Fraction(6500)
        )

    def test_three_level_chain_uses_exact_not_layered_rounding(self):
        _, led, wh = fresh()
        led.register_root_lot("R1", 300)
        led.register_batch("B1", "R1", 100, [("L1", 90)], 10)
        led.register_batch("B2", "L1", 90, [("L2", 80)], 10)
        led.register_batch("B3", "L2", 80, [("L3", 70)], 10)
        wh.set_opening_cost("r", "R1", 1000)
        for b in ("B1", "B2", "B3"):
            wh.set_processing_fee("f", b, 0)
        for lot in ("L1", "L2", "L3"):
            wh.set_reference_value("v", lot, 7)
        wh.compute_cut("c1", "链", "r", "f", "v")
        # 精确传递：根单位 1000/300=10/3；B1 投入 100g 成本 1000/3，
        # 全部由 90g 的 L1 承接（损耗不分摊）→ L1 单位 = (1000/3)/90 = 100/27
        p1 = wh.get_product("c1", "L1")
        p3 = wh.get_product("c1", "L3")
        self.assertEqual(p1.exact_unit_cost, Fraction(100, 27))
        # 下游只消耗 80g/70g，单位成本沿精确值逐批传递，发布值不参与
        self.assertEqual(p3.exact_total_cost, Fraction(1000, 3))
        self.assertEqual(p3.published_total_cost, 333)  # 333.333... 四舍五入
        # 下游传递的是精确值而非发布值：L3 精确成本未受 333 影响
        self.assertNotEqual(p3.exact_total_cost, Fraction(333))

    def test_zero_reference_makes_branch_unpriceable_not_zero(self):
        _, led, wh = fresh()
        led.register_root_lot("R1", 100)
        led.register_root_lot("R2", 100)
        led.register_batch("B1", "R1", 50, [("L1", 50)], 0)
        led.register_batch("B2", "R2", 50, [("L2", 50)], 0)
        led.register_batch("B3", "L2", 50, [("L3", 50)], 0)
        wh.set_opening_cost("r", "R1", 500)
        wh.set_opening_cost("r", "R2", 800)
        wh.set_processing_fee("f", "B1", 0)
        wh.set_processing_fee("f", "B2", 0)
        wh.set_processing_fee("f", "B3", 0)
        wh.set_reference_value("v", "L1", 10)
        wh.set_reference_value("v", "L2", 0)   # 零参考价值
        wh.set_reference_value("v", "L3", 10)  # 下游有价值也无济于事
        wh.compute_cut("c1", "零价分支", "r", "f", "v")
        p1 = wh.get_product("c1", "L1")
        p2 = wh.get_product("c1", "L2")
        p3 = wh.get_product("c1", "L3")
        self.assertTrue(p1.priceable)
        self.assertFalse(p2.priceable)
        self.assertIsNone(p2.exact_total_cost)
        self.assertIsNone(p2.published_total_cost)
        self.assertFalse(p3.priceable)  # 依赖批同样不可计价
        self.assertIn("分母为零", p2.reason)

    def test_one_cent_allocation(self):
        _, led, wh = fresh()
        led.register_root_lot("R3", 10)
        led.register_batch("B7", "R3", 3, [("L7A", 1), ("L7B", 1), ("L7C", 1)], 0)
        # R3 总成本 2 分 → 单位 0.2 分/g；投入 3g 的精确池 0.6 分，四舍五入为 1 分
        wh.set_opening_cost("r", "R3", 2)
        wh.set_processing_fee("f", "B7", 0)
        for lot in ("L7A", "L7B", "L7C"):
            wh.set_reference_value("v", lot, 1)
        wh.compute_cut("c1", "一分钱", "r", "f", "v")
        alloc = {lot: wh.get_product("c1", lot).published_total_cost
                 for lot in ("L7A", "L7B", "L7C")}
        self.assertEqual(sum(alloc.values()), 1)
        self.assertEqual(alloc["L7A"], 1)  # 余数并列按 lot_id
        self.assertEqual(alloc["L7B"], 0)
        self.assertEqual(alloc["L7C"], 0)
        # 精确值仍是三份均分（各 0.2 分），没有被发布值污染
        for lot in ("L7A", "L7B", "L7C"):
            self.assertEqual(wh.get_product("c1", lot).exact_total_cost, Fraction(1, 5))


class TestRecomputeAndIncremental(unittest.TestCase):
    def _build(self):
        _, led, wh = fresh()
        led.register_root_lot("R1", 1000)
        led.register_batch("B1", "R1", 600, [("L1A", 400), ("L1B", 180)], 20)
        led.register_batch("B2", "L1A", 400, [("L2A", 300), ("L2B", 90)], 10)
        led.register_batch("B3", "L2A", 300, [("L3A", 250)], 50)
        wh.set_opening_cost("r1", "R1", 10000)
        for b, fee in (("B1", 500), ("B2", 300), ("B3", 200)):
            wh.set_processing_fee("f1", b, fee)
        for lot, v in (("L1A", 10), ("L1B", 10), ("L2A", 12), ("L2B", 8), ("L3A", 20)):
            wh.set_reference_value("v1", lot, v)
        return led, wh

    def test_price_correction_full_and_incremental_match(self):
        led, wh = self._build()
        wh.compute_cut("c1", "v1 全量", "r1", "f1", "v1", mode="full")
        # 价格更正落在多产出批 B2 的产出 L2A（12 → 18）
        for lot, v in (("L1A", 10), ("L1B", 10), ("L2A", 18), ("L2B", 8), ("L3A", 20)):
            wh.set_reference_value("v2", lot, v)
        wh.compute_cut("c2", "v2 全量", "r1", "f1", "v2", mode="full")
        wh.compute_cut("c2i", "v2 增量", "r1", "f1", "v2", mode="incremental")

        snap = led.build_snapshot()
        for lot_id in snap.lots:
            full_p = wh.get_product("c2", lot_id)
            inc_p = wh.get_product("c2i", lot_id)
            self.assertEqual(full_p.priceable, inc_p.priceable)
            self.assertEqual(full_p.exact_total_cost, inc_p.exact_total_cost)
            self.assertEqual(full_p.published_total_cost, inc_p.published_total_cost)
            self.assertEqual(full_p.exact_unit_cost, inc_p.exact_unit_cost)

        # L2A 是多产出批 B2 的产出：改其参考价值直接改变 L2A/L2B 单位成本，
        # 并沿 B3 传导给未直接改动的 L3A
        before = wh.get_product("c1", "L2A")
        after = wh.get_product("c2", "L2A")
        self.assertNotEqual(before.exact_unit_cost, after.exact_unit_cost)
        explanation = wh.explain_unit_change("c2", "L3A", old_cut_id="c1")
        self.assertEqual(explanation["first_changed_lot"], "L2A")
        self.assertIn("参考", explanation["reason"])

    def test_production_correction_recomputes_descendants(self):
        led, wh = self._build()
        wh.compute_cut("c1", "更正前", "r1", "f1", "v1")
        # B2 无下游对 L2B 的消耗约束外的部分：L2B 减产 90→80，L2A 保持 300
        led.correct_batch("B2", outputs=[("L2A", 300), ("L2B", 80)],
                          input_grams=390, loss_grams=10)
        wh.compute_cut("c2", "更正后全量", "r1", "f1", "v1", mode="full")
        wh.compute_cut("c2i", "更正后增量", "r1", "f1", "v1", mode="incremental")
        # L3A 产量未动，但其单位成本随 B2 池重新分配后经 B3 传递而变化
        p_old = wh.get_product("c1", "L3A")
        p_new = wh.get_product("c2", "L3A")
        self.assertNotEqual(p_old.exact_unit_cost, p_new.exact_unit_cost)
        explanation = wh.explain_unit_change("c2", "L3A", old_cut_id="c1")
        self.assertEqual(explanation["first_changed_lot"], "L2A")
        # 增量与全量一致
        for lot_id in led.build_snapshot().lots:
            self.assertEqual(
                wh.get_product("c2", lot_id).exact_total_cost,
                wh.get_product("c2i", lot_id).exact_total_cost,
            )

    def test_incremental_requires_prior_cut(self):
        _, wh = self._build()
        with self.assertRaises(VersionError):
            wh.compute_cut("cx", "无基线增量", "r1", "f1", "v1", mode="incremental")

    def test_missing_version_rejected(self):
        _, wh = self._build()
        with self.assertRaises(VersionError):
            wh.compute_cut("cx", "版本缺失", "nope", "f1", "v1")

    def test_missing_opening_cost_makes_root_unpriceable(self):
        _, led, wh = fresh()
        led.register_root_lot("R1", 100)
        led.register_batch("B1", "R1", 50, [("L1", 50)], 0)
        # 只建加工费/参考价值版本，缺期初成本版本 → 建切面直接报版本缺失；
        # 若存在版本但缺该根条目，则根及下游不可计价
        wh.set_opening_cost("r", "R1", 0)  # 显式零成本条目是可计价的
        wh.set_processing_fee("f", "B1", 0)
        wh.set_reference_value("v", "L1", 10)
        wh.compute_cut("c1", "零成本可计价", "r", "f", "v")
        p = wh.get_product("c1", "L1")
        self.assertTrue(p.priceable)
        self.assertEqual(p.exact_total_cost, Fraction(0))


if __name__ == "__main__":
    unittest.main()
