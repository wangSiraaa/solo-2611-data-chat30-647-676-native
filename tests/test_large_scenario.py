"""大规模场景：20 部门、深度 5、100 笔支出，独立重算与事件级核对。"""

from __future__ import annotations

import random
import unittest

from tests.support import ServiceTestCase
from orgbudget.audit import audit_scheme
from orgbudget.errors import InsufficientFunds


# 恰好 20 个部门、根到最深叶深度 5 的树（HQ 为第 0 层，最深叶在第 5 层）。
TREE_20: list[tuple[str, str | None]] = [
    ("HQ", None),
    ("D1", "HQ"), ("D2", "HQ"), ("D3", "HQ"), ("D4", "HQ"),
    ("D11", "D1"), ("D12", "D1"),
    ("D21", "D2"), ("D22", "D2"),
    ("D31", "D3"), ("D32", "D3"),
    ("D41", "D4"), ("D42", "D4"),
    ("D111", "D11"),
    ("D211", "D21"),
    ("D311", "D31"),
    ("D411", "D41"),
    ("D1111", "D111"),
    ("D2111", "D211"),
    ("D11111", "D1111"),
]

LEAVES_20 = [
    "D12", "D11111",
    "D22", "D2111",
    "D32", "D311",
    "D42", "D411",
]


class LargeScenarioTest(ServiceTestCase):
    def test_20_departments_depth5_100_spends_and_migration(self) -> None:
        rng = random.Random(20261118)
        version = self.build_tree(TREE_20)
        product = self.org.get_product(version)
        self.assertEqual(len(product.department_ids), 20)
        self.assertEqual(product.max_depth(), 5)
        self.assertEqual(sorted(d for d in product.department_ids if product.is_leaf(d)),
                         sorted(LEAVES_20))

        root_budget = 100_000_000  # 一百万元，单位分
        weights = {}
        for dept in product.department_ids:
            kids = product.children(dept)
            for kid in kids:
                weights[(dept, kid)] = rng.randint(1, 9)
        self.budget.register_root_budget(version, root_budget, weights)

        # 生成恰好 100 笔实际支出事件（含先开承诺、部分取消）。
        leaf_budgets = {leaf: self.budget.budget_of(leaf) for leaf in LEAVES_20}
        open_req_by_leaf: dict[str, list[str]] = {leaf: [] for leaf in LEAVES_20}
        seq = 0
        spend_count = 0
        while spend_count < 100:
            leaf = rng.choice(LEAVES_20)
            if not open_req_by_leaf[leaf] or rng.random() < 0.7:
                summary = self.budget.summary(leaf)
                if summary.available <= 0:
                    continue
                seq += 1
                amount = min(rng.randint(100, 50_000), summary.available)
                req = f"REQ-{seq:04d}"
                self.budget.create_commitment(req, leaf, amount)
                open_req_by_leaf[leaf].append(req)
            req = rng.choice(open_req_by_leaf[leaf])
            commitment = self.budget.get_commitment(req)
            if commitment.remaining <= 0:
                continue
            amount = min(rng.randint(50, 5_000), commitment.remaining)
            spend_count += 1
            self.budget.register_spend(req, amount, f"SP-{spend_count:04d}")
            # 约 1/8 概率在支出后取消，释放剩余占额（支出部分保留）。
            if rng.random() < 0.125 and commitment.remaining - amount > 0:
                self.budget.cancel_commitment(req)
                open_req_by_leaf[leaf].remove(req)

        self.assertEqual(len(self.budget.spend_events()), 100)
        audit_v1 = audit_scheme(self.budget.db_path, self.org.db_path)
        self.assertEqual(audit_v1.departments, 20)
        self.assertEqual(audit_v1.leaves, 8)
        self.assertEqual(audit_v1.spend_events, 100)
        self.assertEqual(audit_v1.root_budget, root_budget)

        # 迁移：把 D3 整棵子树移到 D4 下，最深链（D1 链）仍保持深度 5。
        self.org.move_subtree("D3", "D4")
        v2 = self.org.publish("move D3 under D4").version
        p2 = self.org.get_product(v2)
        self.assertEqual(p2.ancestors("D311"), ("HQ", "D4", "D3", "D31"))
        self.assertEqual(p2.max_depth(), 5)

        new_weights = {}
        for dept in p2.department_ids:
            for kid in p2.children(dept):
                new_weights[(dept, kid)] = rng.randint(1, 9)
        scheme2 = self.budget.revise_budget(v2, root_budget, new_weights)

        # 新树汇总沿新闭包：D4 现在要吞下 D3 子树的全部占额。
        d4 = self.budget.summary("D4")
        d3_facts = self.budget.subtree_summary("D3")
        d3_spent = next(n.spent for n in d3_facts if n.dept_id == "D3")
        self.assertGreaterEqual(d4.spent, d3_spent)

        audit_v2 = audit_scheme(
            self.budget.db_path, self.org.db_path, scheme2.scheme_id
        )
        self.assertEqual(audit_v2.org_version, v2)
        self.assertEqual(audit_v2.spend_events, 100)
        self.assertEqual(audit_v2.total_spent, audit_v1.total_spent)

        # 旧方案审计仍按旧树通过，历史不被新树解释。
        audit_old = audit_scheme(self.budget.db_path, self.org.db_path, 1)
        self.assertEqual(audit_old.org_version, version)
        self.assertEqual(audit_old.spend_events, 100)

        # 换版后承诺仍可继续支出，且不超过原承诺剩余。
        any_open = self.budget.open_commitments()[0]
        before = self.budget.get_commitment(any_open.request_no)
        self.budget.register_spend(
            any_open.request_no, 1, f"SP-AFTER-{any_open.request_no}"
        )
        self.assertEqual(self.budget.get_commitment(any_open.request_no).spent,
                         before.spent + 1)
        with self.assertRaises(InsufficientFunds):
            # 故意超剩余：先把剩余打满再追一笔。
            c = self.budget.get_commitment(any_open.request_no)
            if c.remaining:
                self.budget.register_spend(
                    c.request_no, c.remaining, f"SP-FILL-{c.request_no}"
                )
            self.budget.register_spend(c.request_no, 1, "SP-OVER")


if __name__ == "__main__":
    unittest.main()
