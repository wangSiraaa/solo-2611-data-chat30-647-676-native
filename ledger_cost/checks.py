"""独立守恒核验：只读取台账快照与成本产品，自行按有理数重算。

核验口径完全独立于 :mod:`ledger_cost.engine` 的计算路径：

1. 质量平衡：根产量在“留存 + 全部批次损耗 + 全部现存产出”间逐克守恒；
   每批投入 == 产出合计 + 损耗；不存在零克产出。
2. 成本守恒（精确有理数）：每个可计价根的期初成本 == 留存成本 +
   沿真实消耗比例流入各批的投入成本；每批投入成本 + 加工费 ==
   各产出精确成本之和（损耗不携带可出售成本）。
3. 发布守恒：每批发布整数总额 == 各产出发布分之和（一分钱也不能丢/多）。
4. 不可计价：零参考价值分母批及其依赖批无任何成本数字（不填零）。
"""

from __future__ import annotations

from fractions import Fraction

from .models import CostProduct, Snapshot

ZERO = Fraction(0)


# ---------------------------------------------------------------- 质量守恒


def check_mass_balance(snapshot: Snapshot) -> dict:
    failures: list[str] = []

    # 每批自身守恒 + 零克产出检查
    for b in snapshot.batches.values():
        out_total = sum(o.grams for o in b.outputs)
        if out_total + b.loss_grams != b.input_grams:
            failures.append(
                f"{b.batch_id}: 投入 {b.input_grams} != 产出 {out_total} + 损耗 {b.loss_grams}"
            )
        if any(o.grams <= 0 for o in b.outputs):
            failures.append(f"{b.batch_id}: 存在零克/负克产出")

    # 全局质量守恒（按物料形态逐克核对，中间品不重复计）。
    # 任一时刻物料只有三种形态：未被消耗的批号留存、不可回收损耗、最终产出。
    # 根产量 == 全部批号留存之和（根留存 + 中间批号留存 + 最终产出，互斥不重叠）
    #        + 全部批次损耗。
    root_mass: dict[str, int] = {r: snapshot.lots[r].produced_grams for r in snapshot.roots}
    loss_sum = sum(b.loss_grams for b in snapshot.batches.values())
    lot_retained: dict[str, int] = {}
    for lot in snapshot.lots.values():
        if lot.available_grams < 0:
            failures.append(f"{lot.lot_id}: 可用量为负 {lot.available_grams}")
        if lot.consumed_grams > lot.produced_grams:
            failures.append(
                f"{lot.lot_id}: 累计消耗 {lot.consumed_grams} 超过产量 {lot.produced_grams}"
            )
        lot_retained[lot.lot_id] = lot.available_grams
    retained_total = sum(lot_retained.values())
    if sum(root_mass.values()) != retained_total + loss_sum:
        failures.append(
            f"全局质量不平衡: 根产量 {sum(root_mass.values())} != "
            f"批号留存合计 {retained_total} + 损耗 {loss_sum}"
        )

    # 可用量非负已在上面的批号级核对完成
    return {"ok": not failures, "failures": failures,
            "root_mass": root_mass, "lot_retained": lot_retained,
            "retained_total": retained_total, "loss_sum": loss_sum}


# ---------------------------------------------------------------- 成本守恒


def _root_of_batch(snapshot: Snapshot, batch_id: str) -> str:
    b = snapshot.batches[batch_id]
    lot = snapshot.lots[b.input_lot_id]
    while lot.origin_batch is not None:
        lot = snapshot.lots[snapshot.batches[lot.origin_batch].input_lot_id]
    return lot.lot_id


