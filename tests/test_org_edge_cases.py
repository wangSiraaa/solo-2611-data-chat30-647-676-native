"""组织服务边界：空树发布、同父移动幂等、停用幂等、停用叶历史保留。"""

from __future__ import annotations

from tests.support import ServiceTestCase
from orgbudget.audit import audit_scheme
from orgbudget.errors import InvalidTree


class OrgEdgeCaseTest(ServiceTestCase):
    def test_publish_empty_tree_rejected(self) -> None:
        with self.assertRaises(InvalidTree):
            self.org.publish("empty")

    def test_move_to_same_parent_is_noop(self) -> None:
        self.build_tree([("HQ", None), ("A", "HQ"), ("B", "HQ"), ("A1", "A")])
        self.org.move_subtree("A1", "A")  # 同父移动：无操作、无异常
        product = self.org.publish("noop")
        self.assertEqual(product.parent_of["A1"], "A")
        # 树未变化，发布只是产生下一个递增版本（v1 来自 build_tree）。
        self.assertEqual([v[0] for v in self.org.versions()], [1, 2])

    def test_deactivate_is_idempotent(self) -> None:
        version = self.build_tree([("HQ", None), ("L", "HQ")])
        self.org.deactivate_leaf("L")
        self.org.deactivate_leaf("L")  # 重复停用不报错
        product = self.org.publish("twice")
        self.assertFalse(product.is_active("L"))
        # 旧版本仍为启用。
        self.assertTrue(self.org.get_product(version).is_active("L"))

    def test_deactivated_leaf_keeps_commitment_history_through_revision(self) -> None:
        version = self.build_tree(
            [("HQ", None), ("A", "HQ"), ("B", "HQ"), ("A1", "A"), ("B1", "B")]
        )
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, 10_000, self.weights_for(product))
        self.budget.create_commitment("R1", "A1", 1_000)
        self.budget.register_spend("R1", 600, "S1")
        # 停用 A1：禁止新承诺，但既有承诺与支出继续被换版方案承认。
        self.org.deactivate_leaf("A1")
        v2 = self.org.publish("deactivate A1").version
        p2 = self.org.get_product(v2)
        scheme2 = self.budget.revise_budget(v2, 10_000, self.weights_for(p2))
        c = self.budget.get_commitment("R1")
        self.assertEqual((c.dept_id, c.spent, c.remaining, c.is_open),
                         ("A1", 600, 400, True))
        result = audit_scheme(self.budget.db_path, self.org.db_path, scheme2.scheme_id)
        self.assertEqual(result.total_spent, 600)
        self.assertEqual(result.total_open_remaining, 400)


if __name__ == "__main__":
    unittest.main()
