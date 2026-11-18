"""成本计算纯函数引擎（不接触数据库，便于独立核验）。

规则：

* 对固定生产切面，从根原料沿“实际消耗克数 / 实际产量”的真实消耗比例传递
  精确有理数成本（:class:`fractions.Fraction`，全程无浮点）。
* 某批投入成本 + 加工费在其非零产出间按 ``产量 × 参考单位价值`` 比例分摊；
  损耗不分摊可出售成本。分母为零时该批及其全部依赖批标记不可计价，不填零。
* 发布金额：先把整批待分配成本统一四舍五入到整数分，再对各产出按精确份额
  下取整、按最大余数补分（余数并列按 lot_id 字典序），明细之和恒等于本批整数总额。
* 成本产品同时保留精确值与发布值；下游传递只用精确值，不逐层重复舍入。
"""

from __future__ import annotations

from collections import deque
from fractions import Fraction
from typing import Optional

from .models import BatchCostView, CostProduct, Snapshot

ZERO = Fraction(0)


# ---------------------------------------------------------------- 舍入与补分


def round_half_up(value: Fraction) -> int:
    """非负有理数四舍五入到整数（0.5 进位），返回整数分。"""
    n, d = value.numerator, value.denominator
    q, r = divmod(n, d)
    return q + (1 if 2 * r >= d else 0)


def largest_remainder_allocation(
    total_cents: int, weights: list[tuple[str, Fraction]],
) -> dict[str, int]:
    """把整数分 ``total_cents`` 按精确权重分摊。

    先各自下取整，再把剩余分按最大余数补发；余数并列按 lot_id 字典序。
    权重之和必须为正，且约定调用方已保证 ``sum(权重份额) == total_cents``。
    """
    weight_sum = sum(w for _, w in weights)
    if weight_sum <= 0:
        raise ValueError("最大余数分摊要求正权重")
    floors: dict[str, int] = {}
    remainders: list[tuple[Fraction, str]] = []
    for lot_id, w in weights:
        exact_share = Fraction(total_cents) * w / weight_sum
        floor_share = exact_share.numerator // exact_share.denominator
        floors[lot_id] = floor_share
        remainders.append((exact_share - floor_share, lot_id))
    leftover = total_cents - sum(floors.values())
    # 大余数优先；并列按 lot_id 升序，保证完全确定
    remainders.sort(key=lambda x: (-x[0], x[1]))
    for _, lot_id in remainders[:leftover]:
        floors[lot_id] += 1
    assert sum(floors.values()) == total_cents
    return floors


# ---------------------------------------------------------------- 单批评估


def _unpriceable_products(
    batch, refs: dict[str, Fraction], reason: str,
) -> tuple[list[CostProduct], BatchCostView, bool]:
    products = [
        CostProduct(
            lot_id=o.lot_id,
            exact_total_cost=None,
            published_total_cost=None,
            exact_unit_cost=None,
            reference_unit_value=refs.get(o.lot_id, ZERO),
            grams=o.grams,
            priceable=False,
            reason=reason,
            origin_batch=batch.batch_id,
        )
        for o in batch.outputs
    ]
    view = BatchCostView(
        batch_id=batch.batch_id,
        cut_id="",
        input_lot_id=batch.input_lot_id,
        input_grams=batch.input_grams,
        input_exact_cost=None,
        processing_fee=ZERO,
        pool_exact=None,
        pool_published=None,
        weight_total=ZERO,
        priceable=False,
        reason=reason,
        products=tuple(products),
    )
    return products, view, False


