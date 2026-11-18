"""跨进程并发与重启持久化：验证 SQLite 文件锁与落盘事实。"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from tests.support import ServiceTestCase


class ProcessConcurrencyTest(ServiceTestCase):
    def test_two_processes_race_only_one_wins(self) -> None:
        version = self.build_tree([("HQ", None), ("L", "HQ")])
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, 1_000, self.weights_for(product))

        worker = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {os.getcwd()!r})
            from orgbudget import BudgetService, OrgService
            from orgbudget.errors import InsufficientFunds
            org = OrgService({str(self.org.db_path)!r})
            svc = BudgetService({str(self.budget.db_path)!r}, org, busy_timeout_ms=30000)
            try:
                svc.create_commitment(sys.argv[1], "L", 800)
                print("ok")
            except InsufficientFunds:
                print("reject")
            """
        )
        env = dict(os.environ, PYTHONPATH=os.getcwd())
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", worker, req],
                stdout=subprocess.PIPE,
                env=env,
                text=True,
            )
            for req in ("PROC-RA", "PROC-RB")
        ]
        outcomes = sorted(p.communicate()[0].strip() for p in procs)
        self.assertEqual([p.returncode for p in procs], [0, 0])
        self.assertEqual(outcomes, ["ok", "reject"])
        self.assertEqual(self.budget.summary("L").open_committed, 800)

    def test_state_survives_service_restart(self) -> None:
        version = self.build_tree([("HQ", None), ("L", "HQ")])
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, 1_000, self.weights_for(product))
        self.budget.create_commitment("R1", "L", 600)
        self.budget.register_spend("R1", 250, "S1")

        # 用全新实例重开两个库，事实必须完整可读。
        from orgbudget import BudgetService, OrgService

        org2 = OrgService(self.org.db_path)
        budget2 = BudgetService(self.budget.db_path, org2)
        self.assertEqual(org2.get_product(version).root, "HQ")
        c = budget2.get_commitment("R1")
        self.assertEqual((c.amount, c.spent, c.remaining), (600, 250, 350))
        s = budget2.summary("L")
        self.assertEqual((s.budget, s.spent, s.open_committed, s.available),
                         (1_000, 250, 350, 400))


if __name__ == "__main__":
    unittest.main()
