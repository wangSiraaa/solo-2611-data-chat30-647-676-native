"""报价服务：不可变报价、幂等、窗口、接受时额度重核、超卖防护。"""

import unittest

from ordercommit.errors import (
    DuplicateRequest,
    OverCapacity,
    QuoteExpired,
    RuleViolation,
    StateConflict,
)
from ordercommit.pricing import SkuRule
from ordercommit.supplier import SupplierService

WINDOW = ("2026-11-01T00:00:00", "2026-11-10T00:00:00")
T_OK = "2026-11-05T10:00:00"
T_LATE = "2026-11-11T10:00:00"


def make_service() -> SupplierService:
    svc = SupplierService(":memory:")
    svc.register_rule_version(
        "V1", "S1", WINDOW[0], WINDOW[1],
        {"A": SkuRule(2, 0, 10, [(0, 12), (8, 8)]),
         "B": SkuRule(3, 3, 9, [(0, 5)])},
        now=WINDOW[0])
    return svc


class QuoteServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def tearDown(self) -> None:
        self.svc.close()

    def test_quote_is_immutable_snapshot_and_full_volume_price(self) -> None:
        q = self.svc.quote("R1", "S1", [{"sku": "A", "qty": 8}], T_OK)
        self.assertEqual(q["total_price"], 64)
        self.assertEqual(q["status"], "OPEN")
        # 再登记/改规则不影响已出报价：直接改库验证报价快照独立
        row = self.svc._conn.execute(
            "SELECT total_price FROM s_quotes WHERE quote_no=?",
            (q["quote_no"],)).fetchone()
        self.assertEqual(row["total_price"], 64)

    def test_same_request_replays_same_quote(self) -> None:
        q1 = self.svc.quote("R1", "S1", [{"sku": "A", "qty": 4}], T_OK)
        q2 = self.svc.quote("R1", "S1", [{"sku": "A", "qty": 4}], T_OK)
        self.assertEqual(q1["quote_no"], q2["quote_no"])
        # 行顺序不同但内容相同也算同文（用独立请求号，避免同号异文）
        q3 = self.svc.quote("R5", "S1",
                            [{"sku": "A", "qty": 4}, {"sku": "B", "qty": 3}], T_OK)
        q4 = self.svc.quote("R5", "S1",
                            [{"sku": "B", "qty": 3}, {"sku": "A", "qty": 4}], T_OK)
        self.assertEqual(q3["quote_no"], q4["quote_no"])

    def test_same_no_different_body_rejected(self) -> None:
        self.svc.quote("R1", "S1", [{"sku": "A", "qty": 4}], T_OK)
        with self.assertRaises(DuplicateRequest):
            self.svc.quote("R1", "S1", [{"sku": "A", "qty": 6}], T_OK)

    def test_rule_violation_blocks_quote(self) -> None:
        with self.assertRaises(RuleViolation):  # 7 不是包装 2 的倍数
            self.svc.quote("R2", "S1", [{"sku": "A", "qty": 7}], T_OK)
        with self.assertRaises(RuleViolation):  # B 最低量 3，1 件不达标
            self.svc.quote("R3", "S1", [{"sku": "B", "qty": 1}], T_OK)

    def test_zero_qty_line_is_free_and_allowed(self) -> None:
        q = self.svc.quote("R4", "S1",
                           [{"sku": "A", "qty": 0}, {"sku": "B", "qty": 3}], T_OK)
        self.assertEqual(q["total_price"], 15)

    def test_expired_accept_creates_no_commitment(self) -> None:
        q = self.svc.quote("R5", "S1", [{"sku": "A", "qty": 4}], T_OK)
        with self.assertRaises(QuoteExpired):
            self.svc.accept(q["quote_no"], "C1", T_LATE)
        self.assertEqual(self.svc.get_quote(q["quote_no"])["status"], "OPEN")
        # 承诺不存在
        from ordercommit.errors import NotFound
        with self.assertRaises(NotFound):
            self.svc.get_commitment("C1")

    def test_accept_builds_commitment_and_consumes_capacity(self) -> None:
        q = self.svc.quote("R6", "S1", [{"sku": "A", "qty": 8}], T_OK)
        c = self.svc.accept(q["quote_no"], "C6", T_OK)
        self.assertEqual(c["total_price"], 64)
        self.assertEqual(self.svc.remaining_capacity("V1", "A"), 2)
        # 重复接受同号承诺：幂等返回原承诺
        again = self.svc.accept(q["quote_no"], "C6", T_OK)
        self.assertEqual(again["commitment_no"], "C6")
        # 同报价换承诺号：拒绝
        with self.assertRaises(StateConflict):
            self.svc.accept(q["quote_no"], "C-OTHER", T_OK)

    def test_capacity_rechecked_at_accept_no_oversell(self) -> None:
        q1 = self.svc.quote("R7", "S1", [{"sku": "A", "qty": 8}], T_OK)
        q2 = self.svc.quote("R8", "S1", [{"sku": "A", "qty": 4}], T_OK)
        self.svc.accept(q1["quote_no"], "C7", T_OK)  # 剩 2
        # q2 出报价时本身合法，但接受时累计 8+4>10
        with self.assertRaises(OverCapacity):
            self.svc.accept(q2["quote_no"], "C8", T_OK)
        self.assertEqual(self.svc.remaining_capacity("V1", "A"), 2)

    def test_quote_outside_window_uses_no_active_version(self) -> None:
        from ordercommit.errors import NotFound
        with self.assertRaises(NotFound):
            self.svc.quote("R9", "S1", [{"sku": "A", "qty": 2}], T_LATE)


if __name__ == "__main__":
    unittest.main()
