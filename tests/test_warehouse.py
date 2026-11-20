"""分配仓测试：优先级分配、部分到货、不可变发放、更正/取消、守恒、并发防超发。"""

import threading
import tempfile
import unittest
from pathlib import Path

from ordercommit.errors import (
    NotFound,
    ReceiptAlreadyProcessed,
    RuleViolation,
    StateConflict,
)
from ordercommit.warehouse import Warehouse

T = "2026-11-15T10:00:00"


def bootstrap() -> Warehouse:
    wh = Warehouse(":memory:")
    wh.register_commitment("C1", "S1", "V1",
                           [{"sku": "A", "qty": 10, "unit_price": 5},
                            {"sku": "B", "qty": 6, "unit_price": 7}], now=T)
    return wh


class AllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wh = bootstrap()

    def tearDown(self) -> None:
        self.wh.close()

    def _demands(self) -> None:
        # 同 SKU 三行：优先级/到期日/ID 三种定序
        self.wh.add_demand("d-low", "A", 4, 1, "2026-11-25", now=T)
        self.wh.add_demand("d-hi-late", "A", 4, 9, "2026-11-24", now=T)
        self.wh.add_demand("d-hi-earlier", "A", 4, 9, "2026-11-20", now=T)

    def test_priority_then_due_then_id_ordering(self) -> None:
        self._demands()
        # 只到 6 件 A
        self.wh.receive("R1", "C1", [{"sku": "A", "qty": 6}], now=T)
        hi_early = self.wh.get_demand("d-hi-earlier")   # 优先级9 + 最早到期 → 先满 4
        hi_late = self.wh.get_demand("d-hi-late")       # 再拿剩余 2（部分）
        low = self.wh.get_demand("d-low")               # 空手
        self.assertEqual((hi_early["allocated_qty"], hi_early["status"]), (4, "DONE"))
        self.assertEqual((hi_late["allocated_qty"], hi_late["status"]), (2, "PARTIAL"))
        self.assertEqual((low["allocated_qty"], low["status"]), (0, "OPEN"))

    def test_same_box_never_double_promised_and_leftover_central(self) -> None:
        self.wh.add_demand("d1", "A", 8, 5, "2026-11-20", now=T)
        res = self.wh.receive("R1", "C1", [{"sku": "A", "qty": 5}], now=T)
        # 5 件全发 d1，无中央余额
        self.assertEqual(len(res["allocations"]), 1)
        self.assertEqual(res["leftover"], {"A": 0})
        stock = self.wh.stock_levels()
        self.assertEqual(stock["A"], {"received": 5, "allocated": 5, "central": 0})
        # 新需求不能重算/撤销已完成发放
        self.wh.add_demand("d2", "A", 5, 9, "2026-11-19", now=T)
        self.assertEqual(self.wh.get_demand("d1")["allocated_qty"], 5)
        self.assertEqual(self.wh.get_demand("d2")["allocated_qty"], 0)

    def test_receipt_identity_processed_once(self) -> None:
        self.wh.receive("R1", "C1", [{"sku": "A", "qty": 1}], now=T)
        with self.assertRaises(ReceiptAlreadyProcessed):
            self.wh.receive("R1", "C1", [{"sku": "A", "qty": 1}], now=T)

    def test_cumulative_receipt_cannot_exceed_commitment(self) -> None:
        self.wh.receive("R1", "C1", [{"sku": "A", "qty": 8}], now=T)
        with self.assertRaises(RuleViolation):
            self.wh.receive("R2", "C1", [{"sku": "A", "qty": 3}], now=T)  # 8+3>10
        # 合法累计可以
        self.wh.receive("R3", "C1", [{"sku": "A", "qty": 2}], now=T)
        self.assertEqual(
            self.wh.get_commitment("C1")["lines"][0]["received_qty"], 10)

    def test_receipt_requires_known_commitment_and_sku(self) -> None:
        with self.assertRaises(NotFound):
            self.wh.receive("Rx", "NOPE", [{"sku": "A", "qty": 1}], now=T)
        with self.assertRaises(RuleViolation):
            self.wh.receive("Ry", "C1", [{"sku": "ZZ", "qty": 1}], now=T)

    def test_correction_keeps_issued_qty_and_reallocates_free_stock(self) -> None:
        self.wh.add_demand("d1", "A", 6, 5, "2026-11-20", now=T)
        self.wh.add_demand("d2", "A", 4, 9, "2026-11-19", now=T)
        self.wh.receive("R1", "C1", [{"sku": "A", "qty": 7}], now=T)
        # d2 优先 9 先满 4；d1 得 3（部分），中央余 0
        self.assertEqual(self.wh.get_demand("d1")["allocated_qty"], 3)
        # 下调 d1 到已发量 3：实物保留
        self.wh.correct_demand("d1", 3, now=T)
        self.assertEqual(self.wh.get_demand("d1")["status"], "DONE")
        # 不允许改到低于已发量
        with self.assertRaises(StateConflict):
            self.wh.correct_demand("d1", 2, now=T)
        # 新到货后，上调的新需求也能继续吃中央库存
        self.wh.correct_demand("d1", 5, now=T)
        self.wh.receive("R2", "C1", [{"sku": "A", "qty": 2}], now=T)
        self.assertEqual(self.wh.get_demand("d1")["allocated_qty"], 5)

    def test_cancel_never_returns_physical_to_supplier(self) -> None:
        self.wh.add_demand("d1", "A", 6, 5, "2026-11-20", now=T)
        self.wh.add_demand("d2", "A", 4, 1, "2026-11-21", now=T)
        self.wh.receive("R1", "C1", [{"sku": "A", "qty": 6}], now=T)
        # d1 满 6，d2 空手
        self.wh.cancel_demand("d1", now=T)
        # 已发 6 件不撤回；库存视图不变（实物在门店侧），d2 不会凭空拿到 d1 的货
        stock = self.wh.stock_levels()
        self.assertEqual(stock["A"]["allocated"], 6)
        self.assertEqual(stock["A"]["central"], 0)
        self.assertEqual(self.wh.get_demand("d2")["allocated_qty"], 0)
        # 取消一个空手需求，其未发额度（本就没有实物）无影响
        self.wh.cancel_demand("d2", now=T)
        self.assertTrue(self.wh.conservation_check()["ok"])

    def test_cancel_frees_central_stock_for_others(self) -> None:
        # 先到货后有需求：3 件入中央库存
        self.wh.receive("R1", "C1", [{"sku": "B", "qty": 3}], now=T)
        self.wh.add_demand("d1", "B", 3, 5, "2026-11-20", now=T)  # 吃掉 3
        self.wh.receive("R2", "C1", [{"sku": "B", "qty": 3}], now=T)  # 再到 3 入中央
        self.wh.add_demand("d2", "B", 3, 5, "2026-11-21", now=T)  # 吃掉新 3
        self.assertEqual(self.wh.get_demand("d2")["allocated_qty"], 3)
        self.wh.add_demand("d3", "B", 2, 8, "2026-11-22", now=T)  # 无库存，欠 2
        self.assertEqual(self.wh.get_demand("d3")["allocated_qty"], 0)
        # 取消 d2 不会把已发实物退回（守恒仍成立）
        self.wh.cancel_demand("d2", now=T)
        self.assertEqual(self.wh.get_demand("d3")["allocated_qty"], 0)
        self.assertTrue(self.wh.conservation_check()["ok"])

    def test_allocations_are_append_only_immutable(self) -> None:
        self.wh.add_demand("d1", "A", 4, 5, "2026-11-20", now=T)
        self.wh.receive("R1", "C1", [{"sku": "A", "qty": 4}], now=T)
        before = self.wh.allocation_history()
        self.assertEqual(len(before), 1)
        # 任何后续操作都不得删除/改写既有 allocation_id
        self.wh.add_demand("d2", "A", 2, 5, "2026-11-21", now=T)
        self.wh.cancel_demand("d2", now=T)
        after = self.wh.allocation_history()
        self.assertEqual(after[0]["allocation_id"], before[0]["allocation_id"])
        self.assertEqual(after[0]["qty"], before[0]["qty"])

    def test_conservation_identity(self) -> None:
        self.wh.add_demand("d1", "A", 7, 5, "2026-11-20", now=T)
        self.wh.receive("R1", "C1", [{"sku": "A", "qty": 4},
                                     {"sku": "B", "qty": 6}], now=T)
        self.wh.receive("R2", "C1", [{"sku": "A", "qty": 4}], now=T)
        check = self.wh.conservation_check()
        self.assertTrue(check["ok"], check["problems"])
        self.assertEqual(check["details"]["A"]["central"], 1)
        self.assertEqual(check["details"]["B"]["central"], 6)


