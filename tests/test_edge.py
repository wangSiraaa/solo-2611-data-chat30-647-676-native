"""边界与持久化补充测试（不依赖 pytest，直接 python3 tests/test_edge.py）。"""
from __future__ import annotations

import os
import tempfile
import threading

from airline.reconcile import reconcile
from airline.revenue import (
    DIAG_EVIDENCE_ON_INACTIVE,
    RevenueWarehouse,
)
from airline.ticketing import (
    InvalidOperation,
    LimitExceeded,
    RequestConflict,
    TicketingService,
    replay_journal,
)

PASS_N = 0


def check(cond: bool, message: str) -> None:
    global PASS_N
    if not cond:
        raise AssertionError(message)
    PASS_N += 1
    print(f"  [PASS] {message}")


def test_limits() -> None:
    print("[edge] 上限：30 票 / 每票 6 联 / 100 操作")
    svc = TicketingService(":memory:")
    try:
        svc.issue_ticket("R-BIG", [("S", 1)] * 7, 100)
        raise AssertionError("7 联应被拒绝")
    except LimitExceeded:
        check(True, "每张票超过 6 联被拒绝")
    for i in range(30):
        svc.issue_ticket(f"R-T{i}", [("S", 1)], 10)
    try:
        svc.issue_ticket("R-T31", [("S", 1)], 10)
        raise AssertionError("第 31 张票应被拒绝")
    except LimitExceeded:
        check(True, "第 31 张票被拒绝")

    svc2 = TicketingService(":memory:", max_operations=3)
    svc2.issue_ticket("A", [("S", 1)], 10)
    t = svc2.issue_ticket("B", [("S", 1)], 10)
    svc2.use_coupon("C", t["coupons"][0]["coupon_id"])
    try:
        svc2.issue_ticket("D", [("S", 1)], 10)
        raise AssertionError("第 4 次操作应被拒绝")
    except LimitExceeded:
        check(True, "操作数上限 100（此处注入为 3）生效")
    # 幂等重试不占用新操作配额。
    svc2.use_coupon("C", t["coupons"][0]["coupon_id"])
    check(True, "命中幂等缓存不增加操作计数")


def test_multi_coupon_reissue_and_guards() -> None:
    print("[edge] 多联同时重签、跨票拼联拒绝、已终结联拒绝重签")
    svc = TicketingService(":memory:")
    t = svc.issue_ticket("R0", [("S1", 1), ("S2", 2), ("S3", 3)], 600)
    ids = [c["coupon_id"] for c in t["coupons"]]
    r = svc.reissue("R1", t["ticket_id"], [ids[0], ids[2]],
                    [("N1", 1), ("N2", 1)], surcharge=40)
    old_sum = t["coupons"][0]["original_value"] + t["coupons"][2]["original_value"]
    check(r["new_fare"] == old_sum + 40, "多联重签票价=所选旧联原票值之和+补收")
    check(all(svc.get_coupon(c)["status"] == "EXCHANGED" for c in [ids[0], ids[2]]),
          "所选多联原子全部 EXCHANGED")
    check(svc.get_coupon(ids[1])["status"] == "OPEN", "未选联保持 OPEN 与原身份")
    check(all(c["parent_coupon_ids"] == [ids[0], ids[2]] for c in r["new_coupons"]),
          "每个新联都挂完整父联集合（祖先链）")
    try:
        svc.reissue("R2", t["ticket_id"], [ids[0]], [("X", 1)])
        raise AssertionError("EXCHANGED 联再重签应拒绝")
    except InvalidOperation:
        check(True, "EXCHANGED 联不能再次参加重签")
    t2 = svc.issue_ticket("R3", [("Z", 1)], 50)
    try:
        svc.reissue("R4", t["ticket_id"], [ids[1], t2["coupons"][0]["coupon_id"]],
                    [("X", 1)])
        raise AssertionError("跨票拼联应拒绝")
    except InvalidOperation:
        check(True, "不能跨票选择票联重签")
    # 手续费超额与负手续费。
    try:
        svc.refund_coupon("R5", ids[1], fee=10_000)
        raise AssertionError("超额手续费应拒绝")
    except InvalidOperation:
        check(True, "手续费超过票值被拒绝")
    try:
        svc.refund_coupon("R6", ids[1], fee=-1)
        raise AssertionError("负手续费应拒绝")
    except InvalidOperation:
        check(True, "负手续费被拒绝")
    reconcile(svc.snapshot())
    check(True, "多联重签后独立对账平衡")