def check_cost_conservation(
    snapshot: Snapshot,
    products: dict[str, CostProduct],
    opening_costs: dict[str, Fraction],
    fees: dict[str, Fraction],
) -> dict:
    failures: list[str] = []
    details: dict[str, dict] = {}

    # 根产品总成本必须等于期初成本
    for root_id in snapshot.roots:
        p = products[root_id]
        if not p.priceable:
            continue
        if p.exact_total_cost != opening_costs.get(root_id):
            failures.append(
                f"{root_id}: 根产品精确成本 {p.exact_total_cost} "
                f"!= 期初 {opening_costs.get(root_id)}"
            )

    # 按根分组逐批核验。可计价批：投入成本 + 加工费 == 产出精确成本之和；
    # 不可计价批：产出不得出现任何成本数字，但其投入成本是根流出的一部分，
    # 随不可计价区域整体沉淀（不分摊给可出售产出）。
    root_groups: dict[str, list[str]] = {}
    for bid in snapshot.batches:
        root_groups.setdefault(_root_of_batch(snapshot, bid), []).append(bid)

    for root_id, batch_ids in root_groups.items():
        root_p = products[root_id]
        if not root_p.priceable:
            continue
        flows_in = ZERO          # 根沿真实消耗比例直接流出的成本（仅直接消费根的批）
        fee_total = ZERO
        flows_out = ZERO
        unpriceable_sink = ZERO
        for bid in sorted(batch_ids):
            b = snapshot.batches[bid]
            in_p = products[b.input_lot_id]
            outs = [products[o.lot_id] for o in b.outputs]
            fee = fees.get(bid, ZERO)
            directly_from_root = b.input_lot_id == root_id
            if directly_from_root:
                input_cost = in_p.exact_unit_cost * b.input_grams
                flows_in += input_cost

            if not in_p.priceable:
                # 上游不可计价 → 本批全部产出必须同样无成本数字；
                # 投入没有精确成本，不参与可计价资金流
                for op in outs:
                    if (
                        op.priceable
                        or op.exact_total_cost is not None
                        or op.published_total_cost is not None
                    ):
                        failures.append(
                            f"{bid}: 上游不可计价但产出 {op.lot_id} 被填入成本"
                        )
                if directly_from_root:
                    unpriceable_sink += input_cost + fee
                continue

            if not directly_from_root:
                input_cost = in_p.exact_unit_cost * b.input_grams
            if all(not p.priceable for p in outs):
                # 本批分母为零：投入流在不可计价区域整体沉淀（不分摊给可出售产出）
                for op in outs:
                    if (
                        op.exact_total_cost is not None
                        or op.published_total_cost is not None
                    ):
                        failures.append(
                            f"{bid}: 不可计价产出 {op.lot_id} 被填入成本（禁止填零）"
                        )
                unpriceable_sink += input_cost + fee
                continue

            fee_total += fee
            out_sum = sum((p.exact_total_cost for p in outs if p.priceable), ZERO)
            if input_cost + fee != out_sum:
                failures.append(
                    f"{bid}: 投入 {input_cost} + 加工费 {fee} "
                    f"!= 产出精确成本合计 {out_sum}"
                )
            flows_out += out_sum
        # 根级守恒：期初 == 根留存成本 + 根直接流出（下游批间流动是同一笔钱的形态转换）
        root_lot = snapshot.lots[root_id]
        retained_g = root_lot.produced_grams - root_lot.consumed_grams
        retained_cost = root_p.exact_unit_cost * retained_g
        if opening_costs[root_id] != retained_cost + flows_in:
            failures.append(
                f"{root_id}: 期初 {opening_costs[root_id]} != "
                f"留存 {retained_cost} + 根直接流出 {flows_in}"
            )
        details[root_id] = {
            "opening": opening_costs[root_id],
            "retained_cost": retained_cost,
            "retained_grams": retained_g,
            "direct_outflow": flows_in,
            "fees_on_priceable_batches": fee_total,
            "unpriceable_sink": unpriceable_sink,
            "priceable_outputs_carrying_cost": flows_out,
        }

    # 不可计价区域：零分母批及其后代必须无成本数字
    for b in snapshot.batches.values():
        for o in b.outputs:
            p = products[o.lot_id]
            if not p.priceable and (p.exact_total_cost is not None or p.published_total_cost is not None):
                failures.append(f"{o.lot_id}: 不可计价产品被填入了成本数字（禁止填零）")
    return {"ok": not failures, "failures": failures, "roots": details}


# ---------------------------------------------------------------- 发布守恒


def check_published_totals(snapshot: Snapshot, products: dict[str, CostProduct]) -> dict:
    failures: list[str] = []
    details = {}
    for b in snapshot.batches.values():
        outs = [products[o.lot_id] for o in b.outputs]
        if all(not p.priceable for p in outs):
            continue
        exact_pool = sum(
            (p.exact_total_cost for p in outs if p.priceable), ZERO
        )
        published_sum = sum(p.published_total_cost for p in outs if p.priceable)
        # 统一四舍五入口径独立复算
        n, d = exact_pool.numerator, exact_pool.denominator
        q, r = divmod(n, d)
        expected_pool = q + (1 if 2 * r >= d else 0)
        if published_sum != expected_pool:
            failures.append(
                f"{b.batch_id}: 发布合计 {published_sum} != 池四舍五入 {expected_pool}"
            )
        # 份额单调性检查：发布分必须在精确份额下取整或补 1 分范围内
        for p in outs:
            share = exact_pool * Fraction(p.grams) * p.reference_unit_value / sum(
                (Fraction(o.grams) * products[o.lot_id].reference_unit_value for o in b.outputs),
                ZERO,
            )
            floor_v = share.numerator // share.denominator
            if not (floor_v <= p.published_total_cost <= floor_v + 1):
                failures.append(
                    f"{b.batch_id}/{p.lot_id}: 发布 {p.published_total_cost} "
                    f"超出精确份额 {share} 的下取整+1 范围"
                )
        details[b.batch_id] = {
            "exact_pool": exact_pool,
            "published_pool": published_sum,
            "allocations": {p.lot_id: p.published_total_cost for p in outs if p.priceable},
        }
    return {"ok": not failures, "failures": failures, "batches": details}


def check_cut(
    snapshot: Snapshot,
    products: dict[str, CostProduct],
    opening_costs: dict[str, Fraction],
    fees: dict[str, Fraction],
) -> dict:
    mass = check_mass_balance(snapshot)
    cost = check_cost_conservation(snapshot, products, opening_costs, fees)
    published = check_published_totals(snapshot, products)
    return {
        "ok": mass["ok"] and cost["ok"] and published["ok"],
        "mass": mass,
        "cost": cost,
        "published": published,
    }
