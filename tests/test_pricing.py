"""全量阶梯价与采购规则的单元测试。"""

import unittest

from ordercommit.errors import RuleViolation
from ordercommit.pricing import SkuRule


class PricingTest(unittest.TestCase):
    def setUp(self) -> None:
        # 0-9 件单价 12；>=10 件单价 8；包装 2；最低量 0；上限 20
        self.rule = SkuRule(pack_multiple=2, min_qty=0, capacity=20,
                            tiers=[(0, 12), (10, 8)])

    def test_zero_purchase_is_free(self) -> None:
        self.assertEqual(self.rule.line_price(0), 0)
        self.assertEqual(self.rule.applicable_unit_price(0), 12)

    def test_full_volume_tier_pricing(self) -> None:
        # 全量阶梯：8 件全部按 12；10 件全部按 8（多买反而便宜）
        self.assertEqual(self.rule.line_price(8), 96)
        self.assertEqual(self.rule.line_price(10), 80)
        self.assertLess(self.rule.line_price(10), self.rule.line_price(8))

    def test_pack_multiple_enforced(self) -> None:
        with self.assertRaises(RuleViolation):
            self.rule.check_purchase(7)
        self.rule.check_purchase(8)

    def test_min_qty_enforced(self) -> None:
        rule = SkuRule(4, 8, 20, [(0, 5)])
        with self.assertRaises(RuleViolation):
            rule.check_purchase(4)
        rule.check_purchase(8)

    def test_capacity_enforced(self) -> None:
        with self.assertRaises(RuleViolation):
            self.rule.check_purchase(22)

    def test_feasible_grid_is_sparse(self) -> None:
        self.assertEqual(self.rule.feasible_quantities(),
                         [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20])
        self.assertEqual(self.rule.feasible_quantities(limit=7), [0, 2, 4, 6])

    def test_registration_validation(self) -> None:
        bad = [
            SkuRule(3, 7, 20, [(0, 5)]),          # 最低量非包装倍数
            SkuRule(2, 10, 8, [(0, 5)]),          # 最低量超上限
            SkuRule(2, 0, 9, [(0, 5)]),           # 上限非包装倍数
            SkuRule(2, 0, 10, [(2, 5)]),          # 缺 0 档
            SkuRule(2, 0, 10, [(0, 5), (4, -1)]), # 负单价
        ]
        for i, rule in enumerate(bad):
            with self.subTest(i=i):
                with self.assertRaises(RuleViolation):
                    rule.validate_registration(f"X{i}")

    def test_negative_price_rejected_but_zero_allowed(self) -> None:
        ok = SkuRule(1, 0, 5, [(0, 0)])
        ok.validate_registration("Z")
        self.assertEqual(ok.line_price(5), 0)


if __name__ == "__main__":
    unittest.main()