def test_file_persistence_and_duplicate_replay() -> None:
    print("[edge] 文件库跨服务实例重开 + 重复日志重放幂等")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "persist.db")
    svc = TicketingService(path)
    t = svc.issue_ticket("P1", [("A", 1), ("B", 1)], 201)
    svc.use_coupon("P2", t["coupons"][0]["coupon_id"])
    del svc

    reopened = TicketingService(path)
    check(reopened.get_coupon(t["coupons"][0]["coupon_id"])["status"] == "USED",
          "重开 SQLite 文件后物化状态保留")
    # 同请求号在重开后仍然幂等。
    again = reopened.use_coupon("P2", t["coupons"][0]["coupon_id"])
    check(again["status"] == "USED", "请求日志持久化，重开后同号同文仍返回首次结果")
    try:
        reopened.use_coupon("P2", t["coupons"][1]["coupon_id"])
        raise AssertionError("同号异文应拒绝")
    except RequestConflict:
        check(True, "重开后同号异文仍然拒绝")

    # 日志里故意制造重复条目，重放不得产生重复终结。
    journal = reopened.export_journal()
    replay_journal(journal + journal, os.path.join(d, "dup.db"))
    check(True, "含重复条目的日志重放靠请求号幂等安全收敛")


def test_repeated_race() -> None:
    print("[edge] 使用/退票并发竞争（独立连接），最多一个成功")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "race2.db")
    svc = TicketingService(path)
    t = svc.issue_ticket("K0", [("S", 1)] * 6, 600)
    target = t["coupons"][0]["coupon_id"]
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(kind: str, rid: str) -> None:
        local = TicketingService(path)
        barrier.wait()
        try:
            if kind == "use":
                local.use_coupon(rid, target)
            else:
                local.refund_coupon(rid, target, fee=3)
            results.append(f"{kind}:ok")
        except InvalidOperation:
            results.append(f"{kind}:lost")

    t1 = threading.Thread(target=worker, args=("use", "K1"))
    t2 = threading.Thread(target=worker, args=("refund", "K2"))
    t1.start(); t2.start(); t1.join(); t2.join()
    check(len([r for r in results if r.endswith("ok")]) == 1,
          f"竞争终态唯一: {results}")
    check(svc.get_coupon(target)["status"] in {"USED", "REFUNDED"}, "终态确定且可重读")


def test_warehouse_against_file_db() -> None:
    print("[edge] 售票端离线：先无收入仓完成全部柜台操作，之后再建仓导入")
    d = tempfile.mkdtemp()
    svc = TicketingService(os.path.join(d, "counter.db"))
    t = svc.issue_ticket("W0", [("A", 1), ("B", 1)], 100)
    cid = t["coupons"][0]["coupon_id"]
    svc.use_coupon("W1", cid)
    snap = svc.snapshot()  # 柜台全程不接触收入仓

    wh = RevenueWarehouse(os.path.join(d, "warehouse.db"))
    wh.import_snapshot(snap)
    b0 = wh.build_batch()
    check(b0["recognized_total"] == 0, "无证据时不确认任何运输收入（出票成功≠已运输）")
    wh.import_usage_event("E1", cid, t["coupons"][0]["segment_id"])
    b1 = wh.build_batch()
    check(b1["recognized_total"] == 50 and b0["batch_id"] != b1["batch_id"],
          "晚到证据只影响新批次，旧批次保持零收入")
    check(wh.explain_batch(b0["batch_id"])["recognized_total"] == 0,
          "旧批次可重读且未被改写")


def test_evidence_cannot_grant_eligibility() -> None:
    print("[edge] 证据不能赋予资格：OPEN 联收到使用事件只留诊断、不确认收入")
    svc = TicketingService(":memory:")
    t = svc.issue_ticket("Q0", [("A", 1), ("B", 1)], 100)
    open_cid = t["coupons"][0]["coupon_id"]
    used_cid = t["coupons"][1]["coupon_id"]
    svc.use_coupon("Q1", used_cid)
    wh = RevenueWarehouse(":memory:")
    wh.import_snapshot(svc.snapshot())
    wh.import_usage_event("QE1", open_cid, "A")          # OPEN 联却来了使用证据
    wh.import_usage_event("QE2", used_cid, "B")          # 正常一致证据
    b = wh.build_batch()
    items = {i["coupon_id"]: i for i in b["items"]}
    check(items[open_cid]["recognized_amount"] == 0
          and items[open_cid]["decision"] == "PENDING_OPEN",
          "OPEN 联即使有证据也不确认运输收入")
    check(any(d["diag_type"] == DIAG_EVIDENCE_ON_INACTIVE
              and d["ref_id"] == open_cid for d in b["diagnostics"]),
          "资格不符证据保留 EVIDENCE_ON_INACTIVE 诊断")
    check(items[used_cid]["recognized_amount"] == 50,
          "USED 联的一致证据照常确认收入")


if __name__ == "__main__":
    test_limits()
    test_multi_coupon_reissue_and_guards()
    test_file_persistence_and_duplicate_replay()
    test_repeated_race()
    test_warehouse_against_file_db()
    test_evidence_cannot_grant_eligibility()
    print(f"\n补充测试全部通过，共 {PASS_N} 项断言。")
