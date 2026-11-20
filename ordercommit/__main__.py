"""命令行入口：交付报价、接受、计划、收货、分配与解释六类操作。

示例
----
python -m ordercommit demo --out data/run
python -m ordercommit quote --supplier-db data/run/supplier.db \
    --supplier S1 --request-no R1 --item SKU-A=8 --at 2026-11-15T10:00:00
python -m ordercommit accept --supplier-db data/run/supplier.db \
    --quote Q-R1 --commitment C-R1 --at 2026-11-15T10:00:00
python -m ordercommit plan --supplier-db data/run/supplier.db \
    --at 2026-11-15T10:00:00 --execute --warehouse-db data/run/warehouse.db
python -m ordercommit receive --warehouse-db data/run/warehouse.db \
    --receipt R99 --commitment C-MAIN-S1 --item SKU-A=2
python -m ordercommit stock --warehouse-db data/run/warehouse.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .scenario import build_catalog, demand_lines, run_all
from .execution import ProcurementExecutor
from .planner import PurchasePlanner
from .synthetic_data import OVERBUY_CAPS, export_json
from .supplier import SupplierService
from .warehouse import Warehouse


def _parse_item(text: str) -> dict:
    sku, qty = text.split("=", 1)
    return {"sku": sku.strip(), "qty": int(qty)}


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ordercommit",
                                description="供应商报价承诺服务与门店到货分配仓")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="跑全部规定案例并输出 JSON 与 SQLite 库")
    d.add_argument("--out", default="data/run")

    q = sub.add_parser("quote", help="提交数量向量取得不可变报价")
    q.add_argument("--supplier-db", required=True)
    q.add_argument("--supplier", required=True)
    q.add_argument("--request-no", required=True)
    q.add_argument("--item", action="append", required=True,
                   help="SKU=数量，可重复")
    q.add_argument("--at", required=True)
    q.add_argument("--version", default=None)

    a = sub.add_parser("accept", help="接受报价建立供货承诺")
    a.add_argument("--supplier-db", required=True)
    a.add_argument("--quote", required=True)
    a.add_argument("--commitment", required=True)
    a.add_argument("--at", required=True)

    pl = sub.add_parser("plan", help="穷举采购计划；--execute 时落真实报价/承诺")
    pl.add_argument("--supplier-db", required=True)
    pl.add_argument("--warehouse-db", default=None)
    pl.add_argument("--at", required=True)
    pl.add_argument("--execute", action="store_true")
    pl.add_argument("--plan-ref", default="CLI")

    r = sub.add_parser("receive", help="登记到货并自动优先级分配")
    r.add_argument("--warehouse-db", required=True)
    r.add_argument("--receipt", required=True)
    r.add_argument("--commitment", required=True)
    r.add_argument("--item", action="append", required=True)
    r.add_argument("--at", default=None)

    ad = sub.add_parser("add-demand", help="登记门店需求并立即尝试发放")
    ad.add_argument("--warehouse-db", required=True)
    ad.add_argument("--demand-id", required=True)
    ad.add_argument("--sku", required=True)
    ad.add_argument("--qty", type=int, required=True)
    ad.add_argument("--priority", type=int, required=True)
    ad.add_argument("--due", required=True)

    st = sub.add_parser("stock", help="从原始记录重算库存守恒视图")
    st.add_argument("--warehouse-db", required=True)

    ex = sub.add_parser("explain", help="解释入口：quote/receipt/demand/plan")
    ex.add_argument("--warehouse-db", default=None)
    ex.add_argument("--supplier-db", default=None)
    ex.add_argument("--kind", required=True,
                    choices=["receipt", "demand", "plan"])
    ex.add_argument("--key", default=None)
    ex.add_argument("--at", default=None)

    sub.add_parser("export-data", help="把合成输入写到 data/synthetic_input.json")

    au = sub.add_parser("audit", help="裸 SQL 独立稽核库存守恒（不经仓库类）")
    au.add_argument("--warehouse-db", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "demo":
            out = Path(args.out)
            export_json(out / "synthetic_input.json")
            results = run_all(out)
            _print({"ok": results["final_conservation"]["ok"],
                    "scenarios": list(results["scenarios"]),
                    "main_plan_actual_cost": results["main_plan"]["actual_cost"],
                    "output_dir": str(out),
                    "result_file": str(out / "scenario_results.json")})
            return 0

        if args.cmd == "export-data":
            path = export_json("data/synthetic_input.json")
            _print({"exported": str(path)})
            return 0

        if args.cmd == "audit":
            from .audit import audit
            result = audit(args.warehouse_db)
            _print(result)
            return 0 if result["ok"] else 1

        if args.cmd == "quote":
            svc = SupplierService(args.supplier_db)
            _print(svc.quote(args.request_no, args.supplier,
                             [_parse_item(x) for x in args.item], args.at,
                             version_id=args.version))
            svc.close()
            return 0

        if args.cmd == "accept":
            svc = SupplierService(args.supplier_db)
            _print(svc.accept(args.quote, args.commitment, args.at))
            svc.close()
            return 0

        if args.cmd == "plan":
            svc = SupplierService(args.supplier_db)
            catalog = build_catalog(svc, args.at)
            planner = PurchasePlanner(catalog, OVERBUY_CAPS)
            plan = planner.plan(demand_lines())
            payload = planner.explain_plan(plan)
            if args.execute:
                wh = Warehouse(args.warehouse_db) if args.warehouse_db else None
                result = ProcurementExecutor(svc, wh).execute(
                    plan, plan_ref=args.plan_ref, now=args.at)
                payload["execution"] = {
                    "actual_cost": result.actual_cost,
                    "commitments": result.commitment_nos,
                    "cost_matches_estimate": result.cost_matches_estimate,
                }
                if wh:
                    wh.close()
            _print(payload)
            svc.close()
            return 0

        if args.cmd == "receive":
            wh = Warehouse(args.warehouse_db)
            _print(wh.receive(args.receipt, args.commitment,
                              [_parse_item(x) for x in args.item], now=args.at))
            wh.close()
            return 0

        if args.cmd == "add-demand":
            wh = Warehouse(args.warehouse_db)
            _print(wh.add_demand(args.demand_id, args.sku, args.qty,
                                 args.priority, args.due))
            wh.close()
            return 0

        if args.cmd == "stock":
            wh = Warehouse(args.warehouse_db)
            _print({"stock": wh.stock_levels(),
                    "conservation": wh.conservation_check(),
                    "outstanding": wh.outstanding_demands()})
            wh.close()
            return 0

        if args.cmd == "explain":
            if args.kind == "receipt":
                wh = Warehouse(args.warehouse_db)
                _print(wh.explain_receipt(args.key))
                wh.close()
            elif args.kind == "demand":
                wh = Warehouse(args.warehouse_db)
                _print(wh.explain_demand(args.key))
                wh.close()
            else:
                svc = SupplierService(args.supplier_db)
                catalog = build_catalog(svc, args.at)
                planner = PurchasePlanner(catalog, OVERBUY_CAPS)
                plan = planner.plan(demand_lines())
                _print(planner.explain_plan(plan))
                svc.close()
            return 0
    except Exception as exc:  # 业务错误以稳定 JSON 输出，退出码 2
        code = getattr(exc, "code", "UNEXPECTED_ERROR")
        details = getattr(exc, "details", {})
        print(json.dumps({"error": code, "message": str(exc),
                          "details": details}, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