def evaluate_batch(
    batch,
    input_product: CostProduct,
    fees: dict[str, Fraction],
    refs: dict[str, Fraction],
) -> tuple[list[CostProduct], BatchCostView]:
    """评估单个生产批，返回产出成本产品与批次分摊视图。"""
    fee = fees.get(batch.batch_id, ZERO)

    if not input_product.priceable:
        reason = f"上游批号 {batch.input_lot_id} 不可计价：{input_product.reason}"
        products, view, _ = _unpriceable_products(batch, refs, reason)
        view = BatchCostView(
            batch_id=view.batch_id, cut_id="", input_lot_id=view.input_lot_id,
            input_grams=view.input_grams, input_exact_cost=None,
            processing_fee=fee, pool_exact=None, pool_published=None,
            weight_total=ZERO, priceable=False, reason=reason,
            products=view.products,
        )
        return products, view

    # 沿真实消耗比例取投入成本：单位精确成本 × 实际投入克数
    input_exact_cost = input_product.exact_unit_cost * batch.input_grams
    pool_exact = input_exact_cost + fee

    weights: list[tuple[str, Fraction]] = []
    for o in batch.outputs:
        ref = refs.get(o.lot_id, ZERO)
        weights.append((o.lot_id, Fraction(o.grams) * ref))
    weight_total = sum((w for _, w in weights), ZERO)

    if weight_total == 0:
        reason = "分母为零：Σ(产量×参考单位价值)=0，本批及依赖批不可计价，不填零"
        products, view, _ = _unpriceable_products(batch, refs, reason)
        view = BatchCostView(
            batch_id=view.batch_id, cut_id="", input_lot_id=view.input_lot_id,
            input_grams=view.input_grams, input_exact_cost=input_exact_cost,
            processing_fee=fee, pool_exact=pool_exact, pool_published=None,
            weight_total=ZERO, priceable=False, reason=reason,
            products=view.products,
        )
        return products, view

    pool_published = round_half_up(pool_exact)
    published_map = largest_remainder_allocation(pool_published, weights)

    products: list[CostProduct] = []
    for o in batch.outputs:
        ref = refs[o.lot_id] if o.lot_id in refs else ZERO
        exact_total = pool_exact * Fraction(o.grams) * ref / weight_total
        products.append(
            CostProduct(
                lot_id=o.lot_id,
                exact_total_cost=exact_total,
                published_total_cost=published_map[o.lot_id],
                exact_unit_cost=exact_total / o.grams,
                reference_unit_value=ref,
                grams=o.grams,
                priceable=True,
                reason=None,
                origin_batch=batch.batch_id,
            )
        )
    view = BatchCostView(
        batch_id=batch.batch_id,
        cut_id="",
        input_lot_id=batch.input_lot_id,
        input_grams=batch.input_grams,
        input_exact_cost=input_exact_cost,
        processing_fee=fee,
        pool_exact=pool_exact,
        pool_published=pool_published,
        weight_total=weight_total,
        priceable=True,
        reason=None,
        products=tuple(products),
    )
    return products, view


# ---------------------------------------------------------------- 全量拓扑重算


def full_compute(
    snapshot: Snapshot,
    root_costs: dict[str, Fraction],
    fees: dict[str, Fraction],
    refs: dict[str, Fraction],
) -> tuple[dict[str, CostProduct], dict[str, BatchCostView]]:
    """从根原料开始的全量拓扑重算。"""
    products: dict[str, CostProduct] = {}
    batch_views: dict[str, BatchCostView] = {}

    # 根批号产品：承载完整期初成本；部分留存的留存成本由单位成本×留存克数保留
    for root_id in snapshot.roots:
        lot = snapshot.lots[root_id]
        opening = root_costs.get(root_id)
        ref = refs.get(root_id, ZERO)
        if opening is None:
            products[root_id] = CostProduct(
                lot_id=root_id, exact_total_cost=None, published_total_cost=None,
                exact_unit_cost=None, reference_unit_value=ref,
                grams=lot.produced_grams, priceable=False,
                reason="缺少期初原料总成本版本", origin_batch=None,
            )
        else:
            unit = opening / lot.produced_grams
            # 发布口径：根成本本身就是整数分，直接发布；留存部分发布值按整数克截断口径
            products[root_id] = CostProduct(
                lot_id=root_id, exact_total_cost=opening,
                published_total_cost=round_half_up(opening),
                exact_unit_cost=unit, reference_unit_value=ref,
                grams=lot.produced_grams, priceable=True, reason=None,
                origin_batch=None,
            )

    for batch in snapshot.topological_batches():
        input_product = products[batch.input_lot_id]
        new_products, view = evaluate_batch(batch, input_product, fees, refs)
        for p in new_products:
            products[p.lot_id] = p
        batch_views[batch.batch_id] = view
    return products, batch_views


