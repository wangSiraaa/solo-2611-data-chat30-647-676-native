"""命令行入口：约束导入/发布、路径查询、锚点分析、范围解释与验证。

用法总览见 ``python -m phaseware.cli --help``。
所有数值以整数/有理数字符串输入，程序内部不使用浮点余数。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .catalog import Catalog, validate_product
from .repository import AnalysisRepository, parse_rational


# ---------------------------------------------------------------------------
# 输出工具
# ---------------------------------------------------------------------------


def _emit(data: Any, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(human)


def _parse_kv(items: list[str], cast=str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"参数 {item!r} 应为 key=value 形式")
        k, v = item.split("=", 1)
        out[k] = cast(v)
    return out


def _fmt_frac(d: dict[str, object]) -> str:
    return str(d["text"])


# ---------------------------------------------------------------------------
# catalog 子命令
# ---------------------------------------------------------------------------


def cmd_catalog_init(args: argparse.Namespace) -> None:
    cat = Catalog(args.db, m=args.m)
    _emit(
        {"db": args.db, "m": cat.m},
        args.json,
        f"已初始化目录 {args.db}，模数 M={cat.m}",
    )
    cat.close()


def cmd_add_node(args: argparse.Namespace) -> None:
    cat = Catalog(args.db)
    cat.add_node(args.node)
    _emit({"node": args.node}, args.json, f"已登记节点 {args.node}")
    cat.close()


def cmd_add_edge(args: argparse.Namespace) -> None:
    cat = Catalog(args.db)
    eid = cat.add_edge(
        args.source, args.u, args.v, args.r, args.lo, args.hi
    )
    cands = cat.edge_candidates(eid)
    _emit(
        {"edge_id": eid, "candidates": list(cands)},
        args.json,
        f"已新增边 #{eid}：{args.u}->{args.v} 来源={args.source} "
        f"r={args.r} 候选差值={list(cands)}（多条边同时成立，互不覆盖）",
    )
    cat.close()


def cmd_retract(args: argparse.Namespace) -> None:
    cat = Catalog(args.db)
    if args.edge_id is not None:
        cat.retract_edge(args.edge_id, args.reason)
        ids = [args.edge_id]
    else:
        ids = cat.retract_source(args.source, args.reason)
    _emit(
        {"retracted_edge_ids": ids, "reason": args.reason},
        args.json,
        f"已撤回边 {ids}（历史保留，active=0），原因：{args.reason}",
    )
    cat.close()


def cmd_correct(args: argparse.Namespace) -> None:
    cat = Catalog(args.db)
    new_id = cat.correct_edge(
        args.edge_id, r=args.r, lo=args.lo, hi=args.hi, reason=args.reason
    )
    cands = cat.edge_candidates(new_id)
    _emit(
        {"old_edge_id": args.edge_id, "new_edge_id": new_id, "candidates": list(cands)},
        args.json,
        f"边 #{args.edge_id} 已更正为 #{new_id}，新候选={list(cands)}；旧边撤回留痕",
    )
    cat.close()


def cmd_candidates(args: argparse.Namespace) -> None:
    cat = Catalog(args.db)
    cands = cat.edge_candidates(args.edge_id)
    rec = cat.get_edge(args.edge_id)
    _emit(
        {
            "edge_id": args.edge_id,
            "source": rec.source,
            "u": rec.u,
            "v": rec.v,
            "r": rec.r,
            "lo": rec.lo,
            "hi": rec.hi,
            "active": rec.active,
            "candidates": list(cands),
        },
        args.json,
        f"边 #{args.edge_id} ({rec.u}->{rec.v}, 来源 {rec.source}, "
        f"active={rec.active}) 候选真实差值：{list(cands)}",
    )
    cat.close()


def cmd_intersect(args: argparse.Namespace) -> None:
    cat = Catalog(args.db)
    info = cat.same_direction_intersection(args.u, args.v)
    flag = "【来源冲突：交集为空，观测均保留不删除】" if info["conflict"] else ""
    _emit(
        info,
        args.json,
        f"同向 {args.u}->{args.v} 边 {info['edge_ids']} 交集="
        f"{info['intersection']} {flag}\n逐边候选：{info['per_edge']}",
    )
    cat.close()


def cmd_path(args: argparse.Namespace) -> None:
    cat = Catalog(args.db)
    if args.nodes:
        info = cat.path_range(args.nodes)
    else:
        info = cat.path_range_by_edge_ids(args.edges)
    _emit(
        info,
        args.json,
        f"路径 {info.get('node_path')} 局部总差候选={info['possible_totals']}\n"
        f"范围 [{info['min']}, {info['max']}]（路径局部松弛，非全图真实范围）",
    )
    cat.close()


def cmd_lint(args: argparse.Namespace) -> None:
    cat = Catalog(args.db)
    info = cat.lint()
    if info["ok"]:
        text = "未发现同向冲突或回路残差问题"
    else:
        text = "发现问题（不自动删除，发布时照实保留）：\n" + "\n".join(
            f"- [{p['kind']}] {p['message']}" for p in info["problems"]
        )
    _emit(info, args.json, text)
    cat.close()


def cmd_publish(args: argparse.Namespace) -> None:
    cat = Catalog(args.db)
    out = cat.publish(note=args.note)
    if args.export:
        cat.export_product(out["product_id"], args.export)
    _emit(
        out,
        args.json,
        f"已发布不可变产品 {out['product_id']}，含 "
        f"{len(out['payload']['nodes'])} 节点 / {len(out['payload']['edges'])} 条边"
        + (f"，导出至 {args.export}" if args.export else ""),
    )
    cat.close()


def cmd_products(args: argparse.Namespace) -> None:
    cat = Catalog(args.db)
    rows = cat.list_products()
    _emit(
        rows,
        args.json,
        "\n".join(f"{r['product_id']}  {r['created_at']}  {r['note']}" for r in rows)
        or "（暂无产品）",
    )
    cat.close()


# ---------------------------------------------------------------------------
# repo 子命令
# ---------------------------------------------------------------------------


def cmd_repo_import(args: argparse.Namespace) -> None:
    repo = AnalysisRepository(args.db)
    with open(args.file, "r", encoding="utf-8") as f:
        doc = json.load(f)
    payload = doc["payload"] if "payload" in doc else doc
    validate_product(payload)
    pid = repo.import_product(payload, product_id=doc.get("product_id"), origin=args.origin)
    _emit({"product_id": pid}, args.json, f"已导入产品 {pid}（来源：{args.origin}）")
    repo.close()


def cmd_repo_products(args: argparse.Namespace) -> None:
    repo = AnalysisRepository(args.db)
    rows = repo.list_products()
    _emit(
        rows,
        args.json,
        "\n".join(f"{r['product_id']}  {r['origin']}  {r['imported_at']}" for r in rows)
        or "（仓库暂无产品）",
    )
    repo.close()


def cmd_analyze(args: argparse.Namespace) -> int:
    """返回退出码：0=CONSISTENT，2=INCONSISTENT（合法分析结果，非用法错误）。"""
    repo = AnalysisRepository(args.db)
    scales = _parse_kv(args.scale, cast=parse_rational)
    anchors = _parse_kv(args.anchor, cast=int)
    report = repo.analyze(args.request_id, args.product_id, scales, args.bound, anchors)
    _emit_report(report, args.json)
    repo.close()
    return 2 if report["status"] == "INCONSISTENT" else 0


def cmd_report(args: argparse.Namespace) -> None:
    repo = AnalysisRepository(args.db)
    report = repo.get_report(args.request_id)
    _emit_report(report, args.json)
    repo.close()


def cmd_explain(args: argparse.Namespace) -> None:
    repo = AnalysisRepository(args.db)
    report = repo.get_report(args.request_id)
    lines: list[str] = []
    lines.append(f"状态：{report['status']}（产品 {_request_product(repo, args.request_id)}）")
    lines.append(
        "重要约定：各节点极值来自同一个全图可行域的独立优化，"
        "不同节点的极值不保证同时达到。"
    )
    if report["status"] == "INCONSISTENT":
        lines.append("不相容约束子集（最小删除核）：")
        for item in report["unsat_core"]["items"]:
            lines.append(f"  - {item['constraint_id']} ({item['kind']})")
        lines.append(report["unsat_core"]["message"])
        lines.append("结构诊断：")
        for p in report["diagnostics"]["problems"]:
            lines.append(f"  - [{p['kind']}] {p['message']}")
        lines.append("反证可重放记录见 JSON 输出的 replay 字段。")
    else:
        je = report["joint_extrema"]
        lines.append(
            f"整体唯一解：{report['unique_solution_overall']}；"
            f"示例解（字典序最小，仅示例）：{report['lexicographically_smallest_solution']}"
        )
        lines.append(
            f"全最小向量同时可行：{je['all_min_jointly_feasible']}；"
            f"全最大向量同时可行：{je['all_max_jointly_feasible']}"
        )
        for comp in report["components"]:
            if not comp["has_anchor"]:
                tf = comp["translation_freedom"]
                lines.append(
                    f"分量 {comp['nodes']} 无锚点，不钉零；"
                    f"平移 +1 可行={tf['shift_plus_one_feasible']}，"
                    f"-1 可行={tf['shift_minus_one_feasible']}"
                )
        for n in report["nodes"]:
            r = report["node_results"][n]
            lines.append(
                f"节点 {n}: 相位∈[{r['phase_min']}, {r['phase_max']}]"
                f"（唯一={r['unique_phase']}）；"
                f"位移∈[{_fmt_frac(r['displacement_min'])}, "
                f"{_fmt_frac(r['displacement_max'])}]"
            )
        if report["diagnostics"]["problems"]:
            lines.append("结构诊断（仍可行时通常为空）：")
            for p in report["diagnostics"]["problems"]:
                lines.append(f"  - [{p['kind']}] {p['message']}")
        cc = report.get("brute_force_cross_check")
        if cc:
            if cc["performed"]:
                lines.append(
                    f"独立全枚举核对：{cc['solution_count']} 个解，"
                    f"通过={cc['passed']}，不一致项={cc['mismatches']}"
                )
            else:
                lines.append(f"独立全枚举核对：跳过（{cc['reason']}）")
    _emit(report, args.json, "\n".join(lines))
    repo.close()


def _request_product(repo: AnalysisRepository, request_id: str) -> str:
    row = repo.conn.execute(
        "SELECT product_id FROM requests WHERE request_id=?", (request_id,)
    ).fetchone()
    return row[0] if row else "?"


def _emit_report(report: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if report["status"] == "INCONSISTENT":
        print("状态：INCONSISTENT")
        print("不相容约束子集：", report["unsat_core"]["constraint_ids"])
        for p in report["diagnostics"]["problems"]:
            print(f"  [{p['kind']}] {p['message']}")
        print("完整反证搜索记录：加 --json 查看 replay 字段")
        return
    print("状态：CONSISTENT")
    print("整体唯一解：", report["unique_solution_overall"])
    print("字典序最小示例解：", report["lexicographically_smallest_solution"])
    for n in report["nodes"]:
        r = report["node_results"][n]
        print(
            f"  {n}: 相位 [{r['phase_min']}, {r['phase_max']}] 唯一={r['unique_phase']} | "
            f"位移 [{_fmt_frac(r['displacement_min'])}, {_fmt_frac(r['displacement_max'])}]"
        )
    cc = report.get("brute_force_cross_check")
    if cc and cc.get("performed"):
        print(f"独立全枚举核对：{cc['solution_count']} 解，通过={cc['passed']}")


# ---------------------------------------------------------------------------
# argparse 装配
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phaseware",
        description="缠绕相位约束目录与位移范围事实仓（全离散合成模型，精确整数/有理数）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="group", required=True)

    cat = sub.add_parser("catalog", help="约束目录（生产侧）")
    csub = cat.add_subparsers(dest="cmd", required=True)

    p = csub.add_parser("init", help="新建目录并固定模数 M")
    p.add_argument("--db", required=True)
    p.add_argument("--m", required=True, type=int)
    p.set_defaults(func=cmd_catalog_init)

    p = csub.add_parser("add-node", help="登记节点")
    p.add_argument("--db", required=True)
    p.add_argument("--node", required=True)
    p.set_defaults(func=cmd_add_node)

    p = csub.add_parser("add-edge", help="新增有向观测边（同节点对可多条并存）")
    p.add_argument("--db", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--u", required=True)
    p.add_argument("--v", required=True)
    p.add_argument("--r", required=True, type=int)
    p.add_argument("--lo", required=True, type=int)
    p.add_argument("--hi", required=True, type=int)
    p.set_defaults(func=cmd_add_edge)

    p = csub.add_parser("retract", help="撤回边（按 edge_id 或来源）")
    p.add_argument("--db", required=True)
    p.add_argument("--edge-id", type=int)
    p.add_argument("--source")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_retract)

    p = csub.add_parser("correct", help="更正边的 r/lo/hi（旧边撤回，新边继承来源）")
    p.add_argument("--db", required=True)
    p.add_argument("--edge-id", required=True, type=int)
    p.add_argument("--r", type=int)
    p.add_argument("--lo", type=int)
    p.add_argument("--hi", type=int)
    p.add_argument("--reason", default="correction")
    p.set_defaults(func=cmd_correct)

    p = csub.add_parser("candidates", help="查询单条边的有限候选差值")
    p.add_argument("--db", required=True)
    p.add_argument("--edge-id", required=True, type=int)
    p.set_defaults(func=cmd_candidates)

    p = csub.add_parser("intersect", help="同向边候选交集（来源冲突解释）")
    p.add_argument("--db", required=True)
    p.add_argument("--u", required=True)
    p.add_argument("--v", required=True)
    p.set_defaults(func=cmd_intersect)

    p = csub.add_parser("path", help="路径总差范围（局部松弛）")
    p.add_argument("--db", required=True)
    p.add_argument("--nodes", nargs="*", help="节点序列，如 a b c")
    p.add_argument("--edges", nargs="*", type=int, help="edge_id 序列（可反向经过）")
    p.set_defaults(func=cmd_path)

    p = csub.add_parser("lint", help="来源冲突与回路残差诊断（不阻断发布）")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_lint)

    p = csub.add_parser("publish", help="固化当前活跃边为不可变产品")
    p.add_argument("--db", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--export", help="同时导出产品 JSON 到该路径")
    p.set_defaults(func=cmd_publish)

    p = csub.add_parser("products", help="列出已发布产品")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_products)

    repo = sub.add_parser("repo", help="位移分析仓（消费侧）")
    rsub = repo.add_subparsers(dest="cmd", required=True)

    p = rsub.add_parser("import", help="导入外部同格式产品（不读目录编辑表）")
    p.add_argument("--db", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--origin", default="external")
    p.set_defaults(func=cmd_repo_import)

    p = rsub.add_parser("products", help="列出仓库内产品")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_repo_products)

    p = rsub.add_parser("analyze", help="锚点分析（固定产品+尺度+界+锚点，冻结报告）")
    p.add_argument("--db", required=True)
    p.add_argument("--request-id", required=True)
    p.add_argument("--product-id", required=True)
    p.add_argument("--bound", required=True, type=int, help="全局整数相位界 B，域为 [-B,B]")
    p.add_argument(
        "--scale", action="append", default=[], help="节点有理比例，如 a=3/2，可重复"
    )
    p.add_argument(
        "--anchor", action="append", default=[], help="锚点，如 a=0（整数），可重复"
    )
    p.set_defaults(func=cmd_analyze)

    p = rsub.add_parser("report", help="查看冻结的分析报告")
    p.add_argument("--db", required=True)
    p.add_argument("--request-id", required=True)
    p.set_defaults(func=cmd_report)

    p = rsub.add_parser("explain", help="范围解释：极值同源声明、平移歧义、冲突与核对")
    p.add_argument("--db", required=True)
    p.add_argument("--request-id", required=True)
    p.set_defaults(func=cmd_explain)

    p = rsub.add_parser("requests", help="列出分析请求")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_requests)

    val = sub.add_parser("validate", help="内置验证套件（临时库，覆盖题面全部场景）")
    val.set_defaults(func=cmd_validate)
    return parser


def cmd_requests(args: argparse.Namespace) -> None:
    repo = AnalysisRepository(args.db)
    rows = repo.list_requests()
    _emit(
        rows,
        args.json,
        "\n".join(
            f"{r['request_id']}  产品 {r['product_id']}  {r['created_at']}" for r in rows
        )
        or "（暂无分析请求）",
    )
    repo.close()


def cmd_validate(args: argparse.Namespace) -> None:
    from .validation import run_suite

    result = run_suite()
    _emit(result, args.json, _format_suite(result))


def _format_suite(result: dict[str, object]) -> str:
    lines = [f"验证套件：{result['passed']}/{result['total']} 通过"]
    for case in result["cases"]:
        mark = "PASS" if case["passed"] else "FAIL"
        lines.append(f"[{mark}] {case['name']} —— {case['detail']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc = args.func(args)
    except (ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
