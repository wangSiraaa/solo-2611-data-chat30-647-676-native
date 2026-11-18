"""可复跑演示：构建合成场景，逐节展示生产台账与成本仓的全部要求案例。

运行：
    python -m ledger_cost.demo [数据库文件路径]

不传路径时使用临时文件并在结束后保留路径打印；显式传路径时数据库落盘，
可用 :class:`ledger_cost.scenario.open_systems` 重开验证持久化。
"""

from __future__ import annotations

import os
import sys
import tempfile
from fractions import Fraction

from .checks import check_cut
from .cost import CostWarehouse
from .errors import RejectedByConsumption
from .production import ProductionLedger
from .scenario import (
    B9_CORRECTION,
    FEES_V1,
    OPENING_COSTS_V1,
    build_cost_inputs_v1,
    build_cost_inputs_v2,
    build_ledger,
    open_systems,
)
from .storage import Storage


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show_cut(warehouse: CostWarehouse, ledger: ProductionLedger, cut_id: str) -> None:
    snap = ledger.build_snapshot()
    products = {p.lot_id: p for p in warehouse.list_products(cut_id)}
    opening = {k: Fraction(v) for k, v in OPENING_COSTS_V1.items()}
    fees = {k: Fraction(v) for k, v in FEES_V1.items() if k in snap.batches}
    report = check_cut(snap, products, opening, fees)
    print(f"[独立守恒核验 cut={cut_id}] 总体: {'通过' if report['ok'] else '失败'}")
    m = report["mass"]
    print(f"  质量: 根产量 {sum(m['root_mass'].values())}g = "
          f"批号留存合计 {m['retained_total']}g + 损耗 {m['loss_sum']}g")
    for root_id, d in report["cost"]["roots"].items():
        if d["retained_cost"] is None:
            continue
        print(f"  成本[{root_id}]: 期初 {d['opening']} = 留存 {d['retained_cost']} "
              f"(留存 {d['retained_grams']}g) + 根直接流出 {d['direct_outflow']}"
              + (f"，其中不可计价沉淀 {d['unpriceable_sink']}" if d["unpriceable_sink"] else ""))
    if not report["ok"]:
        print("  失败明细:", report)