# ---------------------------------------------------------------- 增量重算


def closure(snapshot: Snapshot, seed_batches: set[str]) -> set[str]:
    """种子批沿产出-消耗边向下展开的全部受影响批（含种子自身）。"""
    affected = set(seed_batches)
    queue: deque[str] = deque(seed_batches)
    while queue:
        cur = queue.popleft()
        b = snapshot.batches.get(cur)
        if b is None:
            continue
        for lot_id in b.output_lot_ids:
            for other in snapshot.batches.values():
                if other.input_lot_id == lot_id and other.batch_id not in affected:
                    affected.add(other.batch_id)
                    queue.append(other.batch_id)
    return affected


def changed_batches(
    snapshot: Snapshot,
    root_costs_old: dict[str, Fraction],
    root_costs_new: dict[str, Fraction],
    fees_old: dict[str, Fraction],
    fees_new: dict[str, Fraction],
    refs_old: dict[str, Fraction],
    refs_new: dict[str, Fraction],
) -> set[str]:
    """比较两版成本输入，返回必须重算的种子批（调用方再取后代闭包）。"""
    seeds: set[str] = set()
    for bid, fee in fees_new.items():
        if fees_old.get(bid, ZERO) != fee:
            seeds.add(bid)
    for lot_id, value in refs_new.items():
        if refs_old.get(lot_id, ZERO) != value:
            lot = snapshot.lots.get(lot_id)
            if lot and lot.origin_batch:
                seeds.add(lot.origin_batch)
    for root_id, cost in root_costs_new.items():
        if root_costs_old.get(root_id) != cost:
            for b in snapshot.batches.values():
                # 消费该根，或祖先链最终汇入该根的批
                if _root_feeds(snapshot, root_id, b.batch_id):
                    seeds.add(b.batch_id)
    return seeds


def _root_feeds(snapshot: Snapshot, root_id: str, batch_id: str) -> bool:
    seen: set[str] = set()
    cur = batch_id
    while cur and cur not in seen:
        seen.add(cur)
        b = snapshot.batches[cur]
        in_lot = b.input_lot_id
        if in_lot == root_id:
            return True
        parent = snapshot.lots[in_lot].origin_batch
        cur = parent
    return False


def incremental_compute(
    snapshot: Snapshot,
    root_costs: dict[str, Fraction],
    fees: dict[str, Fraction],
    refs: dict[str, Fraction],
    old_products: dict[str, CostProduct],
    affected_batches: set[str],
) -> tuple[dict[str, CostProduct], dict[str, BatchCostView]]:
    """依赖索引增量更新：只重算受影响批及其后代，未变批复用旧产品。

    ``affected_batches`` 应为种子批的后代闭包（见 :func:`closure`）。
    返回的产品集合与全量重算口径一致，可直接逐字段比对验证。
    """
    products: dict[str, CostProduct] = {}
    batch_views: dict[str, BatchCostView] = {}

    # 根产品始终重建（成本只做有理数切分，成本极低且保证口径统一）
    for root_id in snapshot.roots:
        lot = snapshot.lots[root_id]
        opening = root_costs.get(root_id)
        ref = refs.get(root_id, ZERO)
        if opening is None:
            products[root_id] = CostProduct(
                lot_id=root_id, exact_total_cost=None, published_total_cost=None,
                exact_unit_cost=None, reference_unit_value=ref,
                grams=lot.produced_grams, priceable=False,
                reason="缺少期初原料总成本版本", origin_batch=None,
            )
        else:
            products[root_id] = CostProduct(
                lot_id=root_id, exact_total_cost=opening,
                published_total_cost=round_half_up(opening),
                exact_unit_cost=opening / lot.produced_grams,
                reference_unit_value=ref, grams=lot.produced_grams,
                priceable=True, reason=None, origin_batch=None,
            )

    for batch in snapshot.topological_batches():
        if batch.batch_id not in affected_batches:
            for o in batch.outputs:
                old_p = old_products[o.lot_id]
                products[o.lot_id] = CostProduct(
                    lot_id=old_p.lot_id,
                    exact_total_cost=old_p.exact_total_cost,
                    published_total_cost=old_p.published_total_cost,
                    exact_unit_cost=old_p.exact_unit_cost,
                    reference_unit_value=refs.get(o.lot_id, old_p.reference_unit_value),
                    grams=old_p.grams,
                    priceable=old_p.priceable,
                    reason=old_p.reason,
                    origin_batch=old_p.origin_batch,
                )
            # 未变批的批次视图依据旧产品重建（输入成本随上游单位成本联动校验）
            input_product = products[batch.input_lot_id]
            _, view = evaluate_batch(batch, input_product, fees, refs)
            batch_views[batch.batch_id] = view
        else:
            input_product = products[batch.input_lot_id]
            new_products, view = evaluate_batch(batch, input_product, fees, refs)
            for p in new_products:
                products[p.lot_id] = p
            batch_views[batch.batch_id] = view
    return products, batch_views


