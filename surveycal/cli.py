"""命令行入口：导入、校准、发布、查询、验证五条调用链 + 合成数据生成。

用法总览
--------
::

  python -m surveycal generate   --out data/
  python -m surveycal import     --db work.db --project demo --sample s.csv --controls c.json
  python -m surveycal import-resp --db work.db --project demo --responses r.csv
  python -m surveycal calibrate  --db work.db --project demo [--diagnostic] [--maxiter N]
  python -m surveycal publish    --db work.db --project demo [--product-id ID] --export wp.json
  python -m surveycal import-product --db work.db --project demo --product wp.json
  python -m surveycal report     --db work.db --project demo --group region --indicators Y01,Y02
  python -m surveycal query      --db work.db --project demo (products|reports|report ID|trace ID|cells ID)
  python -m surveycal verify     --db work.db --project demo --product-id ID
"""

from __future__ import annotations

import argparse
import json
import sys

from . import calibration as calib_mod
from . import generate as gen_mod
from . import ingest, statistics
from .errors import InfeasibleError, SurveyCalError, UndecidedError
from .models import CalibStatus, ProductKind
from .repository import Repository


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _load_inputs(repo: Repository, project: str, revision: int | None = None):
    sample = repo.load_sample(project)
    units, membership, rev = repo.load_controls(project, sample, revision)
    return sample, units, membership, rev


def cmd_generate(args) -> int:
    paths = gen_mod.generate_all(args.out)
    _print({"generated": paths})
    return 0


def cmd_import(args) -> int:
    sample = ingest.load_sample_csv(args.sample, args.controls)
    spec, units = ingest.load_controls_json(args.controls)
    membership = ingest.resolve_membership(sample, units)
    with Repository(args.db) as repo:
        # 控制更正：同一项目再次导入时控制数版本号自动 +1，旧版本保留
        prev_rev = repo.get_meta(f"{args.project}:control_revision")
        revision = int(prev_rev or 0) + 1
        repo.set_meta(f"{args.project}:sample_sha", sample.source_sha256)
        repo.save_sample(args.project, sample)
        repo.save_controls(args.project, units, membership, sample, revision=revision)
    _print(
        {
            "project": args.project,
            "control_revision": revision,
            "n_persons": len(sample.persons),
            "n_controls": len(units),
            "design_fields": {k: list(v) for k, v in spec.fields.items()},
            "global_bounds": [spec.global_lo, spec.global_hi],
            "membership": {c: sorted(idx) for c, idx in membership.items()},
        }
    )
    return 0


def cmd_import_responses(args) -> int:
    with Repository(args.db) as repo:
        sample = repo.load_sample(args.project)
        batches = ingest.load_responses_csv(args.responses, sample)
        repo.save_responses(args.project, batches)
    _print(
        {
            "imported_indicators": sorted(batches),
            "detail": {
                ind: {
                    "valid": len(b.values),
                    "missing": len(b.missing_ids),
                    "missing_ids": list(b.missing_ids),
                }
                for ind, b in sorted(batches.items())
            },
        }
    )
    return 0


