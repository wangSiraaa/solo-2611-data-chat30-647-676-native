"""预算执行仓测试：迁移换版、拒绝、部分取消、历史版本、并发与幂等。"""

from __future__ import annotations

import threading
import unittest

from tests.support import ServiceTestCase
from orgbudget.audit import audit_scheme
from orgbudget.budget_service import BudgetService
from orgbudget.errors import (
    CommitmentClosedError,
    IdempotencyConflict,
    InactiveLeaf,
    InsufficientFunds,
    NotALeaf,
    SchemeError,
    WeightError,
)


class BudgetMigrationTest(ServiceTestCase):
    """带未结承诺的部门迁移：新树重算、身份不改、旧版沿旧树查询。"""

    def _build_v1(self) -> int:
        # HQ -> A -> (A1, A2)；HQ -> B -> B1
        return self.build_tree(
            [
                ("HQ", None),
                ("A", "HQ"),
                ("B", "HQ"),
                ("A1", "A"),
                ("A2", "A"),
                ("B1", "B"),
            ]
        )

    def test_migration_with_open_commitment(self) -> None:
        v1 = self._build_v1()
        p1 = self.org.get_product(v1)
        weights = self.weights_for(p1)
        self.budget.register_root_budget(v1, 10_000, weights)

        # 在 A1 开立未结承诺并支出一部分。
        self.budget.create_commitment("REQ-1", "A1", 2_000)
        self.budget.register_spend("REQ-1", 700, "SP-1")
        # A2 也来一笔，便于看迁移后汇总。
        self.budget.create_commitment("REQ-2", "A2", 500)

        before_a1 = self.budget.summary("A1")
        self.assertEqual((before_a1.budget, before_a1.spent, before_a1.open_committed),
                         (2_500, 700, 1_300))
        self.assertEqual(before_a1.available, 500)
        # 旧树上 A 子树汇总 = A1 + A2。
        old_a = self.budget.subtree_summary("A")
        self.assertEqual({n.dept_id: n.spent for n in old_a},
                         {"A": 700, "A1": 700, "A2": 0})

        # 迁移：A 整棵子树从 HQ 下移到 B 下，发布 v2。
        self.org.move_subtree("A", "B")
        v2 = self.org.publish("move A under B").version
        p2 = self.org.get_product(v2)
        self.assertEqual(p2.ancestors("A1"), ("HQ", "B", "A"))

        # 预算人员为新直接孩子关系补齐权重（HQ 只剩 B；B 新增孩子 A）。
        new_weights = self.weights_for(p2, {("B", "A"): 3, ("B", "B1"): 1})
        scheme2 = self.budget.revise_budget(v2, 10_000, new_weights)

        # 承诺身份没有被改挂：REQ-1 仍属于 A1。
        c1 = self.budget.get_commitment("REQ-1")
        self.assertEqual((c1.dept_id, c1.amount, c1.spent, c1.remaining),
                         ("A1", 2_000, 700, 1_300))

        # 迁移后所有祖先汇总沿新树：A1 的祖先现在是 HQ,B,A。
        for ancestor in ("HQ", "B", "A"):
            summary = self.budget.summary(ancestor)
            self.assertGreaterEqual(summary.spent, 700)
        b_summary = self.budget.summary("B")
        self.assertEqual(b_summary.spent, 700)
        self.assertEqual(b_summary.open_committed, 1_300 + 500)

        # 旧版查询仍沿旧树：旧方案里 A 直接挂 HQ，且数字冻结。
        old_scheme = self.budget.get_scheme(1)
        old_a1 = self.budget.summary("A1", scheme_id=1)
        self.assertEqual(old_a1.budget, 2_500)
        self.assertEqual(old_scheme.status, "SUPERSEDED")
        # 旧产品里 A 的父是 HQ；新产品里是 B —— 不能按当前树解释历史。
        self.assertEqual(self.org.get_product(v1).parent_of["A"], "HQ")
        self.assertEqual(self.org.get_product(v2).parent_of["A"], "B")

        audit = audit_scheme(self.budget.db_path, self.org.db_path, scheme2.scheme_id)
        self.assertEqual(audit.spend_events, 1)
        self.assertEqual(audit.total_spent, 700)

    def test_revision_rejected_when_new_budget_insufficient(self) -> None:
        v1 = self._build_v1()
        p1 = self.org.get_product(v1)
        self.budget.register_root_budget(v1, 10_000, self.weights_for(p1))
        # A1 叶预算 2500，占用 2400 未结。
        self.budget.create_commitment("REQ-1", "A1", 2_400)

        # 迁移 A 到 B 下发布 v2，但新预算把根砍到 100 分，A1 必然覆盖不了。
        self.org.move_subtree("A", "B")
        v2 = self.org.publish("move").version
        p2 = self.org.get_product(v2)
        new_weights = self.weights_for(p2)
        with self.assertRaises(InsufficientFunds):
            self.budget.revise_budget(v2, 100, new_weights)

        # 拒绝留痕；旧方案继续生效且明确绑定旧组织版本。
        rejected = self.budget.rejected_revisions()
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0][0], v2)
        active = self.budget.active_scheme()
        self.assertEqual(active.org_version, v1)
        # 组织服务的新产品已经发布，不受预算拒绝影响。
        self.assertEqual(self.org.get_product(v2).version, v2)
        # 旧额度照常可用，且不会出现“新旧两份额度”。
        self.assertEqual(self.budget.summary("A1").budget, 2_500)
        with self.assertRaises(InsufficientFunds):
            self.budget.create_commitment("REQ-9", "A1", 200)

    def test_revision_requires_complete_weights(self) -> None:
        v1 = self._build_v1()
        p1 = self.org.get_product(v1)
        self.budget.register_root_budget(v1, 10_000, self.weights_for(p1))
        self.org.move_subtree("A", "B")
        v2 = self.org.publish("move").version
        p2 = self.org.get_product(v2)
        broken = self.weights_for(p2)
        del broken[("B", "A")]
        with self.assertRaises(WeightError):
            self.budget.revise_budget(v2, 10_000, broken)
        self.assertEqual(self.budget.active_scheme().org_version, v1)

    def test_same_org_version_revision_rejected(self) -> None:
        v1 = self._build_v1()
        p1 = self.org.get_product(v1)
        self.budget.register_root_budget(v1, 10_000, self.weights_for(p1))
        with self.assertRaises(SchemeError):
            self.budget.revise_budget(v1, 9_999, self.weights_for(p1))