def main(db_path: str | None = None) -> str:
    own_path = db_path is None
    if db_path is None:
        db_path = os.path.join(tempfile.gettempdir(), "ledger_cost_demo.db")
        if os.path.exists(db_path):
            os.remove(db_path)

    storage = Storage(db_path)
    ledger = build_ledger(storage)
    warehouse = CostWarehouse(storage, ledger)

    # ------------------------------------------------------------ 1. 无成本台账
    hr("1. 无成本数据时的独立生产台账（物料去向查询）")
    snap = ledger.build_snapshot()
    print(f"已登记根批号 {len(snap.roots)} 个、生产批 {len(snap.batches)} 个、"
          f"最大谱系深度 {snap.max_depth}（上限 6）")
    trace = ledger.trace_lot("L1B")
    print(f"trace_lot(L1B): 产量 {trace['produced_grams']}g, 已耗 "
          f"{trace['consumed_grams']}g, 可用 {trace['available_grams']}g, "
          f"下游 {[c['batch_id'] for c in trace['consumers']]}")
    print(f"R1 部分投入 600/1000g，当前可用（留存）= {ledger.available('R1')}g")

    # ------------------------------------------------------------ 2. 成本输入与首切面
    hr("2. 成本仓版本化输入 + 首切面全量拓扑重算（cut-v1）")
    build_cost_inputs_v1(warehouse)
    warehouse.compute_cut("cut-v1", "v1 全量", "root-v1", "fee-v1", "ref-v1",
                          mode="full", note="初始全量重算")
    b1 = warehouse.get_batch_cost("cut-v1", "B1")
    print(f"B1: 投入精确成本 {b1.input_exact_cost} + 加工费 {b1.processing_fee} "
          f"= 池 {b1.pool_exact} 分；四舍五入发布池 {b1.pool_published} 分")
    for p in b1.products:
        print(f"  {p.describe()}")
    retained = warehouse.retained_cost("cut-v1", "R1")
    print(f"R1 部分留存: {retained['retained_grams']}g × 单位 "
          f"{retained['exact_unit_cost']} = 精确留存成本 {retained['exact_retained_cost']} 分，"
          f"发布 {retained['published_retained_cost']} 分")
    show_cut(warehouse, ledger, "cut-v1")

    # ------------------------------------------------------------ 3. 零参考价值
    hr("3. 零参考价值 → 分母为零 → 不可计价（B5/B6，不填零）")
    for lot_id in ("L5A", "L6A"):
        print(" ", warehouse.get_product("cut-v1", lot_id).describe())
    b5 = warehouse.get_batch_cost("cut-v1", "B5")
    print(f"  B5 权重分母 = {b5.weight_total}，原因：{b5.reason}")

    # ------------------------------------------------------------ 4. 一分钱分摊
    hr("4. 一分钱分摊（root-cent 版本：B7 精确池 0.6 分 → 发布 1 分）")
    warehouse.compute_cut("cut-cent", "一分钱", "root-cent", "fee-v1", "ref-v1")
    b7 = warehouse.get_batch_cost("cut-cent", "B7")
    print(f"  精确池 {b7.pool_exact} 分 → 统一四舍五入 {b7.pool_published} 分；"
          f"下取整+最大余数（并列按 lot_id）：")
    for p in b7.products:
        print(f"    {p.lot_id}: 精确 {p.exact_total_cost} 分, 发布 {p.published_total_cost} 分")

    # ------------------------------------------------------------ 5. 下游消耗阻止减量
    hr("5. 后代已实际消耗 → 阻止上游减量更正（整次拒绝，台账不变）")
    try:
        ledger.correct_batch(
            "B1", input_grams=470,
            outputs=[("L1A", 400), ("L1B", 50)], loss_grams=20,
        )
    except RejectedByConsumption as exc:
        print(f"  拒绝: {exc}")
        print(f"  blocking={exc.blocking}（lot, 拟保留克数, 已被下游消耗克数）")
    print(f"  拒绝后 B1 修订号仍为 {ledger.get_batch('B1').revision_no}，"
          f"L1B 可用仍为 {ledger.available('L1B')}g")

    # ------------------------------------------------------------ 6. 价格更正与两套重算对拍
    hr("6. 价格更正（L2A 参考价值 12→18）：全量 vs 依赖索引增量")
    build_cost_inputs_v2(warehouse)
    warehouse.compute_cut("cut-v2", "v2 全量", "root-v1", "fee-v1", "ref-v2",
                          mode="full")
    warehouse.compute_cut("cut-v2i", "v2 增量", "root-v1", "fee-v1", "ref-v2",
                          mode="incremental", note="只重算 B2 闭包 {B2,B3,B11}")
    snap = ledger.build_snapshot()
    mismatch = [
        lot_id for lot_id in snap.lots
        if warehouse.get_product("cut-v2", lot_id).exact_total_cost
        != warehouse.get_product("cut-v2i", lot_id).exact_total_cost
    ]
    print(f"  全量/增量精确值不一致批号: {mismatch or '无（两口径完全一致）'}")
    explanation = warehouse.explain_unit_change("cut-v2", "L3A", old_cut_id="cut-v1")
    print(f"  L3A 单位成本 {explanation['old_unit']} → {explanation['new_unit']}")
    print(f"  变化解释: 首个变化批号={explanation['first_changed_lot']}；{explanation['reason']}")
    show_cut(warehouse, ledger, "cut-v2")

    # ------------------------------------------------------------ 7. 生产更正触发重算
    hr("7. 生产更正（B9: L9A 60→55、L9B 30→35）后全量/增量重算")
    rev_no = ledger.correct_batch("B9", **B9_CORRECTION)
    print(f"  B9 新修订号 {rev_no}，历史修订数 {len(ledger.list_revisions('B9'))}")
    warehouse.compute_cut("cut-v3", "B9 更正后全量", "root-v1", "fee-v1", "ref-v2")
    warehouse.compute_cut("cut-v3i", "B9 更正后增量", "root-v1", "fee-v1", "ref-v2",
                          mode="incremental")
    b9 = warehouse.get_batch_cost("cut-v3", "B9")
    print(f"  B9 发布池 {b9.pool_published} 分，明细 "
          f"{ {p.lot_id: p.published_total_cost for p in b9.products} }，"
          f"合计 {sum(p.published_total_cost for p in b9.products)}")
    why = warehouse.explain_unit_change("cut-v3", "L9B", old_cut_id="cut-v1")
    print(f"  L9B 总成本变化解释: {why['reason']}")
    show_cut(warehouse, ledger, "cut-v3")

    # ------------------------------------------------------------ 8. 撤销
    hr("8. 撤销无下游占用的 B10，投入批号可用量恢复")
    print(f"  撤销前 R5 可用 {ledger.available('R5')}g")
    ledger.cancel_batch("B10")
    print(f"  撤销后 R5 可用 {ledger.available('R5')}g；B10 修订状态: "
          f"{ledger.list_revisions('B10')[0]['status']}")
    warehouse.compute_cut("cut-v4", "B10 撤销后", "root-v1", "fee-v1", "ref-v2")
    show_cut(warehouse, ledger, "cut-v4")
    print("  历史切面 cut-v1 中 L10A 产品仍保留:",
          warehouse.get_product("cut-v1", "L10A").describe())

    # ------------------------------------------------------------ 9. 持久化重开
    hr("9. 关闭重开持久化验证")
    cuts_before = [c["cut_id"] for c in warehouse.list_cuts()]
    storage.close()
    storage2, ledger2, wh2 = open_systems(db_path)
    cuts_after = [c["cut_id"] for c in wh2.list_cuts()]
    print(f"  重开前面切面 {cuts_before}")
    print(f"  重开后切面 {cuts_after}")
    print(f"  B9 修订历史: {[(r['revision_no'], r['status']) for r in ledger2.list_revisions('B9')]}")
    print(f"  重开后查询 cut-v1/L1A: {wh2.get_product('cut-v1', 'L1A').describe()}")
    storage2.close()

    hr("演示完成")
    print(f"数据库文件: {db_path}")
    if own_path:
        print("（临时目录文件，可自行删除；或重新运行本脚本覆盖）")
    return db_path


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
