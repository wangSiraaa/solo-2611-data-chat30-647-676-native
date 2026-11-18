"""交付入口：出票/使用/退票/重签/导入证据/收入解释的端到端场景。

覆盖题目要求的案例：
1. 一分余数（最大余数法、coupon_id 定序）；
2. 部分重签（其余旧联身份与状态不变）；
3. 连续两次重签（祖先链追溯运输基数、补收不进运输收入）；
4. 退票与使用竞争（两个连接同时终结同一联，最多一个成功）；
5. 同请求丢响应重试（同号同文返回首次结果，同号异文拒绝）；
6. 晚到使用证据（旧批次不改写、新批次转收入）与缺证据/航段不符/重复冲突诊断；
7. 事件重放重建与逐表比对；
8. 独立核票值去向（首次付款/后续补退/最终联状态三口径守恒）。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from .reconcile import reconcile
from .revenue import (
    RevenueWarehouse,
    DIAG_DUPLICATE_CONFLICT,
    DIAG_MISSING_EVIDENCE,
    DIAG_SEGMENT_MISMATCH,
    DIAG_UNMATCHED_EVIDENCE,
    DECISION_RECOGNIZED,
    DECISION_PENDING_OPEN,
    DECISION_TERMINAL_NOT_USED,
)
from .ticketing import (
    InvalidOperation,
    RequestConflict,
    TicketingService,
    replay_journal,
)

PASS = "PASS"


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)
    print(f"    [{PASS}] {message}")


def _ids(coupons: list[dict[str, Any]]) -> list[str]:
    return [c["coupon_id"] for c in coupons]


# --------------------------------------------------------------------------- 1. 一分余数


def scenario_one_cent_remainder() -> dict[str, Any]:
    print("\n[1] 一分余数案例：100 分 / 3 个等权联，余 1 分按余数降序、coupon_id 定序")
    svc = TicketingService(":memory:")
    r = svc.issue_ticket("REQ-1C", [("SEG-A", 1), ("SEG-B", 1), ("SEG-C", 1)], 100)
    values = [c["original_value"] for c in r["coupons"]]
    print("   分摊结果:", values)
    check(values == [34, 33, 33], "精确比例 33.33... 下取整后余 1 分，定序给最早 coupon_id")
    check(sum(values) == 100, "分摊之和严格等于总票价，无丢分")

    # 权重 1/1/1/1 与总分 1：仅一联拿到 1 分，其余 0 分（非负，允许 0 值联）。
    r2 = svc.issue_ticket("REQ-1C2", [("S1", 1), ("S2", 1), ("S3", 1), ("S4", 1)], 1)
    v2 = [c["original_value"] for c in r2["coupons"]]
    check(v2 == [1, 0, 0, 0], f"总分 1 分四联：{v2}，首联按定序取得")
    return {"service": svc, "ticket": r}


# --------------------------------------------------------------------------- 2. 部分重签


def scenario_partial_reissue(svc: TicketingService, ticket: dict[str, Any]) -> dict[str, Any]:
    print("\n[2] 部分重签：只交换 4 联票中的第 2 联，其余联保持原身份与状态")
    cids = _ids(ticket["coupons"])
    untouched_before = svc.get_coupon(cids[0])
    r = svc.reissue(
        "REQ-EX1", ticket["ticket_id"], [cids[1]],
        [("SEG-B1", 2), ("SEG-B2", 3)], surcharge=50,
    )
    print("   旧联:", cids[1], "-> EXCHANGED；新票:", r["new_ticket_id"],
          "新联:", _ids(r["new_coupons"]))
    check(svc.get_coupon(cids[1])["status"] == "EXCHANGED", "被选旧联原子标为 EXCHANGED")
    untouched_after = svc.get_coupon(cids[0])
    check(
        untouched_before["coupon_id"] == untouched_after["coupon_id"]
        and untouched_after["status"] == "OPEN"
        and untouched_before["original_value"] == untouched_after["original_value"],
        "其余旧联身份、状态、原票值均不变（不整票作废重建）",
    )
    new_values = [c["original_value"] for c in r["new_coupons"]]
    check(r["new_fare"] == 100 + 50 and sum(new_values) == r["new_fare"],
          f"新票价 = 旧联原票值 100 + 补收 50 = {r['new_fare']}，新联分摊守恒")
    check(r["new_coupons"][0]["parent_coupon_ids"] == [cids[1]],
          "新联保留指向旧联的祖先链")
    return {"exchange1": r}


# --------------------------------------------------------------------------- 3. 连续两次重签


def scenario_two_consecutive_reissues(
    svc: TicketingService, exchange1: dict[str, Any]
) -> dict[str, Any]:
    print("\n[3] 连续两次重签：祖先链递归追溯，补收不计运输收入")
    new1_ids = _ids(exchange1["new_coupons"])
    # 第二次重签：选第一次重签产生的一个 OPEN 联，再补收 20 分。
    r2 = svc.reissue(
        "REQ-EX2", exchange1["new_ticket_id"], [new1_ids[0]],
        [("SEG-B1-X", 1), ("SEG-B1-Y", 1)], surcharge=20,
    )
    check(svc.get_coupon(new1_ids[0])["status"] == "EXCHANGED",
          "第一次重签的新联可再次参加重签并终结为 EXCHANGED")
    check(
        svc.get_coupon(new1_ids[1])["status"] == "OPEN",
        "同票未被选中的联继续保持 OPEN",
    )
    print("   第二次重签新票价:", r2["new_fare"],
          "（旧联原票值", exchange1["new_coupons"][0]["original_value"], "+ 补收 20）")
    check(r2["new_fare"] == exchange1["new_coupons"][0]["original_value"] + 20,
          "第二次重签票价等式成立且非负")

    # 规则护栏：补收与退还同时为正必须拒绝；负新票价必须拒绝。
    try:
        svc.reissue("REQ-BAD1", exchange1["new_ticket_id"], [new1_ids[1]],
                    [("X", 1)], surcharge=10, refund_amount=10)
        raise AssertionError("补收退还同时为正应被拒绝")
    except InvalidOperation:
        check(True, "补收和退还同时为正被拒绝")
    try:
        svc.reissue("REQ-BAD2", exchange1["new_ticket_id"],
                    [new1_ids[1]], [("X", 1)], refund_amount=10_000)
        raise AssertionError("负新票价应被拒绝")
    except InvalidOperation:
        check(True, "退还超过旧联票值导致负新票价被拒绝")
    return {"exchange2": r2}


# --------------------------------------------------------------------------- 4. 退票与使用竞争


def scenario_refund_use_race(workdir: str) -> dict[str, Any]:
    print("\n[4] 退票与使用竞争：两个连接同时终结同一 OPEN 联，最多一个成功")
    db_path = os.path.join(workdir, "race.db")
    svc = TicketingService(db_path)
    outcomes: list[dict[str, Any]] = []
    barrier = threading.Barrier(2)

    def worker(kind: str, request_id: str) -> None:
        local = TicketingService(db_path)  # 独立连接
        barrier.wait()
        try:
            if kind == "use":
                local.use_coupon(request_id, "CPN-000001")
            else:
                local.refund_coupon(request_id, "CPN-000001", fee=5)
            outcomes.append({"kind": kind, "ok": True})
        except InvalidOperation as exc:
            outcomes.append({"kind": kind, "ok": False, "error": str(exc)})

    svc.issue_ticket("REQ-RACE-ISSUE", [("R1", 1), ("R2", 1)], 500)
    t1 = threading.Thread(target=worker, args=("use", "REQ-RACE-USE"))
    t2 = threading.Thread(target=worker, args=("refund", "REQ-RACE-REF"))
    t1.start(); t2.start(); t1.join(); t2.join()
    print("   竞争结果:", outcomes)
    successes = [o for o in outcomes if o["ok"]]
    check(len(successes) == 1, f"恰好一个连接成功，实际成功数 {len(successes)}")
    final = svc.get_coupon("CPN-000001")
    check(final["status"] in {"USED", "REFUNDED"}, f"终态确定: {final['status']}")
    check(svc.operation_count() == 2,
          "日志含出票与唯一成功终结；业务失败者回滚，不留操作记录")

    # USED 联不能退；手续费不得超过票值。
    if final["status"] == "USED":
        try:
            svc.refund_coupon("REQ-RACE-R2", "CPN-000001", fee=0)
            raise AssertionError("USED 联退票应被拒绝")
        except InvalidOperation:
            check(True, "USED 联不能退票")
    return {"service": svc, "outcomes": outcomes}


# --------------------------------------------------------------------------- 5. 同请求丢响应重试


def scenario_idempotent_retry(svc: TicketingService, ticket: dict[str, Any]) -> None:
    print("\n[5] 同请求丢响应重试：同号同文返回首次结果，同号异文拒绝")
    cid = _ids(ticket["coupons"])[2]
    first = svc.use_coupon("REQ-USE-RETRY", cid)
    before = svc.operation_count()
    retry = svc.use_coupon("REQ-USE-RETRY", cid)
    after = svc.operation_count()
    check(first == retry, "重试返回与首次完全一致的响应")
    check(before == after, "幂等重试不产生新操作、不重复终结")
    check(svc.get_coupon(cid)["status"] == "USED", "票联只被终结一次")

    try:
        svc.use_coupon("REQ-USE-RETRY", _ids(ticket["coupons"])[3])
        raise AssertionError("同号异文应被拒绝")
    except RequestConflict:
        check(True, "同请求号不同请求体被拒绝（RequestConflict）")

    # 退票手续费与退款确定值。
    refund_cid = _ids(ticket["coupons"])[3]
    rr = svc.refund_coupon("REQ-REF1", refund_cid, fee=7)
    check(rr["refund_amount"] == rr["original_value"] - 7,
          f"退款 = 原票值 {rr['original_value']} - 手续费 7 = {rr['refund_amount']}")
    try:
        svc.refund_coupon("REQ-REF2", refund_cid, fee=1)
        raise AssertionError("REFUNDED 联不能再次退票")
    except InvalidOperation:
        check(True, "REFUNDED 联不能再次终结")


# --------------------------------------------------------------------------- 6. 收入仓：证据、诊断、晚到、冻结批次


def scenario_revenue_warehouse(
    svc: TicketingService, exchange2: dict[str, Any]
) -> RevenueWarehouse:
    print("\n[6] 收入事实仓：双侧切面解释、四类诊断、晚到证据不改旧批次")
    wh = RevenueWarehouse(":memory:")
    snap1 = svc.snapshot()
    imp = wh.import_snapshot(snap1)
    again = wh.import_snapshot(snap1)
    check(again["deduplicated"] and again["snapshot_id"] == imp["snapshot_id"],
          "相同切面内容哈希去重，不产生新版本")

    # 各诊断类型使用独立票联，避免同一联多证据互相干扰。
    recognized_cid = "CPN-000003"   # 场景 5 中已 USED 的联
    mismatch_cid = "CPN-000006"     # 第一次重签后仍 OPEN 的存活联
    dup_cid = exchange2["new_coupons"][1]["coupon_id"]
    late_cid = exchange2["new_coupons"][0]["coupon_id"]
    recognized_seg = next(
        c["segment_id"] for c in snap1["coupons"] if c["coupon_id"] == recognized_cid
    )
    svc.use_coupon("REQ-USE-MISMATCH", mismatch_cid)
    svc.use_coupon("REQ-USE-DUP", dup_cid)
    svc.use_coupon("REQ-USE-LATE", late_cid)
    mismatch_seg = svc.get_coupon(mismatch_cid)["segment_id"]
    dup_seg = svc.get_coupon(dup_cid)["segment_id"]
    late_seg = svc.get_coupon(late_cid)["segment_id"]

    # 证据侧：一致证据 / 错误航段证据 / 切面外证据 / 同联重复一致证据。
    wh.import_usage_event("EV-OK", recognized_cid, recognized_seg)
    wh.import_usage_event("EV-MISMATCH", mismatch_cid, "SEG-WRONG")
    wh.import_usage_event("EV-OUTSIDE", "CPN-NOPE", "SEG-X")
    check(wh.import_usage_event("EV-OK", recognized_cid, recognized_seg)["status"]
          == "IDEMPOTENT", "同一事件 ID 重放为幂等返回，不产生第二条证据")

    snap2 = svc.snapshot()
    wh.import_snapshot(snap2)
    wh.import_usage_event("EV-DUP1", dup_cid, dup_seg)
    wh.import_usage_event("EV-DUP2", dup_cid, dup_seg)  # 第二条一致证据 -> 重复冲突
    # late_cid 此刻故意不给证据 -> 缺证据，待晚到事件补救。

    batch1 = wh.build_batch()
    items = {it["coupon_id"]: it for it in batch1["items"]}
    check(items[recognized_cid]["decision"] == DECISION_RECOGNIZED,
          "切面 USED + 唯一一致证据 -> 确认运输收入（同号重放不产生第二条证据）")
    check(items[mismatch_cid]["decision"] == DIAG_SEGMENT_MISMATCH,
          "航段不符 -> SEGMENT_MISMATCH，不确认收入")
    check(items[dup_cid]["decision"] == DIAG_DUPLICATE_CONFLICT,
          "两条一致证据 -> DUPLICATE_CONFLICT，不确认收入")
    check(items[late_cid]["decision"] == DIAG_MISSING_EVIDENCE,
          "已 USED 但缺证据 -> MISSING_EVIDENCE，不能把出票成功当已运输")
    check(any(d["diag_type"] == DIAG_UNMATCHED_EVIDENCE
              and d["ref_id"] == "EV-OUTSIDE" for d in batch1["diagnostics"]),
          "切面外证据保留 UNMATCHED_EVIDENCE 诊断")

    # 运输基数：新联继承值沿真实祖先链追溯，补收被排除在运输口径之外。
    root_fare_total = sum(
        t["fare"] for t in snap2["tickets"] if t["ticket_id"] == t["root_ticket_id"]
    )
    money = batch1["money"]
    check(
        money["transport_basis_total"]
        == root_fare_total - money["refund_amounts"] - money["refund_fees"]
        - money["exchange_refunds"],
        f"存活联运输基数存量 {money['transport_basis_total']} == 首次付款 {root_fare_total}"
        f" - 退票退款 {money['refund_amounts']} - 手续费 {money['refund_fees']}"
        "（补收 70 分不进运输基数）",
    )
    check(money["transport_basis_gross"] > root_fare_total,
          "毛额含 EXCHANGED/REFUNDED 联已离开存量的基数，与存量口径分别列示")
    check(batch1["money"]["surcharges"] == 70, "补收 70 分独立列示，不当运输收入")

    # 晚到证据：旧批次冻结，新批次把待核联转为收入。
    old_recognized = batch1["recognized_total"]
    wh.import_usage_event("EV-LATE", late_cid, late_seg)
    batch2 = wh.build_batch()
    old_reread = wh.explain_batch(batch1["batch_id"])
    check(old_reread["recognized_total"] == old_recognized
          and old_reread["items"] == batch1["items"],
          "晚到证据到达后重读旧批次，内容一字不改")
    new_items = {it["coupon_id"]: it for it in batch2["items"]}
    check(new_items[late_cid]["decision"] == DECISION_RECOGNIZED
          and new_items[late_cid]["recognized_amount"] == items[late_cid]["transport_basis"],
          "新批次中先前待核联按追溯基数转为运输收入")
    check(batch2["recognized_total"] > old_recognized, "新批次确认收入增加")
    check([b["batch_id"] for b in wh.list_batches()] == [1, 2], "批次有序且可重读")

    # OPEN 与非 USED 终态解释。
    decisions = {it["decision"] for it in batch2["items"]}
    check(DECISION_PENDING_OPEN in decisions, "仍 OPEN 的联保持待核")
    check(DECISION_TERMINAL_NOT_USED in decisions,
          "REFUNDED/EXCHANGED 联解释为非运输（重签原联不在此确认收入）")
    return wh


# --------------------------------------------------------------------------- 7. 事件重放


def scenario_replay(workdir: str, svc: TicketingService) -> None:
    print("\n[7] 事件重放：仅追加日志重建物化状态，逐表切面比对一致")
    journal = svc.export_journal()
    replay_path = os.path.join(workdir, "replayed.db")
    replayed = replay_journal(journal, replay_path)
    check(replayed.export_journal() == journal, "重放后请求日志逐字节一致")
    check(replayed.snapshot()["coupons"] == svc.snapshot()["coupons"],
          "重放库票联切面（含状态/原票值/祖先关系）与原库一致")

    def _semantic(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # created_at 是墙钟时间戳，不属于可重放的业务语义，比对时剔除。
        return [{k: v for k, v in row.items() if k != "created_at"} for row in rows]

    check(
        _semantic(replayed.snapshot()["exchanges"])
        == _semantic(svc.snapshot()["exchanges"]),
        "重放库重签关系（票值等式、补退、新旧票映射）与原库一致",
    )
    check(
        replayed.snapshot()["exchange_parents"] == svc.snapshot()["exchange_parents"],
        "重放库祖先链与原库一致",
    )
    print("   重放库:", replay_path)


# --------------------------------------------------------------------------- 8. 独立核票值去向


def scenario_reconciliation(svc: TicketingService) -> None:
    print("\n[8] 独立核票值去向：首次付款/后续补退/最终联状态三口径守恒")
    snap = svc.snapshot()
    report = reconcile(snap)
    check(report["balanced"], "对账平衡，无票值凭空产生或消失")
    print("   汇总（分）:", json.dumps(report["totals"], ensure_ascii=False))
    totals = report["totals"]
    check(
        totals["issuance_fare"]
        == totals["transport_basis_stock"] + totals["refund_amounts"]
        + totals["refund_fees"] + totals["exchange_refunds"],
        "运输恒等式：首次付款 = 运输基数存量 + 退票退款 + 退票手续费 + 重签退还",
    )
    check(
        totals["issuance_fare"] + totals["surcharges"]
        == totals["cash_stock"] + totals["refund_amounts"]
        + totals["refund_fees"] + totals["exchange_refunds"],
        "现金恒等式：首次付款+补收 = 票联现金存量 + 退票退款 + 退票手续费 + 重签退还",
    )


# --------------------------------------------------------------------------- 编排


def run_all() -> dict[str, Any]:
    workdir = tempfile.mkdtemp(prefix="airline_")
    print("工作目录（SQLite 物化库，可事后检查）:", workdir)

    # 一分余数案例使用独立服务/独立库，与主生命周期隔离。
    scenario_one_cent_remainder()
    main_svc = TicketingService(os.path.join(workdir, "ticketing.db"))
    ticket = main_svc.issue_ticket(
        "REQ-MAIN", [("SEG-1", 1), ("SEG-2", 1), ("SEG-3", 1), ("SEG-4", 1)], 400
    )
    ex1 = scenario_partial_reissue(main_svc, ticket)
    ex2 = scenario_two_consecutive_reissues(main_svc, ex1["exchange1"])
    scenario_refund_use_race(workdir)
    scenario_idempotent_retry(main_svc, ticket)
    wh = scenario_revenue_warehouse(main_svc, ex2["exchange2"])
    scenario_replay(workdir, main_svc)
    scenario_reconciliation(main_svc)

    print("\n全部场景通过。库文件目录:", workdir)
    return {"workdir": workdir, "service": main_svc, "warehouse": wh}


if __name__ == "__main__":  # pragma: no cover
    run_all()
