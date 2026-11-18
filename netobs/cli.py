"""命令行入口：generate / check / compile / classify / load。

示例::

    python3 -m netobs.cli generate --root . --packets 100000
    python3 -m netobs.cli check --rules data/rules_base.json
    python3 -m netobs.cli compile --rules data/rules_base.json --out artifacts/cells.json
    python3 -m netobs.cli classify --rules data/rules_base.json \
        --src 203.0.113.7 --dst 198.51.100.7 --proto TCP --sport 9000 --dport 443
    python3 -m netobs.cli load --rules data/rules_base.json \
        --packets data/packets.jsonl --out artifacts/facts_manual.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compile import CompilationResourceError, compile_rules, equivalence_report
from .demo import run
from .generate import build_all
from .rules import (
    RuleValidationError,
    load_ruleset,
    parse_ipv4,
)
from .shadow import RuleCatalog
from .traffic import TrafficWarehouse, read_packets, save_analysis


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netobs", description="有序双端地址规则产品与流量事实仓")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="生成合成规则与流量")
    p_gen.add_argument("--root", default=".")
    p_gen.add_argument("--packets", type=int, default=100_000)

    p_check = sub.add_parser("check", help="逐规则遮蔽分析（不依赖流量）")
    p_check.add_argument("--rules", required=True)
    p_check.add_argument("--rule-id", action="append", default=None)
    p_check.add_argument("--out", default=None)

    p_comp = sub.add_parser("compile", help="编译为无重叠决策单元并给出精确等价报告")
    p_comp.add_argument("--rules", required=True)
    p_comp.add_argument("--out", required=True)
    p_comp.add_argument("--max-cells", type=int, default=200_000)

    p_cls = sub.add_parser("classify", help="对单个五元组做首匹配分类")
    p_cls.add_argument("--rules", required=True)
    p_cls.add_argument("--src", required=True)
    p_cls.add_argument("--dst", required=True)
    p_cls.add_argument("--proto", required=True, choices=["TCP", "UDP"])
    p_cls.add_argument("--sport", required=True, type=int)
    p_cls.add_argument("--dport", required=True, type=int)

    p_load = sub.add_parser("load", help="装载流量并生成事实仓 + 聚合")
    p_load.add_argument("--rules", required=True)
    p_load.add_argument("--packets", required=True)
    p_load.add_argument("--out", required=True)
    p_load.add_argument("--window", type=int, default=60)

    p_demo = sub.add_parser("demo", help="全流程演示与核对")
    p_demo.add_argument("--root", default=".")
    p_demo.add_argument("--packets", type=int, default=100_000)
    p_demo.add_argument("--window", type=int, default=60)
    p_demo.add_argument("--compile-budget", type=int, default=200_000)
    p_demo.add_argument("--skip-generate", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            summary = build_all(args.root, packet_count=args.packets)
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        elif args.command == "check":
            ruleset = load_ruleset(args.rules)
            catalog = RuleCatalog(ruleset)
            ids = args.rule_id or [rule.rule_id for rule in ruleset.rules]
            payload = {
                "ruleset_digest": ruleset.digest,
                "verdicts": [],
            }
            for rid in ids:
                verdict = catalog.analyze(rid)
                entry = verdict.to_dict()
                entry["verification"] = catalog.verify_verdict(verdict)
                payload["verdicts"].append(entry)
            if args.out:
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        elif args.command == "compile":
            ruleset = load_ruleset(args.rules)
            compiled = compile_rules(ruleset, max_cells=args.max_cells)
            report = equivalence_report(ruleset, compiled)
            payload = compiled.to_dict()
            payload["equivalence"] = report
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(json.dumps({
                "cells": len(compiled.cells),
                "equivalent": report["equivalent"],
                "issues": report["issues"],
            }, indent=2, ensure_ascii=False))
        elif args.command == "classify":
            ruleset = load_ruleset(args.rules)
            rule_id, action, category, index = ruleset.classify(
                parse_ipv4(args.src),
                parse_ipv4(args.dst),
                args.proto,
                args.sport,
                args.dport,
            )
            print(json.dumps({
                "rule_id": rule_id,
                "action": action,
                "category": category,
                "rule_index": index,
                "ruleset_digest": ruleset.digest,
            }, indent=2, ensure_ascii=False))
        elif args.command == "load":
            ruleset = load_ruleset(args.rules)
            packets = read_packets(args.packets)
            analysis = TrafficWarehouse(ruleset, window_seconds=args.window).load(packets)
            digest = save_analysis(analysis, args.out)
            print(json.dumps({
                "digest": digest,
                "packets": analysis.packets_total,
                "bytes": analysis.bytes_total,
                "windows": len(analysis.summaries),
                "ruleset_digest": analysis.ruleset_digest,
            }, indent=2, ensure_ascii=False))
        elif args.command == "demo":
            result = run(
                args.root,
                packet_count=args.packets,
                window_seconds=args.window,
                compile_budget=args.compile_budget,
                skip_generate=args.skip_generate,
            )
            print(json.dumps({
                "demo_report_digest": result["demo_report_digest"],
                "compiled": result["compiled"],
            }, indent=2, ensure_ascii=False))
        return 0
    except (RuleValidationError, CompilationResourceError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