# ---------------------------------------------------------------- 变化解释


def explain_unit_change(
    snapshot: Snapshot,
    old_products: dict[str, CostProduct],
    new_products: dict[str, CostProduct],
    lot_id: str,
) -> dict:
    """解释一个未被直接改动的批号单位成本为何变化：沿谱系定位首个变化的祖先。"""
    if lot_id not in new_products:
        raise KeyError(f"新切面中不存在批号 {lot_id}")
    old_p = old_products.get(lot_id)
    new_p = new_products[lot_id]

    chain: list[dict] = []
    cur_lot = lot_id
    guard = 0
    while cur_lot is not None and guard <= 100:
        lot = snapshot.lots.get(cur_lot)
        op = old_products.get(cur_lot)
        np = new_products.get(cur_lot)
        entry = {
            "lot_id": cur_lot,
            "old_unit": op.exact_unit_cost if op and op.priceable else None,
            "new_unit": np.exact_unit_cost if np and np.priceable else None,
            "old_priceable": bool(op and op.priceable),
            "new_priceable": bool(np and np.priceable),
            "changed": not _same_product(op, np),
            "origin_batch": lot.origin_batch if lot else None,
        }
        chain.append(entry)
        if lot is None or lot.origin_batch is None:
            break
        cur_lot = snapshot.batches[lot.origin_batch].input_lot_id
        guard += 1

    first_changed = next((c for c in reversed(chain) if c["changed"]), None)
    if first_changed is None:
        reason = "该批号单位成本在两个切面间没有变化"
    elif first_changed["origin_batch"] is None:
        reason = (
            f"根批号 {first_changed['lot_id']} 的期初总成本版本发生变化，"
            "沿真实消耗比例向下游逐批传递，导致本批号单位成本变化"
        )
    else:
        b = snapshot.batches[first_changed["origin_batch"]]
        reason = (
            f"首个发生变化的祖先为批号 {first_changed['lot_id']}（来自生产批 "
            f"{first_changed['origin_batch']}，投入 {b.input_lot_id} "
            f"{b.input_grams}g，加工费/参考价值版本变化后重新分摊），"
            "其精确单位成本变化经后代批次按真实比例继续传递"
        )
    return {
        "lot_id": lot_id,
        "old_unit": old_p.exact_unit_cost if old_p and old_p.priceable else None,
        "new_unit": new_p.exact_unit_cost if new_p and new_p.priceable else None,
        "first_changed_lot": first_changed["lot_id"] if first_changed else None,
        "lineage": chain,
        "reason": reason,
    }


def _same_product(a: Optional[CostProduct], b: Optional[CostProduct]) -> bool:
    if a is None or b is None:
        return a is b
    return (
        a.priceable == b.priceable
        and a.exact_total_cost == b.exact_total_cost
        and a.exact_unit_cost == b.exact_unit_cost
        and a.published_total_cost == b.published_total_cost
        and a.reference_unit_value == b.reference_unit_value
    )