class ConcurrencyTest(unittest.TestCase):
    def test_two_connections_race_same_stock_no_over_issue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "wh.db")
            wh0 = Warehouse(db)
            wh0.register_commitment("C1", "S1", "V1",
                                    [{"sku": "A", "qty": 4, "unit_price": 1}], now=T)
            wh0.add_demand("d1", "A", 4, 5, "2026-11-20", now=T)
            wh0.close()

            errors: list[Exception] = []

            def receiver(receipt_id: str) -> None:
                wh = Warehouse(db)
                try:
                    wh.receive(receipt_id, "C1", [{"sku": "A", "qty": 4}], now=T)
                except Exception as exc:  # noqa: BLE001 - 并发下记录所有失败
                    errors.append(exc)
                finally:
                    wh.close()

            t1 = threading.Thread(target=receiver, args=("R1",))
            t2 = threading.Thread(target=receiver, args=("R2",))
            t1.start(); t2.start(); t1.join(); t2.join()

            audit = Warehouse(db)
            stock = audit.stock_levels()
            # 只有一张到货能成功（承诺仅 4 件）；库存绝不超过承诺、绝不超发
            self.assertEqual(stock["A"]["received"], 4)
            self.assertLessEqual(stock["A"]["allocated"], 4)
            self.assertTrue(audit.conservation_check()["ok"])
            self.assertEqual(len(errors), 1)  # 另一张被规则/串行化拦截
            self.assertEqual(audit.get_demand("d1")["allocated_qty"], 4)
            audit.close()

    def test_two_connections_contend_central_stock_no_over_issue(self) -> None:
        """两连接各自触发同一 SKU 的分配，合计发放不得超过中央库存。"""
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "wh2.db")
            wh0 = Warehouse(db)
            wh0.register_commitment("C1", "S1", "V1",
                                    [{"sku": "A", "qty": 4, "unit_price": 1}], now=T)
            # 两个需求合计要 8，但中央库存只有 4
            wh0.add_demand("d1", "A", 4, 5, "2026-11-20", now=T)
            wh0.add_demand("d2", "A", 4, 5, "2026-11-21", now=T)
            wh0.close()

            barrier = threading.Barrier(2)

            def trigger(receipt_id: str) -> None:
                wh = Warehouse(db)
                barrier.wait()  # 尽量让两个连接同时进入写事务
                wh.receive(receipt_id, "C1", [{"sku": "A", "qty": 2}], now=T)
                wh.close()

            t1 = threading.Thread(target=trigger, args=("R1",))
            t2 = threading.Thread(target=trigger, args=("R2",))
            t1.start(); t2.start(); t1.join(); t2.join()

            audit = Warehouse(db)
            stock = audit.stock_levels()["A"]
            self.assertEqual(stock["received"], 4)
            self.assertEqual(stock["allocated"], 4)      # 绝不超发
            self.assertEqual(stock["central"], 0)
            got = {d["demand_id"]: d["allocated_qty"]
                   for d in audit.list_demands() if d["sku"] == "A"}
            self.assertEqual(sum(got.values()), 4)        # 同一箱不承诺给两个门店
            self.assertTrue(audit.conservation_check()["ok"])
            audit.close()


if __name__ == "__main__":
    unittest.main()
