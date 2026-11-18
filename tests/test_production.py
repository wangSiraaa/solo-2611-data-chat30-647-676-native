"""生产台账规则测试：登记守恒、部分投入、DAG 谱系、修订/撤销拦截。"""

import unittest

from ledger_cost.errors import (
    AlreadyExists,
    ConstraintViolation,
    NotFound,
    RejectedByConsumption,
)
from ledger_cost.production import MAX_BATCHES, MAX_DEPTH, MAX_OUTPUTS, ProductionLedger
from ledger_cost.storage import Storage


def fresh_ledger() -> ProductionLedger:
    return ProductionLedger(Storage(":memory:"))


class TestRegistration(unittest.TestCase):
    def test_root_and_basic_batch_mass_balance(self):
        led = fresh_ledger()
        led.register_root_lot("R1", 100)
        led.register_batch("B1", "R1", 60, [("L1", 40), ("L2", 15)], 5)
        snap = led.build_snapshot()
        self.assertEqual(snap.lots["R1"].produced_grams, 100)
        self.assertEqual(snap.lots["R1"].consumed_grams, 60)
        self.assertEqual(led.available("R1"), 40)
        self.assertEqual(snap.batches["B1"].depth, 1)

    def test_zero_output_rejected(self):
        led = fresh_ledger()
        led.register_root_lot("R1", 10)
        with self.assertRaises(ConstraintViolation):
            led.register_batch("B1", "R1", 5, [("L1", 5), ("L2", 0)], 0)

    def test_mass_imbalance_rejected(self):
        led = fresh_ledger()
        led.register_root_lot("R1", 100)
        with self.assertRaises(ConstraintViolation):
            led.register_batch("B1", "R1", 50, [("L1", 49)], 0)  # 49 != 50
        with self.assertRaises(ConstraintViolation):
            led.register_batch("B1", "R1", 50, [("L1", 40)], 5)  # 40+5 != 50

    def test_partial_input_and_over_consumption(self):
        led = fresh_ledger()
        led.register_root_lot("R1", 100)
        led.register_batch("B1", "R1", 30, [("L1", 30)], 0)
        self.assertEqual(led.available("R1"), 70)
        # 同一批号至多一次投入来源
        with self.assertRaises(ConstraintViolation):
            led.register_batch("B2", "R1", 10, [("L2", 10)], 0)
        # 超出可用量（B1 更正时）
        with self.assertRaises(ConstraintViolation):
            led.correct_batch("B1", input_grams=101, outputs=[("L1", 101)], loss_grams=0)

    def test_unknown_input_lot(self):
        led = fresh_ledger()
        with self.assertRaises(NotFound):
            led.register_batch("B1", "RX", 10, [("L1", 10)], 0)

    def test_duplicate_output_lot_id(self):
        led = fresh_ledger()
        led.register_root_lot("R1", 100)
        led.register_batch("B1", "R1", 20, [("L1", 20)], 0)
        with self.assertRaises(AlreadyExists):
            led.register_batch("B2", "L1", 10, [("L1", 10)], 0)

    def test_max_outputs_limit(self):
        led = fresh_ledger()
        led.register_root_lot("R1", 100)
        outs = [(f"L{i}", 1) for i in range(MAX_OUTPUTS + 1)]
        with self.assertRaises(ConstraintViolation):
            led.register_batch("B1", "R1", MAX_OUTPUTS + 1, outs, 0)


class TestDAG(unittest.TestCase):
    def _chain(self, led: ProductionLedger, n: int):
        led.register_root_lot("R1", 10_000)
        prev, prev_g = "R1", 10_000
        for i in range(1, n + 1):
            out = f"L{i}"
            led.register_batch(f"B{i}", prev, prev_g, [(out, prev_g)], 0)
            prev, prev_g = out, prev_g

    def test_three_level_reprocessing(self):
        led = fresh_ledger()
        self._chain(led, 3)
        snap = led.build_snapshot()
        self.assertEqual([snap.batches[f"B{i}"].depth for i in (1, 2, 3)], [1, 2, 3])

    def test_depth_limit(self):
        led = fresh_ledger()
        with self.assertRaises(ConstraintViolation):
            self._chain(led, MAX_DEPTH + 1)

    def test_cannot_consume_own_output(self):
        led = fresh_ledger()
        led.register_root_lot("R1", 100)
        led.register_batch("B1", "R1", 50, [("L1", 50)], 0)
        with self.assertRaises(ConstraintViolation):
            led.correct_batch("B1", input_lot_id="L1", input_grams=10,
                              outputs=[("L2", 10)], loss_grams=0)

    def test_batch_count_limit(self):
        led = fresh_ledger()
        # 浅层独立批：每批各自消费独立根批号，避免深度限制先触发
        for i in range(1, MAX_BATCHES + 1):
            led.register_root_lot(f"R{i}", 10)
            led.register_batch(f"B{i}", f"R{i}", 1, [(f"L{i}", 1)], 0)
        led.register_root_lot("RX", 10)
        with self.assertRaises(ConstraintViolation):
            led.register_batch("BX", "RX", 1, [("LX", 1)], 0)