class CommitmentLifecycleTest(ServiceTestCase):
    def _setup(self) -> int:
        version = self.build_tree(
            [("HQ", None), ("L1", "HQ"), ("L2", "HQ"), ("X", "L1")]
        )
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, 1_000, self.weights_for(product))
        return version

    def test_available_and_leaf_only(self) -> None:
        version = self._setup()
        product = self.org.get_product(version)
        # HQ/L1 非叶，不能开承诺。
        with self.assertRaises(NotALeaf):
            self.budget.create_commitment("R0", "L1", 10)
        # 叶 L2 预算 = 1000 按 HQ 两孩子各 500；L1 再把 500 全给 X。
        self.assertEqual(self.budget.budget_of("L2"), 500)
        self.assertEqual(self.budget.budget_of("X"), 500)
        self.budget.create_commitment("R1", "L2", 300)
        s = self.budget.summary("L2")
        self.assertEqual((s.budget, s.spent, s.open_committed, s.available),
                         (500, 0, 300, 200))
        with self.assertRaises(InsufficientFunds):
            self.budget.create_commitment("R2", "L2", 201)
        self.budget.create_commitment("R2", "L2", 200)
        self.assertEqual(self.budget.summary("L2").available, 0)

    def test_spend_cannot_exceed_commitment_remaining(self) -> None:
        self._setup()
        self.budget.create_commitment("R1", "L2", 100)
        self.budget.register_spend("R1", 60, "S1")
        with self.assertRaises(InsufficientFunds):
            self.budget.register_spend("R1", 41, "S2")
        self.budget.register_spend("R1", 40, "S2")
        c = self.budget.get_commitment("R1")
        self.assertEqual((c.spent, c.remaining), (100, 0))

    def test_partial_cancel_keeps_spent_part(self) -> None:
        self._setup()
        self.budget.create_commitment("R1", "L2", 400)
        self.budget.register_spend("R1", 150, "S1")
        cancelled = self.budget.cancel_commitment("R1")
        self.assertEqual(cancelled.status, "CANCELLED")
        # 已实际支出 150 分不能撤销，仅剩余 250 分被释放。
        self.assertEqual((cancelled.spent, cancelled.remaining), (150, 250))
        with self.assertRaises(CommitmentClosedError):
            self.budget.register_spend("R1", 1, "S2")
        s = self.budget.summary("L2")
        self.assertEqual((s.budget, s.spent, s.open_committed, s.available),
                         (500, 150, 0, 350))
        # 重复取消是幂等空操作，事实不变。
        again = self.budget.cancel_commitment("R1")
        self.assertEqual(again.status, "CANCELLED")
        self.assertEqual(len(self.budget.spend_events("L2")), 1)

    def test_idempotent_retry_does_not_double_reserve(self) -> None:
        self._setup()
        self.budget.create_commitment("R1", "L2", 400)
        # 模拟丢响应后同请求号重试：返回同一承诺，不新增占额。
        retry = self.budget.create_commitment("R1", "L2", 400)
        self.assertEqual(retry.request_no, "R1")
        self.assertEqual(len(self.budget.open_commitments("L2")), 1)
        # 同请求号但内容不同必须拒绝。
        with self.assertRaises(IdempotencyConflict):
            self.budget.create_commitment("R1", "L2", 401)
        # 支出幂等键重试。
        self.budget.register_spend("R1", 100, "S1")
        self.budget.register_spend("R1", 100, "S1")
        self.assertEqual(self.budget.get_commitment("R1").spent, 100)
        with self.assertRaises(IdempotencyConflict):
            self.budget.register_spend("R1", 99, "S1")

    def test_inactive_leaf_cannot_commit(self) -> None:
        version = self._setup()
        self.org.deactivate_leaf("L2")
        v2 = self.org.publish("deactivate L2").version
        weights = self.weights_for(self.org.get_product(v2))
        self.budget.revise_budget(v2, 1_000, weights)
        with self.assertRaises(InactiveLeaf):
            self.budget.create_commitment("R1", "L2", 10)


