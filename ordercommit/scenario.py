"""端到端场景编排与六个规定案例。

run_all() 会在指定目录建立两套独立 SQLite 库（报价服务 / 分配仓），
跑完全部案例并把结构化结果写成 JSON，同时把合成输入一并落盘。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import (
    DuplicateRequest,
    InfeasiblePlan,
    OverCapacity,
    QuoteExpired,
    ReceiptAlreadyProcessed,
    RuleViolation,
    StateConflict,
)
from .execution import ProcurementExecutor
from .planner import DemandLine, PurchasePlanner, SupplierCatalogEntry, TRADEOFF_NOTES
from .pricing import SkuRule
from .supplier import SupplierService
from .synthetic_data import (
    OVERBUY_CAPS,
    T_EARLY,
    T_EXPIRED,
    T_JUST_AFTER_EARLY,
    T_MAIN,
    WINDOW_MAIN,
    build_demands,
    build_versions,
)
from .warehouse import Warehouse


# ---------------------------------------------------------------- 装配
def build_supplier_service(db_path: str | Path) -> SupplierService:
    svc = SupplierService(str(db_path))
    for vid, spec in build_versions().items():
        svc.register_rule_version(
            version_id=vid,
            supplier_id=spec["supplier_id"],
            effective_from=spec["window"][0],
            effective_to=spec["window"][1],
            sku_rules=spec["sku_rules"],
            now=spec["window"][0],
        )
    return svc


def build_catalog(svc: SupplierService, at: str) -> list[SupplierCatalogEntry]:
    """从报价服务的原始规则与已接受额度构建计划时刻的不可变目录快照。"""
    catalog: list[SupplierCatalogEntry] = []
    for supplier_id in ("S1", "S2", "S3"):
        version_id = svc.active_version(supplier_id, at)
        rules = svc.load_rules(version_id)
        remaining = {sku: svc.remaining_capacity(version_id, sku) for sku in rules}
        catalog.append(SupplierCatalogEntry(
            supplier_id=supplier_id, version_id=version_id,
            rules=rules, remaining=remaining))
    return catalog


def demand_lines() -> list[DemandLine]:
    return [DemandLine(d.demand_id, d.sku, d.qty, d.priority, d.due_date)
            for d in build_demands()]


# ---------------------------------------------------------------- 案例 1：多买反而便宜 + 请求幂等
def scenario_overbuy_cheaper(svc: SupplierService) -> dict[str, Any]:
    q8 = svc.quote("REQ-OB-8", "S1", [{"sku": "SKU-A", "qty": 8}], T_MAIN)
    q10 = svc.quote("REQ-OB-10", "S1", [{"sku": "SKU-A", "qty": 10}], T_MAIN)
    replay = svc.quote("REQ-OB-8", "S1", [{"sku": "SKU-A", "qty": 8}], T_MAIN)
    diff_text: dict[str, Any]
    try:
        svc.quote("REQ-OB-8", "S1", [{"sku": "SKU-A", "qty": 6}], T_MAIN)
        diff_text = {"rejected": False}
    except DuplicateRequest as exc:
        diff_text = {"rejected": True, "code": exc.code, "message": exc.message}
    return {
        "case": "多买反而便宜（全量阶梯价）+ 同号复读/异文拒绝",
        "buy_8": {"qty": 8, "total_price": q8["total_price"],
                  "unit_price": q8["line_results"][0]["unit_price"]},
        "buy_10": {"qty": 10, "total_price": q10["total_price"],
                   "unit_price": q10["line_results"][0]["unit_price"]},
        "cheaper_by_buying_more": q10["total_price"] < q8["total_price"],
        "replay_same_quote_no": replay["quote_no"] == q8["quote_no"],
        "same_no_diff_body_rejected": diff_text,
    }


# ---------------------------------------------------------------- 案例 2：包装使需求不能恰好满足
def scenario_packaging(svc: SupplierService) -> dict[str, Any]:
    rules = svc.load_rules("V-S1-MAIN")
    feasible_for_need4 = rules["SKU-B"].feasible_quantities(limit=10)
    exact_possible = 4 in feasible_for_need4
    # 全量穷举计划：SKU-B 需求合计 8，超买上限 4，包装 5/3/4 下必须超买凑整
    catalog = build_catalog(svc, T_MAIN)
    planner = PurchasePlanner(catalog, OVERBUY_CAPS)
    allocs = planner.sku_allocations("SKU-B", 8, OVERBUY_CAPS["SKU-B"])
    plan = planner.plan(demand_lines())
    b_lines = [ln for ln in planner.explain_plan(plan)["lines"] if ln["sku"] == "SKU-B"]
    return {
        "case": "包装倍数使需求无法恰好满足，穷举选择最省的凑整组合",
        "single_demand_qty": 4,
        "s1_pack5_feasible_up_to_10": feasible_for_need4,
        "exact_4_possible_at_s1": exact_possible,
        "needed": 8,
        "enumerated_allocation_count_for_sku_b": len(allocs),
        "chosen_sku_b_plan": b_lines[0],
        "overbuy_b": plan.overbuy["SKU-B"],
        "forced_overbuy": plan.overbuy["SKU-B"] > 0,
    }


# ---------------------------------------------------------------- 案例 3：低价过期
def scenario_expired_quote(svc: SupplierService) -> dict[str, Any]:
    # 早鸟窗口内取得低价报价，并在窗口内正常接受一份（边界可用性对照）
    cheap = svc.quote("REQ-EXP-C", "S3",
                      [{"sku": "SKU-C", "qty": 10}], T_EARLY)
    ok_quote = svc.quote("REQ-EXP-OK", "S3",
                         [{"sku": "SKU-C", "qty": 4}], T_EARLY)
    ok_accept = svc.accept(ok_quote["quote_no"], "C-EXP-OK", T_EARLY)

    expired_accept: dict[str, Any]
    try:
        svc.accept(cheap["quote_no"], "C-EXP-SHOULD-FAIL", T_EXPIRED)
        expired_accept = {"rejected": False}
    except QuoteExpired as exc:
        expired_accept = {"rejected": True, "code": exc.code, "message": exc.message}
    # 过期接受不产生承诺，报价仍停在 OPEN
    commitments_after = [c["commitment_no"] for c in svc.list_commitments()
                         if c["commitment_no"] == "C-EXP-SHOULD-FAIL"]

    # early 已结束：同一供应商的活动版本切到 main，重新报价价格回升
    switched = svc.active_version("S3", T_JUST_AFTER_EARLY)
    late = svc.quote("REQ-LATE-C", "S3",
                     [{"sku": "SKU-C", "qty": 10}], T_JUST_AFTER_EARLY)
    return {
        "case": "低价规则版本到期：过期接受不产生承诺，重新报价价格回升",
        "early_quote": {"quote_no": cheap["quote_no"],
                        "version": cheap["version_id"],
                        "total_price": cheap["total_price"],
                        "status_after_expired_accept":
                            svc.get_quote(cheap["quote_no"])["status"]},
        "in_window_accept_works": ok_accept["commitment_no"] == "C-EXP-OK",
        "expired_accept": expired_accept,
        "no_commitment_created": commitments_after == [],
        "active_version_switched_to": switched,
        "main_window_quote": {"quote_no": late["quote_no"],
                              "version": late["version_id"],
                              "total_price": late["total_price"]},
        "price_increased_after_expiry":
            late["total_price"] > cheap["total_price"],
    }


# ---------------------------------------------------------------- 主计划 + 案例 4：两门店争短缺
def execute_main_plan(svc: SupplierService, wh: Warehouse) -> dict[str, Any]:
    catalog = build_catalog(svc, T_MAIN)
    planner = PurchasePlanner(catalog, OVERBUY_CAPS)
    plan = planner.plan(demand_lines())
    executor = ProcurementExecutor(svc, wh)
    result = executor.execute(plan, plan_ref="MAIN", now=T_MAIN)
    return {"planner": planner, "plan": plan, "result": result}


def scenario_shortage(svc: SupplierService, wh: Warehouse,
                      main: dict[str, Any]) -> dict[str, Any]:
    """SKU-D 承诺 16 件但只到货 8 件，两个门店按优先级争抢。"""
    # 登记全部 20 条需求
    for d in build_demands():
        wh.add_demand(d.demand_id, d.sku, d.qty, d.priority, d.due_date, now=T_MAIN)

    # 按承诺构造到货：除 SKU-D 足额外，D 只送 8/16
    commitments = main["result"].commitments
    receipts: list[dict[str, Any]] = []
    seq = 0
    d_delivered = 0
    d_budget = 8
    for c in commitments:
        lines: list[dict[str, Any]] = []
        for ln in c["lines"]:
            if ln["qty"] <= 0:
                continue
            if ln["sku"] == "SKU-D":
                q = min(ln["qty"], max(0, d_budget - d_delivered))
                if q <= 0:
                    continue
                d_delivered += q
            else:
                q = ln["qty"]
            lines.append({"sku": ln["sku"], "qty": q})
        if not lines:
            continue
        seq += 1
        rid = f"R-{seq:02d}"
        receipts.append(wh.receive(rid, c["commitment_no"], lines, now=T_MAIN))

    d_demands = [d for d in wh.list_demands() if d["sku"] == "SKU-D"]
    outstanding = wh.outstanding_demands()
    return {
        "case": "到货不足：两门店争 SKU-D，按优先级/到期日/需求ID分配，余额入中央库存",
        "committed_d": 16,
        "delivered_d": d_delivered,
        "receipts": [{"receipt_id": r["receipt_id"],
                      "leftover": r["leftover"]} for r in receipts],
        "d_demand_status": [
            {"demand_id": d["demand_id"], "qty": d["qty"],
             "allocated": d["allocated_qty"], "status": d["status"],
             "priority": d["priority"], "due_date": d["due_date"]}
            for d in d_demands],
        "outstanding_after_shortage": outstanding,
        "central_stock": wh.stock_levels(),
    }


# ---------------------------------------------------------------- 案例 5：到货重送 / 超承诺收货
def scenario_receipt_replay(wh: Warehouse, svc: SupplierService,
                            main: dict[str, Any]) -> dict[str, Any]:
    # 用一笔独立的小承诺验证到货身份与超承诺约束，避免干扰主流程库存
    quote = svc.quote("REQ-REPLAY-E", "S1",
                      [{"sku": "SKU-E", "qty": 2}], T_MAIN)
    svc.accept(quote["quote_no"], "C-REPLAY", T_MAIN)
    wh.register_commitment(
        commitment_no="C-REPLAY", supplier_id="S1",
        version_id=quote["version_id"],
        lines=[{"sku": "SKU-E", "qty": 2,
                "unit_price": quote["line_results"][0]["unit_price"]}],
        now=T_MAIN)

    rid = "R-REPLAY"
    first = wh.receive(rid, "C-REPLAY",
                       [{"sku": "SKU-E", "qty": 1}], now=T_MAIN)
    replay: dict[str, Any]
    try:
        wh.receive(rid, "C-REPLAY",
                   [{"sku": "SKU-E", "qty": 1}], now=T_MAIN)
        replay = {"rejected": False}
    except ReceiptAlreadyProcessed as exc:
        replay = {"rejected": True, "code": exc.code, "message": exc.message}

    # 超承诺数量到货：承诺 2、已到 1，再到 2 → 累计 3 > 2
    over: dict[str, Any]
    try:
        wh.receive("R-OVER", "C-REPLAY",
                   [{"sku": "SKU-E", "qty": 2}], now=T_MAIN)
        over = {"rejected": False}
    except RuleViolation as exc:
        over = {"rejected": True, "code": exc.code, "message": exc.message,
                "attempted_qty": 2}

    # 把剩余合法 1 件收齐，验证同承诺多到货身份可累计、但不超量
    final = wh.receive("R-REPLAY-REST", "C-REPLAY",
                       [{"sku": "SKU-E", "qty": 1}], now=T_MAIN)
    return {
        "case": "一个到货身份只能分配一次；累计到货不得超过承诺数量",
        "first_receipt": first["receipt_id"],
        "duplicate_receipt_rejected": replay,
        "over_commitment_receipt_rejected": over,
        "valid_second_receipt": final["receipt_id"],
        "commitment_total_received":
            wh.get_commitment("C-REPLAY")["lines"][0]["received_qty"],
    }


# ---------------------------------------------------------------- 案例 6：库存守恒独立稽核 + 更正/取消
def scenario_conservation_and_correction(wh: Warehouse) -> dict[str, Any]:
    before = wh.conservation_check()
    # 短缺 SKU-D 上必然存在未满足需求：上调数量（无新库存则维持缺口，实物一笔不动）
    target = next(d for d in wh.list_demands()
                  if d["sku"] == "SKU-D" and d["status"] in ("OPEN", "PARTIAL"))
    corrected = wh.correct_demand(target["demand_id"], target["qty"] + 2,
                                  note="门店追加 2 件", now=T_MAIN)
    # 不能把数量改到低于已发量
    shrink: dict[str, Any]
    try:
        wh.correct_demand(target["demand_id"],
                          max(0, corrected["demand"]["allocated_qty"] - 1),
                          now=T_MAIN)
        shrink = {"rejected": False}
    except (RuleViolation, StateConflict) as exc:
        shrink = {"rejected": True, "code": exc.code, "message": exc.message}
    # 取消另一个未完成需求：已发数量保留、未发实物留给其他需求
    cancel_target = next(d for d in wh.list_demands()
                         if d["status"] in ("OPEN", "PARTIAL")
                         and d["demand_id"] != target["demand_id"])
    cancelled = wh.cancel_demand(cancel_target["demand_id"],
                                 note="门店撤单", now=T_MAIN)
    after = wh.conservation_check()
    history = wh.allocation_history()
    return {
        "case": "更正重排未发库存、取消不退回实物；从原始记录独立核库存守恒",
        "conservation_before": before,
        "correction": {"demand_id": target["demand_id"],
                       "new_qty": corrected["demand"]["qty"],
                       "allocated_kept": corrected["demand"]["allocated_qty"]},
        "shrink_below_allocated_rejected": shrink,
        "cancel": {"demand_id": cancel_target["demand_id"],
                   "status": cancelled["demand"]["status"],
                   "allocated_kept": cancelled["demand"]["allocated_qty"]},
        "conservation_after": after,
        "allocations_append_only_count": len(history),
        "physical_stock_never_returns_to_supplier": after["ok"],
    }


# ---------------------------------------------------------------- 无可行计划缺口
def scenario_infeasible_gap(svc: SupplierService) -> dict[str, Any]:
    """把 SKU-D 超买上限收紧为 0 且构造超过总可供的需求，验证明确缺口回报。"""
    catalog = build_catalog(svc, T_MAIN)
    tight_caps = dict(OVERBUY_CAPS)
    tight_caps["SKU-D"] = 0
    planner = PurchasePlanner(catalog, tight_caps)
    demands = demand_lines() + [
        DemandLine("D-EXTRA-1", "SKU-D", 10, 1, "2026-12-05"),
        DemandLine("D-EXTRA-2", "SKU-D", 10, 1, "2026-12-06"),
    ]
    out: dict[str, Any]
    try:
        planner.plan(demands)
        out = {"infeasible_reported": False}
    except InfeasiblePlan as exc:
        out = {"infeasible_reported": True, "code": exc.code,
               "message": exc.message, "gaps": exc.details["gaps"]}
    return {"case": "无可行计划：明确逐 SKU 缺口，不拼造承诺", **out}


# ---------------------------------------------------------------- 额度超卖
def scenario_capacity() -> dict[str, Any]:
    """同版本累计接受量不得超可供上限；接受时重新核额度。

    使用独立的内存服务，避免与主流程额度互相干扰。
    """
    fresh = SupplierService(":memory:")
    fresh.register_rule_version(
        "V-CAP", "SX", WINDOW_MAIN[0], WINDOW_MAIN[1],
        {"SKU-X": SkuRule(pack_multiple=2, min_qty=0, capacity=10,
                          tiers=[(0, 5), (8, 4)])},
        now=WINDOW_MAIN[0])
    cap = fresh.load_rules("V-CAP")["SKU-X"].capacity
    first = fresh.quote("REQ-CAP-1", "SX",
                        [{"sku": "SKU-X", "qty": cap}], T_MAIN)
    fresh.accept(first["quote_no"], "C-CAP-1", T_MAIN)
    # 第二张报价在规则本身仍合法（2 件满足包装/上限），但接受时额度已不够
    second = fresh.quote("REQ-CAP-2", "SX",
                         [{"sku": "SKU-X", "qty": 2}], T_MAIN)
    out: dict[str, Any]
    try:
        fresh.accept(second["quote_no"], "C-CAP-2", T_MAIN)
        out = {"oversold": True}
    except OverCapacity as exc:
        out = {"oversold": False, "code": exc.code, "message": exc.message,
               "details": exc.details}
    remaining = fresh.remaining_capacity("V-CAP", "SKU-X")
    fresh.close()
    return {"case": "凭旧报价也不能超卖：接受时重核同版本剩余额度",
            "capacity": cap, "result": out, "remaining_after": remaining}


# ---------------------------------------------------------------- 总入口
def run_all(out_dir: str | Path) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    supplier_db = out / "supplier.db"
    warehouse_db = out / "warehouse.db"
    for p in (supplier_db, warehouse_db):
        if p.exists():
            p.unlink()

    svc = build_supplier_service(supplier_db)
    wh = Warehouse(str(warehouse_db))

    results: dict[str, Any] = {"scenarios": {}}
    results["scenarios"]["1_overbuy_cheaper_and_idempotency"] = \
        scenario_overbuy_cheaper(svc)
    results["scenarios"]["2_packaging"] = scenario_packaging(svc)
    results["scenarios"]["3_expired_quote"] = scenario_expired_quote(svc)
    main = execute_main_plan(svc, wh)
    plan = main["plan"]
    results["main_plan"] = {
        "estimated_cost": plan.estimated_cost,
        "actual_cost": main["result"].actual_cost,
        "cost_matches_estimate": main["result"].cost_matches_estimate,
        "overbuy": plan.overbuy,
        "quantities": {f"{k[0]}|{k[1]}": v for k, v in sorted(plan.quantities.items())},
        "commitment_nos": main["result"].commitments and [
            c["commitment_no"] for c in main["result"].commitments],
        "explanation": main["planner"].explain_plan(plan),
        "tradeoffs": TRADEOFF_NOTES,
    }
    results["scenarios"]["4_shortage_two_stores"] = scenario_shortage(svc, wh, main)
    results["scenarios"]["5_receipt_replay"] = scenario_receipt_replay(wh, svc, main)
    results["scenarios"]["6_conservation_correction_cancel"] = \
        scenario_conservation_and_correction(wh)
    results["scenarios"]["7_infeasible_gap"] = scenario_infeasible_gap(svc)
    results["scenarios"]["8_capacity_no_oversell"] = scenario_capacity()
    results["final_conservation"] = wh.conservation_check()
    results["final_stock_levels"] = wh.stock_levels()
    # 完全独立的裸 SQL 稽核（不经过 Warehouse 类方法）
    from .audit import audit as independent_audit
    results["independent_sql_audit"] = independent_audit(str(warehouse_db))

    (out / "scenario_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    svc.close()
    wh.close()
    return results