class TestRevisionAndCancel(unittest.TestCase):
    def _setup(self):
        led = fresh_ledger()
        led.register_root_lot("R1", 1000)
        led.register_batch("B1", "R1", 600, [("L1A", 400), ("L1B", 180)], 20)
        led.register_batch("B2", "L1B", 100, [("L2", 90), ("L3", 8)], 2)
        return led

    def test_downstream_consumption_blocks_decrease(self):
        led = self._setup()
        # L1B 已被 B2 消耗 100g，B1 想把 L1B 减到 50g → 整次拒绝
        with self.assertRaises(RejectedByConsumption) as ctx:
            led.correct_batch("B1", outputs=[("L1A", 400), ("L1B", 50)],
                              input_grams=470, loss_grams=20)
        self.assertEqual(ctx.exception.blocking[0][0], "L1B")
        self.assertEqual(ctx.exception.blocking[0][1:], (50, 100))
        # 台账保持原样
        self.assertEqual(led.get_batch("B1").revision_no, 1)
        self.assertEqual(led.available("L1B"), 80)

    def test_correction_without_consumption_succeeds(self):
        led = self._setup()
        # L1A 无下游：减产并把投入减量、损耗改 0（400→300，投入 600→500，损耗 20→20?）
        rev_no = led.correct_batch("B1", input_grams=500,
                                   outputs=[("L1A", 300), ("L1B", 180)], loss_grams=20)
        self.assertEqual(rev_no, 2)
        self.assertEqual(led.get_batch("B1").revision_no, 2)
        self.assertEqual(led.available("R1"), 500)
        revs = led.list_revisions("B1")
        self.assertEqual([r["status"] for r in revs], ["superseded", "active"])
        self.assertEqual(revs[0]["outputs"][0]["grams"], 400)  # 旧值持久保留

    def test_correction_can_add_new_lot_and_drop_unused_lot(self):
        led = fresh_ledger()
        led.register_root_lot("R1", 100)
        led.register_batch("B1", "R1", 50, [("L1", 30), ("L2", 15)], 5)
        # L1、L2 均无下游：移除 L2、新增 L3，质量仍守恒
        led.correct_batch("B1", input_grams=50,
                          outputs=[("L1", 30), ("L3", 15)], loss_grams=5)
        snap = led.build_snapshot()
        self.assertNotIn("L2", snap.lots)
        self.assertEqual(snap.lots["L3"].produced_grams, 15)
        # 被移除批号的历史仍可在修订台账中查到
        revs = led.list_revisions("B1")
        self.assertEqual(revs[0]["outputs"][1]["lot_id"], "L2")

    def test_cancel_blocked_by_consumption(self):
        led = self._setup()
        with self.assertRaises(RejectedByConsumption):
            led.cancel_batch("B1")  # L1B 被 B2 消耗

    def test_cancel_releases_input(self):
        led = fresh_ledger()
        led.register_root_lot("R9", 50)
        led.register_batch("B10", "R9", 20, [("X1", 18)], 2)
        self.assertEqual(led.available("R9"), 30)
        led.cancel_batch("B10")
        self.assertEqual(led.available("R9"), 50)
        with self.assertRaises(NotFound):
            led.get_batch("B10")
        # 撤销后不能再更正
        with self.assertRaises(ConstraintViolation):
            led.correct_batch("B10", outputs=[("X1", 18)])

    def test_trace_lot_shows_whereabouts(self):
        led = self._setup()
        trace = led.trace_lot("L1B")
        self.assertEqual(trace["produced_grams"], 180)
        self.assertEqual(trace["consumed_grams"], 100)
        self.assertEqual(trace["available_grams"], 80)
        self.assertEqual(trace["consumers"][0]["batch_id"], "B2")
        root_trace = led.trace_lot("R1")
        self.assertEqual(root_trace["origin"]["kind"], "root")
        self.assertEqual(root_trace["consumers"][0]["batch_id"], "B1")


class TestLedgerWorksWithoutCosts(unittest.TestCase):
    def test_full_ledger_flow_without_any_cost_data(self):
        led = fresh_ledger()
        led.register_root_lot("R1", 100)
        led.register_batch("B1", "R1", 80, [("L1", 70)], 10)
        led.register_batch("B2", "L1", 70, [("L2", 65)], 5)
        self.assertEqual(led.available("L1"), 0)
        snap = led.build_snapshot()
        self.assertEqual(snap.max_depth, 2)
        self.assertEqual(led.trace_lot("L2")["origin"]["origin_batch"], "B2")


if __name__ == "__main__":
    unittest.main()