class ConcurrencyTest(ServiceTestCase):
    def test_two_connections_race_only_one_wins(self) -> None:
        version = self.build_tree([("HQ", None), ("L", "HQ")])
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, 1_000, self.weights_for(product))
        # 叶预算 1000，两笔各 800 的并发承诺最多一方成功。
        barrier = threading.Barrier(2)
        results: list[object] = []

        def worker(request_no: str) -> None:
            svc = BudgetService(self.budget.db_path, self.org, busy_timeout_ms=30_000)
            barrier.wait()
            try:
                svc.create_commitment(request_no, "L", 800)
                results.append(("ok", request_no))
            except InsufficientFunds:
                results.append(("reject", request_no))

        t1 = threading.Thread(target=worker, args=("RA",))
        t2 = threading.Thread(target=worker, args=("RB",))
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(r[0] for r in results), ["ok", "reject"])
        self.assertEqual(self.budget.summary("L").open_committed, 800)
        self.assertEqual(self.budget.summary("L").available, 200)

    def test_concurrent_same_request_retries_one_charge(self) -> None:
        version = self.build_tree([("HQ", None), ("L", "HQ")])
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, 1_000, self.weights_for(product))
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def worker() -> None:
            svc = BudgetService(self.budget.db_path, self.org, busy_timeout_ms=30_000)
            barrier.wait()
            try:
                c = svc.create_commitment("DUP", "L", 600)
                outcomes.append(c.request_no)
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(); t2.join()
        # 双方拿到同一条承诺，占额只算一次。
        self.assertEqual(outcomes, ["DUP", "DUP"])
        self.assertEqual(len(self.budget.open_commitments("L")), 1)
        self.assertEqual(self.budget.summary("L").open_committed, 600)

    def test_concurrent_spends_respect_remaining(self) -> None:
        version = self.build_tree([("HQ", None), ("L", "HQ")])
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, 1_000, self.weights_for(product))
        self.budget.create_commitment("R1", "L", 1_000)
        barrier = threading.Barrier(3)
        outcomes: list[object] = []

        def worker(idem: str) -> None:
            svc = BudgetService(self.budget.db_path, self.org, busy_timeout_ms=30_000)
            barrier.wait()
            try:
                svc.register_spend("R1", 400, idem)
                outcomes.append("ok")
            except InsufficientFunds:
                outcomes.append("reject")

        threads = [
            threading.Thread(target=worker, args=(f"S{i}",)) for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 承诺 1000，每笔 400：恰好两笔成功。
        self.assertEqual(sorted(outcomes), ["ok", "ok", "reject"])
        self.assertEqual(self.budget.get_commitment("R1").spent, 800)


if __name__ == "__main__":
    unittest.main()
