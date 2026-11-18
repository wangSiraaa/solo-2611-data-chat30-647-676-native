"""全合成场景端到端：独立核验质量/成本/发布守恒，覆盖题目全部要求案例。"""

import unittest
from fractions import Fraction

from ledger_cost.checks import check_cut
from ledger_cost.errors import RejectedByConsumption
from ledger_cost.scenario import (
    B9_CORRECTION,
    FEES_V1,
    OPENING_COSTS_V1,
    REFS_V1,
    REFS_V2,
    build_cost_inputs_v1,
    build_cost_inputs_v2,
    build_full_system,
)


class TestFullScenario(unittest.TestCase):
    def setUp(self):
        self.storage, self.ledger, self.wh = build_full_system(":memory:")

    def test_cut_v1_all_invariants(self):
        self.wh.compute_cut("c1", "v1 全量", "root-v1", "fee-v1", "ref-v1")
        snap = self.ledger.build_snapshot()
        products = {p.lot_id: p for p in self.wh.list_products("c1")}
        opening = {k: Fraction(v) for k, v in OPENING_COSTS_V1.items()}
        fees = {k: Fraction(v) for k, v in FEES_V1.items()}
        report = check_cut(snap, products, opening, fees)
        self.assertTrue(report["ok"], report)

        # 部分投入留存：R1 留存 400g，R2 留存 300g，R3 留存 7g
        self.assertEqual(self.wh.retained_cost("c1", "R1")["retained_grams"], 400)
        self.assertEqual(self.wh.retained_cost("c1", "R2")["retained_grams"], 300)
        self.assertEqual(self.wh.retained_cost("c1", "R3")["retained_grams"], 7)

    def test_zero_price_branch_unpriceable_but_keeps_no_numbers(self):
        self.wh.compute_cut("c1", "v1", "root-v1", "fee-v1", "ref-v1")
        for lot_id in ("L5A", "L6A"):
            p = self.wh.get_product("c1", lot_id)
            self.assertFalse(p.priceable)
            self.assertIsNone(p.exact_total_cost)
            self.assertIsNone(p.published_total_cost)
        # 同切面其他分支仍正常计价
        self.assertTrue(self.wh.get_product("c1", "L1A").priceable)

    def test_one_cent_case(self):
        self.wh.compute_cut("c1", "v1", "root-v1", "fee-v1", "ref-v1")
        self.wh.compute_cut("c-cent", "一分钱切面", "root-cent", "fee-v1", "ref-v1")
        alloc = {
            lot: self.wh.get_product("c-cent", lot).published_total_cost
            for lot in ("L7A", "L7B", "L7C")
        }
        self.assertEqual(sum(alloc.values()), 1)
        self.assertEqual(alloc["L7A"], 1)
        self.assertEqual((alloc["L7B"], alloc["L7C"]), (0, 0))
        # 精确值三等分（池 0.6 分 / 3 = 0.2 分），发布值不回写精确值
        for lot in ("L7A", "L7B", "L7C"):
            self.assertEqual(
                self.wh.get_product("c-cent", lot).exact_total_cost, Fraction(1, 5)
            )
        # 主 v1 切面下 B7 池为 3 分，精确值各 1 分、发布各 1 分
        for lot in ("L7A", "L7B", "L7C"):
            p = self.wh.get_product("c1", lot)
            self.assertEqual(p.exact_total_cost, Fraction(1))
            self.assertEqual(p.published_total_cost, 1)

    def test_downstream_blocks_upstream_decrease(self):
        self.wh.compute_cut("c1", "v1", "root-v1", "fee-v1", "ref-v1")
        # L1B 已被 B8 消耗 100g，B1 试图把 L1B 从 180 减到 50
        with self.assertRaises(RejectedByConsumption) as ctx:
            self.ledger.correct_batch(
                "B1", input_grams=470,
                outputs=[("L1A", 400), ("L1B", 50)], loss_grams=20,
            )
        self.assertEqual(ctx.exception.blocking, [("L1B", 50, 100)])
        # 拒绝后无新修订、成本切面不受影响
        self.assertEqual(self.ledger.get_batch("B1").revision_no, 1)
        self.assertEqual(len(self.wh.list_cuts()), 1)

    def test_price_correction_full_equals_incremental(self):
        self.wh.compute_cut("c1", "v1", "root-v1", "fee-v1", "ref-v1")
        build_cost_inputs_v2(self.wh)
        self.wh.compute_cut("c2", "v2 全量", "root-v1", "fee-v1", "ref-v2")
        self.wh.compute_cut("c2i", "v2 增量", "root-v1", "fee-v1", "ref-v2",
                            mode="incremental")
        snap = self.ledger.build_snapshot()
        for lot_id in snap.lots:
            a = self.wh.get_product("c2", lot_id)
            b = self.wh.get_product("c2i", lot_id)
            self.assertEqual((a.priceable, a.exact_total_cost, a.published_total_cost),
                             (b.priceable, b.exact_total_cost, b.published_total_cost))
        # 两版切面独立守恒
        opening = {k: Fraction(v) for k, v in OPENING_COSTS_V1.items()}
        fees = {k: Fraction(v) for k, v in FEES_V1.items()}
        for cut in ("c1", "c2", "c2i"):
            products = {p.lot_id: p for p in self.wh.list_products(cut)}
            report = check_cut(snap, products, opening, fees)
            self.assertTrue(report["ok"], (cut, report))

    def test_production_correction_and_recompute_plus_explanation(self):
        self.wh.compute_cut("c1", "更正前", "root-v1", "fee-v1", "ref-v1")
        before = self.wh.get_product("c1", "L9B")
        self.ledger.correct_batch("B9", **B9_CORRECTION)
        self.wh.compute_cut("c3", "更正后全量", "root-v1", "fee-v1", "ref-v1")
        self.wh.compute_cut("c3i", "更正后增量", "root-v1", "fee-v1", "ref-v1",
                            mode="incremental")
        after = self.wh.get_product("c3", "L9B")
        # L9B 克数 30→35：单位成本虽因单产出批同比例分母而不变，
        # 但总成本随真实产量变化，且同批 L9A 单位成本变化
        self.assertNotEqual(before.exact_total_cost, after.exact_total_cost)
        explanation = self.wh.explain_unit_change("c3", "L9B", old_cut_id="c1")
        self.assertEqual(explanation["first_changed_lot"], "L9B")
        # 更正后批次明细仍与整数总额相等
        view = self.wh.get_batch_cost("c3", "B9")
        self.assertEqual(
            sum(p.published_total_cost for p in view.products), view.pool_published
        )
        for lot_id in self.ledger.build_snapshot().lots:
            self.assertEqual(
                self.wh.get_product("c3", lot_id).exact_total_cost,
                self.wh.get_product("c3i", lot_id).exact_total_cost,
            )
        opening = {k: Fraction(v) for k, v in OPENING_COSTS_V1.items()}
        fees = {k: Fraction(v) for k, v in FEES_V1.items()}
        snap = self.ledger.build_snapshot()
        products = {p.lot_id: p for p in self.wh.list_products("c3")}
        self.assertTrue(check_cut(snap, products, opening, fees)["ok"])

    def test_cancel_releases_mass_and_recompute_stays_conservative(self):
        self.wh.compute_cut("c1", "撤销前", "root-v1", "fee-v1", "ref-v1")
        self.assertEqual(self.ledger.available("R5"), 30)
        self.ledger.cancel_batch("B10")
        self.assertEqual(self.ledger.available("R5"), 50)
        self.wh.compute_cut("c4", "撤销后", "root-v1", "fee-v1", "ref-v1")
        snap = self.ledger.build_snapshot()
        self.assertNotIn("B10", snap.batches)
        self.assertNotIn("L10A", snap.lots)
        opening = {k: Fraction(v) for k, v in OPENING_COSTS_V1.items()}
        fees = {k: Fraction(v) for k, v in FEES_V1.items() if k != "B10"}
        products = {p.lot_id: p for p in self.wh.list_products("c4")}
        report = check_cut(snap, products, opening, fees)
        self.assertTrue(report["ok"], report)
        # 历史切面仍保留被撤销批的产品
        self.assertTrue(self.wh.get_product("c1", "L10A").priceable)

    def test_scale_constraints_of_scenario(self):
        snap = self.ledger.build_snapshot()
        self.assertLessEqual(len(snap.batches), 20)
        self.assertLessEqual(max(len(b.outputs) for b in snap.batches.values()), 5)
        self.assertLessEqual(snap.max_depth, 6)
        self.assertEqual(snap.max_depth, 4)  # B1→B2→B3→B11


if __name__ == "__main__":
    unittest.main()