def cmd_calibrate(args) -> int:
    with Repository(args.db) as repo:
        sample, units, membership, rev = _load_inputs(repo, args.project, args.revision)
        result = calib_mod.calibrate(
            sample,
            units,
            membership,
            maxiter=args.maxiter,
            emit_diagnostic=args.diagnostic,
        )
        payload = {
            "project": args.project,
            "control_revision": rev,
            "status": result.status.value,
            "solver": result.solver,
            "representation": result.representation,
            "iterations": result.iterations,
            "tol": result.tol,
        }
        if result.status == CalibStatus.PUBLISHABLE:
            verify = result.diagnostics["verify"]
            payload["objective"] = result.objective
            payload["worst_residual_abs"] = verify["worst_residual_abs"]
            payload["worst_bound_violation"] = verify["worst_bound_violation"]
            payload["relative_dual_gap"] = verify["relative_dual_gap"]
            payload["residuals"] = list(result.residuals)
            payload["weight_ratio_range"] = [
                min(w / p.base_weight for w, p in zip(result.weights, sample.persons)),
                max(w / p.base_weight for w, p in zip(result.weights, sample.persons)),
            ]
            if args.diagnostic:
                payload["note"] = "正式解已通过独立复核，无需诊断产品"
        elif result.status == CalibStatus.INFEASIBLE:
            payload["infeasible_reason"] = result.diagnostics.get("reason")
            payload["evidence"] = result.diagnostics.get("evidence")
            payload["related_constraint"] = result.diagnostics.get("related_constraint")
            bmr = result.diagnostics.get("bounded_min_residual")
            if bmr is not None:
                payload["bounded_worst_residual"] = max(abs(x["residual"]) for x in bmr)
            if args.diagnostic and "diagnostic_weights" in result.diagnostics:
                import numpy as np

                # 诊断结果单独保存并显式打标，绝不冒充正式权重
                dw = np.array(result.diagnostics["diagnostic_weights"])
                residuals = [x["residual"] for x in bmr]
                objective = calib_mod._primal_objective(dw, sample.base_weights)
                diag_result = _diagnostic_result(result, residuals, objective)
                product = calib_mod.build_product(
                    sample,
                    units,
                    membership,
                    diag_result,
                    created_from={
                        "project": args.project,
                        "control_revision": rev,
                        "sample_sha256": sample.source_sha256,
                        "mode": "diagnostic_relaxation",
                    },
                    note="诊断性放宽（最小残差）结果，禁止作为正式权重使用",
                )
                repo.save_product(args.project, product, CalibStatus.DIAGNOSTIC.value)
                payload["diagnostic_product_id"] = product.product_id
        else:
            payload["undecided_reason"] = result.diagnostics.get("reason")
            payload["message"] = "数值求解未能判定，单独报告 UNDECIDED，不当作数学不可行"
            bmr = result.diagnostics.get("bounded_min_residual")
            if args.diagnostic and "diagnostic_weights" in result.diagnostics:
                import numpy as np

                dw = np.array(result.diagnostics["diagnostic_weights"])
                residuals = [x["residual"] for x in bmr] if bmr is not None else []
                objective = calib_mod._primal_objective(dw, sample.base_weights)
                diag_result = _diagnostic_result(result, residuals, objective)
                product = calib_mod.build_product(
                    sample,
                    units,
                    membership,
                    diag_result,
                    created_from={
                        "project": args.project,
                        "control_revision": rev,
                        "sample_sha256": sample.source_sha256,
                        "mode": "diagnostic_undecided_feasible_candidate",
                    },
                    note="未决案例的可行候选点，非最优性确认，禁止作为正式权重",
                )
                repo.save_product(args.project, product, CalibStatus.DIAGNOSTIC.value)
                payload["diagnostic_product_id"] = product.product_id
        repo.set_meta(f"{args.project}:last_calibration", payload)
    _print(payload)
    return 0


def _diagnostic_result(result, residuals, objective):
    """把诊断权重包装成 DIAGNOSTIC 状态的结果对象（与正式产品类型隔离）。"""
    from .models import CalibrationResult

    dw = result.diagnostics["diagnostic_weights"]
    return CalibrationResult(
        status=CalibStatus.DIAGNOSTIC,
        weights=dw,
        residuals=tuple(float(x) for x in residuals),  # 真实残差，独立重算
        objective=float(objective),
        diagnostics={"origin_status": result.status.value, **result.diagnostics},
        tol=result.tol,
        iterations=result.iterations,
        solver=result.solver,
        representation=result.representation,
    )


def cmd_publish(args) -> int:
    with Repository(args.db) as repo:
        sample, units, membership, rev = _load_inputs(repo, args.project, args.revision)
        result = calib_mod.calibrate(sample, units, membership, maxiter=args.maxiter)
        # 发布守卫：不可行/未决在此显式失败，不静默丢约束、不补造人、不放宽边界
        calib_mod.require_publishable(result)
        product = calib_mod.build_product(
            sample,
            units,
            membership,
            result,
            created_from={
                "project": args.project,
                "control_revision": rev,
                "sample_sha256": sample.source_sha256,
                "mode": "official_calibration",
            },
        )
        # 独立复核是发布的强制前置条件（求解器成功不构成发布理由）
        verify = calib_mod.verify_product(product)
        if not verify["passed"]:
            raise SurveyCalError(f"独立复核未通过，拒绝发布: {verify}")
        repo.save_product(args.project, product, CalibStatus.PUBLISHABLE.value)
        if args.export:
            with open(args.export, "w", encoding="utf-8") as fh:
                json.dump(product.to_export_dict(), fh, ensure_ascii=False, indent=2)
    _print(
        {
            "product_id": product.product_id,
            "kind": product.kind.value,
            "n_persons": len(product.sample_ids),
            "n_controls": len(product.controls),
            "objective": product.objective,
            "verify": {k: v for k, v in verify.items() if k != "per_unit"},
            "exported_to": args.export,
        }
    )
    return 0


