"""独立审计测试：模糊交叉核对、篡改必被发现。"""

from __future__ import annotations

import random
import sqlite3
import unittest

from orgbudget.audit import independent_allocate
from orgbudget.budget import WeightedChild, allocate_children
from orgbudget.errors import InvalidTree
from tests.support import ServiceTestCase


class AllocationFuzzTest(unittest.TestCase):
    def test_fuzz_matches_production(self) -> None:
        rng = random.Random(7)
        for _ in range(2_000):
            n = rng.randint(1, 8)
            kids = [WeightedChild(f"D{i:02d}", rng.randint(1, 20)) for i in range(n)]
            budget = rng.randint(0, 100_000)
            production = allocate_children(budget, kids)
            audit = independent_allocate(budget, [(c.department_id, c.weight) for c in kids])
            self.assertEqual(production, audit)
            self.assertEqual(sum(production.values()), budget)


class AuditTamperTest(ServiceTestCase):
    def _build(self) -> None:
        version = self.build_tree(
            [("HQ", None), ("A", "HQ"), ("B", "HQ"), ("A1", "A"), ("B1", "B")]
        )
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, 10_000, self.weights_for(product))
        self.budget.create_commitment("R1", "A1", 1_000)
        self.budget.register_spend("R1", 400, "S1")

    def test_tampered_allocation_detected(self) -> None:
        self._build()
        with sqlite3.connect(self.budget.db_path) as con:
            con.execute("UPDATE allocation SET budget = budget + 5 WHERE dept_id = 'A1'")
            con.commit()
        with self.assertRaises(InvalidTree):
            from orgbudget.audit import audit_scheme
            audit_scheme(self.budget.db_path, self.org.db_path)

    def test_tampered_spend_event_detected(self) -> None:
        self._build()
        # 直接篡改事件表伪造超支：承诺 1000，支出改成 1200。
        with sqlite3.connect(self.budget.db_path) as con:
            con.execute("UPDATE spend SET amount = 1200 WHERE request_no = 'R1'")
            con.commit()
        from orgbudget.audit import audit_scheme
        with self.assertRaises(InvalidTree):
            audit_scheme(self.budget.db_path, self.org.db_path)

    def test_clean_passes(self) -> None:
        self._build()
        from orgbudget.audit import audit_scheme
        result = audit_scheme(self.budget.db_path, self.org.db_path)
        self.assertEqual(result.total_spent, 400)
        self.assertEqual(result.total_open_remaining, 600)
        self.assertEqual(result.commitments, 1)
        self.assertEqual(result.spend_events, 1)


if __name__ == "__main__":
    unittest.main()
