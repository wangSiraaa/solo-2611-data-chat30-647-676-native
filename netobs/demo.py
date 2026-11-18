"""演练编排：生成 → 遮蔽目录 → 编译 → 装载 → 修订重分类 → 全量核对。

所有输入/输出均在本地合成，不与真实防火墙交互。产物为内容寻址的 JSON，
旧产品文件不被覆盖式改写（除非同一路径显式重建）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .compile import CompilationResourceError, compile_rules, equivalence_report
from .generate import build_all
from .index import RuleIndex
from .reference import (
    check_byte_conservation,
    enumerate_all,
    high_bit_points,
    reference_classify,
    small_domain_ruleset,
    verify_facts_against_reference,
)
from .rules import load_ruleset
from .shadow import RuleCatalog
from .spatial import format_point
from .traffic import (
    TrafficWarehouse,
    fact_identity_map,
    read_packets,
    save_analysis,
)


def _write_json(path: Path, obj: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run(
    root: str | Path = ".",
    *,
    packet_count: int = 100_000,
    window_seconds: int = 60,
    compile_budget: int = 200_000,
    skip_generate: bool = False,
) -> dict[str, Any]:
    root = Path(root)
    data = root / "data"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    if not skip_generate or not (data / "rules_base.json").exists():
        generate_summary = build_all(root, packet_count=packet_count)
    else:
        generate_summary = json.loads((artifacts / "generate_summary.json").read_text("utf-8"))

    variants = {
        "base": load_ruleset(data / "rules_base.json"),
        "reorder_lpm": load_ruleset(data / "rules_reorder_lpm.json"),
        "revised": load_ruleset(data / "rules_revised.json"),
        "deleted": load_ruleset(data / "rules_deleted.json"),
    }
    packets = read_packets(data / "packets.jsonl")

    result: dict[str, Any] = {"generate": generate_summary}

    # 1) 规则目录（遮蔽分析），不依赖流量
    catalogs: dict[str, Any] = {}
    for name, ruleset in variants.items():
        catalog = RuleCatalog(ruleset)
        verdicts = catalog.analyze_all()
        verification = [catalog.verify_verdict(v) for v in verdicts]
        payload = catalog.to_dict()
        payload["verification"] = [
            {"rule_id": v.rule_id, **check} for v, check in zip(verdicts, verification)
        ]
        digest = _write_json(artifacts / f"shadow_{name}.json", payload)
        catalogs[name] = {
            "digest": digest,
            "catalog_digest": catalog.digest(),
            "shadowed": [v.rule_id for v in verdicts if v.fully_shadowed],
            "open": [v.rule_id for v in verdicts if not v.fully_shadowed],
            "all_verified": all(check["verified"] for check in verification),
            "highlight": {
                rid: catalog.analyze(rid).to_dict()
                for rid in ("U_C", "I5A", "I5B", "P3", "B3", "B4", "O_DENY", "O_ACCEPT")
                if rid in {r.rule_id for r in ruleset.rules}
            },
        }
    result["catalogs"] = catalogs

    # 2) 编译为无重叠决策单元 + 精确等价检查
    compiled_payloads: dict[str, Any] = {}
    base_compiled = None
    for name, ruleset in variants.items():
        try:
            compiled = compile_rules(ruleset, max_cells=compile_budget)
        except CompilationResourceError as exc:
            compiled_payloads[name] = {"resource_refusal": str(exc), "equivalent": None}
            continue
        report = equivalence_report(ruleset, compiled)
        payload = compiled.to_dict()
        payload["equivalence"] = report
        digest = _write_json(artifacts / f"cells_{name}.json", payload)
        compiled_payloads[name] = {
            "digest": digest,
            "cells": len(compiled.cells),
            "equivalent": report["equivalent"],
            "issues": report["issues"],
        }
        if name == "base":
            base_compiled = compiled
    result["compiled"] = compiled_payloads

    # 2b) 用编译单元做批量分类（仅演示吞吐，不作为等价证明）
    if base_compiled is not None:
        sample = packets[:2000]
        mismatches = 0
        for packet in sample:
            cell = base_compiled.classify(
                packet.src, packet.dst, packet.protocol, packet.sport, packet.dport
            )
            if (cell.rule_id, cell.action, cell.category) != variants["base"].classify(
                packet.src, packet.dst, packet.protocol, packet.sport, packet.dport
            )[:3]:
                mismatches += 1
        result["batch_via_cells"] = {
            "packets_checked": len(sample),
            "mismatches": mismatches,
            "note": "演示用批量抽样；等价结论来自集合运算与完整枚举，不依赖此抽样",
        }

    # 3) 小域完整枚举 + 高位点
    small_ruleset, domain = small_domain_ruleset()
    enum = enumerate_all(small_ruleset, **domain)
    small_catalog = RuleCatalog(small_ruleset)
    result["small_domain"] = {
        "points": enum.points,
        "mismatches": enum.mismatches,
        "ok": enum.ok,
        "compiled_cells": enum.compiled_cells,
        "shadow": {
            v.rule_id: {
                "fully_shadowed": v.fully_shadowed,
                "covered_by": [rid for rid, _ in v.cover],
                "counterexample": (
                    format_point(v.counterexample) if v.counterexample else None
                ),
                "verified": small_catalog.verify_verdict(v)["verified"],
            }
            for v in small_catalog.analyze_all()
        },
        "domain": {k: list(v) for k, v in domain.items()},
    }
    high_bits = []
    base_index = RuleIndex(variants["base"])
    for point in high_bit_points():
        high_bits.append(
            {
                "point": format_point(point),
                "expected": reference_classify(variants["base"], *point),
                "ruleset": variants["base"].classify(*point),
                "index": base_index.classify(*point),
            }
        )
    result["high_bit_checks"] = high_bits

    # 4) 装载流量（每个规则产品独立事实仓）；先落盘 base 并快照，装载变体后复核未被改写
    identities: dict[str, dict[str, tuple[str, str, str]]] = {}
    analyses: dict[str, Any] = {}
    base_path = artifacts / "facts_base.json"
    base_snapshot = None
    for name, ruleset in variants.items():
        warehouse = TrafficWarehouse(ruleset, window_seconds=window_seconds)
        analysis = warehouse.load(packets)
        digest = save_analysis(analysis, artifacts / f"facts_{name}.json")
        if name == "base":
            base_snapshot = (base_path.read_bytes(), base_path.stat().st_mtime_ns)
        ref_issues = verify_facts_against_reference(ruleset, analysis.facts)
        conservation = check_byte_conservation(analysis, packets, ruleset)
        identities[name] = fact_identity_map(analysis)
        analyses[name] = {
            "digest": digest,
            "identity_digest": analysis.identity_digest,
            "packets": analysis.packets_total,
            "bytes": analysis.bytes_total,
            "category_totals": _category_totals(analysis),
            "action_totals": _action_totals(analysis),
            "reference_issues": ref_issues[:10],
            "reference_issue_count": len(ref_issues),
            "conservation_issues": conservation[:10],
            "conservation_issue_count": len(conservation),
            "window_count": len(analysis.summaries),
        }
    result["analyses"] = analyses
    if base_snapshot is not None:
        result["base_product_file_unchanged"] = (
            base_path.read_bytes() == base_snapshot[0]
            and base_path.stat().st_mtime_ns == base_snapshot[1]
        )

    # 5) 变体差异（只换序 / 修订 / 删除 各自改变了哪些包的命中）
    result["variant_diffs"] = {
        name: _diff_identities(identities["base"], identities[name])
        for name in ("reorder_lpm", "revised", "deleted")
    }

    # 6) 资源拒绝示例：给编译极小的单元预算，必须显式报错而不是“等价”
    try:
        compile_rules(variants["base"], max_cells=5)
        refusal = None
    except CompilationResourceError as exc:
        refusal = str(exc)
    result["resource_refusal_demo"] = {
        "max_cells": 5,
        "error": refusal,
        "note": "资源不足时显式拒绝；不把拒绝当作规则等价结论",
    }

    summary_digest = _write_json(artifacts / "demo_report.json", result)
    result["demo_report_digest"] = summary_digest
    return result


def _category_totals(analysis) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for summary in analysis.summaries:
        bucket = out.setdefault(summary.category, {"packets": 0, "bytes": 0})
        bucket["packets"] += summary.packets
        bucket["bytes"] += summary.bytes
    return dict(sorted(out.items()))


def _action_totals(analysis) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for summary in analysis.summaries:
        bucket = out.setdefault(summary.action, {"packets": 0, "bytes": 0})
        bucket["packets"] += summary.packets
        bucket["bytes"] += summary.bytes
    return dict(sorted(out.items()))


def _diff_identities(
    base: dict[str, tuple[str, str, str]], other: dict[str, tuple[str, str, str]]
) -> dict[str, Any]:
    changed = []
    for pid in sorted(base):
        if base[pid] != other.get(pid):
            changed.append({"packet_id": pid, "base": base[pid], "variant": other.get(pid)})
    return {
        "changed_packets": len(changed),
        "examples": changed[:10],
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="netobs 演示：合成数据 + 全流程核对")
    parser.add_argument("--root", default=".")
    parser.add_argument("--packets", type=int, default=100_000)
    parser.add_argument("--window", type=int, default=60)
    parser.add_argument("--compile-budget", type=int, default=200_000)
    parser.add_argument("--skip-generate", action="store_true")
    args = parser.parse_args(argv)
    result = run(
        args.root,
        packet_count=args.packets,
        window_seconds=args.window,
        compile_budget=args.compile_budget,
        skip_generate=args.skip_generate,
    )
    print(json.dumps({
        "demo_report_digest": result.get("demo_report_digest"),
        "compiled": result["compiled"],
        "small_domain": {k: result["small_domain"][k] for k in ("points", "ok", "compiled_cells")},
        "analyses": {
            name: {
                "packets": data["packets"],
                "bytes": data["bytes"],
                "reference_issue_count": data["reference_issue_count"],
                "conservation_issue_count": data["conservation_issue_count"],
            }
            for name, data in result["analyses"].items()
        },
        "variant_diffs": {
            name: data["changed_packets"] for name, data in result["variant_diffs"].items()
        },
        "resource_refusal_demo": result["resource_refusal_demo"]["error"] is not None,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
