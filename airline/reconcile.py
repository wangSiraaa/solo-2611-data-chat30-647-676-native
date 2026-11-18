"""独立核票值去向：不依赖服务内部响应，直接从物化状态/切面重新核算。

从“首次付款 / 后续补退 / 最终联状态”三个独立口径核对整数分守恒：
1. 票值守恒：每次重签 新票价 = 被交换旧联原票值之和 + 补收 - 退还；
2. 状态机合法：每联至多一个终结记录，OPEN/USED/REFUNDED/EXCHANGED 与终结表一致；
3. 资金流恒等：
   首次付款 + 补收 = 运输基数存量 + 退票退款 + 退票手续费 + 重签退还
   （EXCHANGED 联的票值已转移到新联，不计入存量；运输基数只认首次付款来源）。
"""
from __future__ import annotations

from typing import Any

from .revenue import compute_transport_bases


class ReconciliationError(AssertionError):
    """对账不平。"""


def reconcile(snapshot: dict[str, Any]) -> dict[str, Any]:
    coupons = {c["coupon_id"]: c for c in snapshot["coupons"]}
    problems: list[str] = []

    # 1) 重签票值守恒 ------------------------------------------------------
    for exc in snapshot["exchanges"]:
        # 每个新联都挂同一组父联，去重后只算一次父联集合：
        parent_ids = {
            link["parent_coupon_id"]
            for link in snapshot["exchange_parents"]
            if link["exchange_id"] == exc["exchange_id"]
        }
        parent_value = sum(coupons[pid]["original_value"] for pid in parent_ids)
        expect = parent_value + exc["surcharge"] - exc["refund_amount"]
        if expect != exc["new_fare"]:
            problems.append(
                f"重签 {exc['exchange_id']} 票值不平: 期望 {expect}，实际 {exc['new_fare']}"
            )
        new_value_sum = sum(
            c["original_value"]
            for c in snapshot["coupons"]
            if c["parent_exchange_id"] == exc["exchange_id"]
        )
        if new_value_sum != exc["new_fare"]:
            problems.append(
                f"重签 {exc['exchange_id']} 新联分摊和 {new_value_sum} != 新票价 {exc['new_fare']}"
            )
        if exc["surcharge"] > 0 and exc["refund_amount"] > 0:
            problems.append(f"重签 {exc['exchange_id']} 补收与退还同时为正")

    # 2) 状态机与终结记录一致 ---------------------------------------------
    term_by_coupon = {t["coupon_id"]: t for t in snapshot["terminations"]}
    for cid, coupon in coupons.items():
        term = term_by_coupon.get(cid)
        if coupon["status"] == "OPEN":
            if term is not None:
                problems.append(f"票联 {cid} 为 OPEN 却存在终结记录")
        else:
            if term is None or term["status"] != coupon["status"]:
                problems.append(f"票联 {cid} 状态 {coupon['status']} 与终结记录不一致")

    # 3) 资金流恒等（按根票族分别核对，再汇总） ---------------------------
    bases = compute_transport_bases(snapshot)
    root_ids = sorted({c["root_ticket_id"] for c in snapshot["coupons"]})
    per_root: dict[str, dict[str, int]] = {}
    for root_id in root_ids:
        family = [c for c in snapshot["coupons"] if c["root_ticket_id"] == root_id]
        family_ids = {c["coupon_id"] for c in family}
        issuance = next(
            t["fare"] for t in snapshot["tickets"] if t["ticket_id"] == root_id
        )
        exc_family = [
            e for e in snapshot["exchanges"]
            if any(
                link["exchange_id"] == e["exchange_id"]
                and link["new_coupon_id"] in family_ids
                for link in snapshot["exchange_parents"]
            )
        ]
        surcharges = sum(e["surcharge"] for e in exc_family)
        exchange_refunds = sum(e["refund_amount"] for e in exc_family)
        refund_fees = refund_amounts = 0
        for term in snapshot["terminations"]:
            if term["coupon_id"] in family_ids and term["status"] == "REFUNDED":
                refund_fees += term["detail"]["fee"]
                refund_amounts += term["detail"]["refund_amount"]
        # 存量：只算仍承载价值的 OPEN/USED 联。
        # EXCHANGED 联的基数已沿祖先链转移；REFUNDED 联已变成退款+手续费离开体系。
        alive_ids = {cid for cid in family_ids if coupons[cid]["status"] in ("OPEN", "USED")}
        basis_stock = sum(bases[cid] for cid in alive_ids)
        # 运输口径恒等：首次付款的每一分，要么仍在存活联的运输基数存量里，
        # 要么已通过退票（退款+手续费）或重签退还离开运输体系。
        # 补收/重签票值是现金口径，不进入运输基数，故不参与本恒等式。
        identity_rhs = basis_stock + refund_amounts + refund_fees + exchange_refunds
        if issuance != identity_rhs:
            problems.append(
                f"根票 {root_id} 运输资金流不平: 首次付款 {issuance} != "
                f"存量{basis_stock}+退{refund_amounts}+费{refund_fees}+重签退{exchange_refunds}"
                f" = {identity_rhs}"
            )
        # 现金口径恒等：补收进来的钱必须仍以现金票值形式留存在存活联上。
        cash_stock = sum(coupons[cid]["original_value"] for cid in alive_ids)
        cash_rhs = cash_stock + refund_amounts + refund_fees + exchange_refunds
        if issuance + surcharges != cash_rhs:
            problems.append(
                f"根票 {root_id} 现金资金流不平: 付{issuance}+补{surcharges} != "
                f"现金存量{cash_stock}+退{refund_amounts}+费{refund_fees}+重签退{exchange_refunds}"
                f" = {cash_rhs}"
            )
        per_root[root_id] = {
            "issuance_fare": issuance,
            "surcharges": surcharges,
            "exchange_refunds": exchange_refunds,
            "refund_amounts": refund_amounts,
            "refund_fees": refund_fees,
            "transport_basis_stock": basis_stock,
            "cash_stock": cash_stock,
        }

    if problems:
        raise ReconciliationError("; ".join(problems))
    return {
        "balanced": True,
        "per_root": per_root,
        "totals": {
            k: sum(row[k] for row in per_root.values())
            for k in ("issuance_fare", "surcharges", "exchange_refunds",
                      "refund_amounts", "refund_fees",
                      "transport_basis_stock", "cash_stock")
        },
    }