def cmd_import_product(args) -> int:
    product = ingest.load_external_product(args.product)
    verify = calib_mod.verify_product(product)
    if not verify["passed"]:
        _print({"rejected": True, "product_id": product.product_id, "verify": verify})
        return 2
    with Repository(args.db) as repo:
        repo.save_product(args.project, product, product.kind.value)
    _print({"imported": product.product_id, "kind": product.kind.value, "verify_summary": {
        k: v for k, v in verify.items() if k != "per_unit"
    }})
    return 0


def cmd_report(args) -> int:
    with Repository(args.db) as repo:
        product = repo.load_product(args.product_id)
        if product.kind != ProductKind.OFFICIAL:
            raise SurveyCalError(f"产品 {product.product_id} 为 {product.kind.value}，不得用于正式报告")
        batches = repo.load_responses(args.project)
        indicators = args.indicators.split(",") if args.indicators else None
        report = statistics.publish_report(product, batches, args.group, indicators)
        repo.save_report(args.project, report)
    _print(
        {
            "report_id": report.report_id,
            "product_id": report.product_id,
            "group_field": report.group_field,
            "n_cells": len(report.cells),
            "cells": [c.__dict__ for c in report.cells],
        }
    )
    return 0


def cmd_query(args) -> int:
    with Repository(args.db) as repo:
        if args.what == "products":
            out = repo.list_products(args.project)
        elif args.what == "reports":
            out = repo.list_reports(args.project)
        elif args.what == "report":
            out = repo.report_detail(args.ref)
        elif args.what == "trace":
            out = repo.trace_product(args.ref)
        elif args.what == "cells":
            out = repo.load_report_cells(args.ref)
        else:  # meta
            out = repo.get_meta(args.ref)
    _print(out)
    return 0


def cmd_verify(args) -> int:
    with Repository(args.db) as repo:
        product = repo.load_product(args.product_id)
        verify = calib_mod.verify_product(product)
    _print(verify)
    return 0 if verify["passed"] else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="surveycal", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("generate", help="生成全部合成案例")
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("import", help="登记样本 CSV 与控制数 JSON")
    sp.add_argument("--db", required=True)
    sp.add_argument("--project", required=True)
    sp.add_argument("--sample", required=True)
    sp.add_argument("--controls", required=True)
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("import-resp", help="登记回答 CSV（缺失留空，零值写 0）")
    sp.add_argument("--db", required=True)
    sp.add_argument("--project", required=True)
    sp.add_argument("--responses", required=True)
    sp.set_defaults(func=cmd_import_responses)

    sp = sub.add_parser("calibrate", help="执行校准并三态报告（不发布）")
    sp.add_argument("--db", required=True)
    sp.add_argument("--project", required=True)
    sp.add_argument("--revision", type=int, default=None)
    sp.add_argument("--maxiter", type=int, default=1000)
    sp.add_argument("--diagnostic", action="store_true", help="附加诊断性放宽产品（显式标记）")
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("publish", help="独立复核通过后固化正式权重产品")
    sp.add_argument("--db", required=True)
    sp.add_argument("--project", required=True)
    sp.add_argument("--revision", type=int, default=None)
    sp.add_argument("--maxiter", type=int, default=1000)
    sp.add_argument("--export", default=None, help="同时导出外部产品 JSON")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("import-product", help="消费外部权重产品（独立验证后入库）")
    sp.add_argument("--db", required=True)
    sp.add_argument("--project", required=True)
    sp.add_argument("--product", required=True)
    sp.set_defaults(func=cmd_import_product)

    sp = sub.add_parser("report", help="发布分组统计报告")
    sp.add_argument("--db", required=True)
    sp.add_argument("--project", required=True)
    sp.add_argument("--product-id", required=True)
    sp.add_argument("--group", required=True, help="如 region / age_group / region||age_group")
    sp.add_argument("--indicators", default=None)
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("query", help="查询产品、报告、单元或权重追溯")
    sp.add_argument("--db", required=True)
    sp.add_argument("--project", required=True)
    sp.add_argument("what", choices=["products", "reports", "report", "trace", "cells", "meta"])
    sp.add_argument("ref", nargs="?", default=None)
    sp.set_defaults(func=cmd_query)

    sp = sub.add_parser("verify", help="对库内/外部产品做独立复核")
    sp.add_argument("--db", required=True)
    sp.add_argument("--project", required=True)
    sp.add_argument("--product-id", required=True)
    sp.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (InfeasibleError, UndecidedError) as exc:
        _print(
            {
                "error_type": type(exc).__name__,
                "reason": getattr(exc, "reason", None),
                "message": str(exc),
                "diagnostics": getattr(exc, "diagnostics", {}),
            }
        )
        return 3
    except SurveyCalError as exc:
        _print({"error_type": type(exc).__name__, "message": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
