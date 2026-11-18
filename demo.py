#!/usr/bin/env python3
"""端到端自证：合成生成 → plan → collect → inspect → verify → 规则热更。

运行：``python3 demo.py``。所有产物都落在 ``./demo_workspace`` 下，
只操作本脚本自己生成的合成采集根。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

from ignarc.archiver import (
    PAYLOAD_PREFIX,
    collect,
    inspect_package,
    verify_package,
)
from ignarc.cli import main as cli_main
from ignarc.engine import RuleEngine
from ignarc.fs import AuditedFS
from ignarc.planner import build_plan
from ignarc.synthgen import (
    TARGET_BYTES,
    TARGET_DEPTH,
    TARGET_IGNORE_FILES,
    TARGET_ITEMS,
    generate_synthetic_tree,
)
from ignarc.traversal import select

WORK = Path("demo_workspace")


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    root = WORK / "synthetic_root"
    out = WORK / "packages"
    extract = WORK / "unpacked"

    section("1. 生成确定性合成采集树（只写本脚本自己的目录）")
    t0 = time.perf_counter()
    info = generate_synthetic_tree(root)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print(f"目标规模: 条目≥{TARGET_ITEMS} 深度={TARGET_DEPTH} "
          f"规则文件={TARGET_IGNORE_FILES} 正文≈{TARGET_BYTES // (1024 * 1024)}MiB")
    assert info["items"] >= TARGET_ITEMS, info
    assert info["ignore_files"] >= TARGET_IGNORE_FILES, info
    assert info["bytes"] >= TARGET_BYTES, info
    print(f"生成耗时 {time.perf_counter() - t0:.1f}s")

    section("2. plan：只读元数据与规则，审计中不得出现 body-read")
    fs = AuditedFS(root)
    engine = RuleEngine.from_fs(fs)
    plan = build_plan(fs, engine)
    print("plan stats:", json.dumps(plan.stats, ensure_ascii=False))
    plan_ops = {e["op"] for e in plan.audit_snapshot}
    assert plan_ops == {"metadata"}, plan_ops
    (WORK / "plan.json").write_text(plan.to_json(), encoding="utf-8")
    print("plan 审计操作类型:", plan_ops, "→ 证明规划阶段没有打开任何正文")

    section("3. collect：实际读取被选文件，生成自包含包 + manifest")
    fs = AuditedFS(root)
    engine = RuleEngine.from_fs(fs)
    t0 = time.perf_counter()
    result = collect(fs, engine, out, note="revision-1")
    print(f"包: {result.package_path}")
    print(f"manifest 旁车: {result.manifest_path}")
    print("stats:", json.dumps(result.selection.stats, ensure_ascii=False))
    print(f"采集耗时 {time.perf_counter() - t0:.1f}s")

    excluded = {n.path for n in result.selection.excluded}
    opened = {e.relpath for e in fs.body_reads()}
    assert excluded.isdisjoint(opened), excluded & opened
    print(f"排除 {len(excluded)} 项；正文读取 {len(opened)} 项；"
          f"交集={sorted(excluded & opened)} → 排除文件零打开")

    section("4. 解包逐字节对照来源（不是只比文件数）")
    report = verify_package(
        result.package_path,
        AuditedFS(root), RuleEngine.from_fs(AuditedFS(root)),
        extract_root=extract,
    )
    print(json.dumps(report["checks"], ensure_ascii=False, indent=2))
    assert report["ok"], report["errors"]
    mismatches = 0
    with zipfile.ZipFile(result.package_path) as zf:
        for entry in result.manifest["files"]:
            source = (root / entry["path"]).read_bytes()
            unpacked = (extract / PAYLOAD_PREFIX / entry["path"]).read_bytes()
            if unpacked != source or hashlib.sha256(unpacked).hexdigest() != entry["sha256"]:
                mismatches += 1
    print(f"逐字节对照 {len(result.manifest['files'])} 个文件，不一致 {mismatches} 个")
    assert mismatches == 0

    section("5. inspect：旧包离线可查来源与规则依据")
    manifest = inspect_package(result.package_path)
    sample = next(e for e in manifest["files"]
                  if e["basis"]["source"] is not None)
    print("带规则依据的样例条目:")
    print(json.dumps(sample, ensure_ascii=False, indent=2))
    print(f"排除记录 {len(manifest['excluded'])} 条；剪枝目录 "
          f"{len(manifest['pruned_dirs'])} 个；随包规则文件 "
          f"{len(manifest['rule_files'])} 份")

    section("6. 规则修订：新选择 + 新包，旧包保留且仍可查；缓存不复用")
    target_ignore = root / "case_override" / ".ignore"
    with target_ignore.open("a", encoding="utf-8") as fh:
        fh.write("!a.log\n")
    fs2 = AuditedFS(root)
    eng2 = RuleEngine.from_fs(fs2)
    assert eng2.rule_digest != result.rule_digest
    result2 = collect(fs2, eng2, out, note="revision-2")
    old_paths = {e["path"] for e in result.manifest["files"]}
    new_paths = {e["path"] for e in result2.manifest["files"]}
    print("修订前包含 a.log:", "case_override/a.log" in old_paths)
    print("修订后包含 a.log:", "case_override/a.log" in new_paths)
    print("旧包仍存在:", os.path.exists(result.package_path),
          "新包路径不同:", result.package_path != result2.package_path)
    assert "case_override/a.log" not in old_paths
    assert "case_override/a.log" in new_paths
    drift = verify_package(result.package_path, fs2, eng2)
    print("用新规则校验旧包 → ok =", drift["ok"],
          "（预期 False，并给出选择集合漂移明细）")
    assert not drift["ok"]

    section("7. CLI 冒烟：四个子命令")
    cli_main(["plan", "--root", str(root), "--quiet",
              "--output", str(WORK / "cli_plan.json")])
    cli_main(["collect", "--root", str(root), "--output-dir", str(out),
              "--quiet", "--note", "cli"])
    pkg = sorted(Path(out).glob("collection-*.pack.zip"))[-1]
    cli_main(["inspect", str(pkg)])
    code = cli_main(["verify", str(pkg), "--root", str(root),
                     "--extract-root", str(WORK / "cli_unpack")])
    assert code == 0

    section("全部自证通过 ✔")
    print("产物目录:", WORK.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
