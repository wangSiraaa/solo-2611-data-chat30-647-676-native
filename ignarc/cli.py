"""命令行入口：plan / collect / inspect / verify。

所有命令都要求显式传入采集根；工具自身不会主动扫描任何其他目录。
"""

from __future__ import annotations

import argparse
import json
import sys

from .archiver import collect, inspect_package, verify_package
from .engine import RuleEngine
from .fs import AuditedFS
from .planner import build_plan


def _open_root(root: str) -> AuditedFS:
    fs = AuditedFS(root)
    return fs


def cmd_plan(args: argparse.Namespace) -> int:
    fs = _open_root(args.root)
    engine = RuleEngine.from_fs(fs)
    plan = build_plan(fs, engine)
    payload = json.loads(plan.to_json())
    payload["audit"] = plan.audit_snapshot
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    if not args.quiet:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[plan] metadata-only OK; no body-read occurred; "
          f"stats={plan.stats}", file=sys.stderr)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    fs = _open_root(args.root)
    engine = RuleEngine.from_fs(fs)
    result = collect(fs, engine, args.output_dir, note=args.note)
    if not args.quiet:
        print(json.dumps({
            "package": result.package_path,
            "manifest": result.manifest_path,
            "package_id": result.package_id,
            "rule_digest": result.rule_digest,
            "stats": result.selection.stats,
        }, ensure_ascii=False, indent=2))
    print(f"[collect] body reads={len(fs.body_reads())} "
          f"excluded={len(result.selection.excluded)} "
          f"pruned={len(result.selection.pruned_dirs)}", file=sys.stderr)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    manifest = inspect_package(args.package)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    fs = engine = None
    if args.root:
        fs = _open_root(args.root)
        engine = RuleEngine.from_fs(fs)
    report = verify_package(
        args.package, fs, engine,
        extract_root=args.extract_root,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ignarc",
        description="独立 .ignore 规则解释服务与可追溯目录归档器",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="只基于元数据与规则产出选择规划")
    p.add_argument("--root", required=True)
    p.add_argument("--output")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_plan)

    c = sub.add_parser("collect", help="实际读取被选文件并生成自包含包")
    c.add_argument("--root", required=True)
    c.add_argument("--output-dir", required=True)
    c.add_argument("--note", default="")
    c.add_argument("--quiet", action="store_true")
    c.set_defaults(func=cmd_collect)

    i = sub.add_parser("inspect", help="离线查看包的 manifest 与规则依据")
    i.add_argument("package")
    i.set_defaults(func=cmd_inspect)

    v = sub.add_parser("verify", help="校验包；可选对照采集根与解包字节")
    v.add_argument("package")
    v.add_argument("--root")
    v.add_argument("--extract-root")
    v.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
