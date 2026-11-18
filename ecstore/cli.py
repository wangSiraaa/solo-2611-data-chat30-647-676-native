"""命令行入口: 独立条带服务与对象恢复仓。

服务类 (不依赖对象仓):
  ecstore svc-register / svc-encode / svc-verify / svc-recover / svc-configs
对象仓类:
  ecstore archive / repo-list / inspect / recover / read-range / rerun
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import codec
from .repository import ObjectRepository
from .service import StripeService


def _emit(obj: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(_human(obj))


def _human(obj: dict, indent: int = 0) -> str:
    pad = "  " * indent
    lines = []
    for key, value in obj.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(_human(value, indent + 1))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{pad}{key}:")
            for item in value:
                lines.append(_human(item, indent + 1))
        else:
            lines.append(f"{pad}{key}: {value}")
    return "\n".join(lines)


def _parse_rows(text: str | None) -> list[list[int]] | None:
    if text is None:
        return None
    rows = json.loads(text)
    if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
        raise argparse.ArgumentTypeError("--rows 必须是二维 JSON 数组, 如 [[1,2],[3,4]]")
    return rows


# ---------------------------------------------------------------- 服务命令
def cmd_svc_register(args) -> int:
    svc = StripeService(args.root)
    config = svc.register(args.k, args.n, args.L, _parse_rows(args.rows))
    _emit({"registered": True, "config_digest": config.digest,
           "spec": config.to_spec()}, args.json)
    return 0


def cmd_svc_configs(args) -> int:
    svc = StripeService(args.root)
    _emit({"configs": svc.list_configs()}, args.json)
    return 0


def cmd_svc_encode(args) -> int:
    svc = StripeService(args.root)
    data = Path(args.input).read_bytes()
    meta = svc.encode(args.config_digest, data, args.out_dir)
    _emit({"encoded": True, **meta}, args.json)
    return 0


def cmd_svc_verify(args) -> int:
    svc = StripeService(args.root)
    report = svc.verify(args.dir, args.stripe_id)
    out = svc_report_dict(report)
    _emit(out, args.json)
    return 0 if report.status == "recovered" else 2


def cmd_svc_recover(args) -> int:
    svc = StripeService(args.root)
    report = svc.recover(args.dir, args.stripe_id)
    if report.status == "recovered":
        Path(args.output).write_bytes(report.recovered_data)
    out = svc_report_dict(report)
    out["output"] = args.output if report.status == "recovered" else None
    _emit(out, args.json)
    return 0 if report.status == "recovered" else 2


def svc_report_dict(report) -> dict:
    def issues(items):
        return [{"file": i.file, "reason": i.reason} for i in items]
    return {
        "status": report.status,
        "config_digest": report.config_digest,
        "stripe_id": report.stripe_id,
        "valid_indices": report.valid_indices,
        "reason": report.reason,
        "original_sha256": report.original_digest,
        "quarantined": issues(report.quarantined),
        "conflicts": issues(report.conflicts),
        "duplicates": issues(report.duplicate_files),
        "foreign": issues(report.foreign),
    }


# ---------------------------------------------------------------- 仓命令
def cmd_archive(args) -> int:
    repo = ObjectRepository(args.repo)
    data = Path(args.input).read_bytes()
    result = repo.import_object(data, args.name)
    _emit(result, args.json)
    return 0


def cmd_repo_list(args) -> int:
    repo = ObjectRepository(args.repo)
    _emit({"objects": repo.list_objects()}, args.json)
    return 0


def cmd_inspect(args) -> int:
    repo = ObjectRepository(args.repo)
    info = repo.inspect_dir(args.recovery_dir)
    _emit(info, args.json)
    return 0 if info.get("complete") else 2


def cmd_recover(args) -> int:
    repo = ObjectRepository(args.repo)
    result = repo.recover_object(args.recovery_dir, args.output)
    _emit(repo.result_to_dict(result), args.json)
    return 0 if result.complete else 2


def cmd_read_range(args) -> int:
    repo = ObjectRepository(args.repo)
    data = repo.read_range(args.recovery_dir, args.start, args.end)
    Path(args.output).write_bytes(data)
    _emit({"start": args.start, "end": args.end, "bytes": len(data),
           "output": args.output,
           "sha256": __import__("hashlib").sha256(data).hexdigest()}, args.json)
    return 0


def cmd_rerun(args) -> int:
    """复跑: 对恢复目录做 inspect, 完整时再做整对象恢复并核对摘要。"""
    repo = ObjectRepository(args.repo)
    info = repo.inspect_dir(args.recovery_dir)
    _emit({"phase": "inspect", **info}, args.json)
    if not info.get("complete"):
        print("rerun: 存在未恢复条带, 不执行整对象发布", file=sys.stderr)
        return 2
    result = repo.recover_object(args.recovery_dir, args.output)
    _emit({"phase": "recover", **repo.result_to_dict(result)}, args.json)
    return 0 if result.complete else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ecstore",
                                description="GF(2^8) 纠删码条带服务与受损对象恢复仓")
    sub = p.add_subparsers(dest="command", required=True)

    def add_json(sp):
        sp.add_argument("--json", action="store_true", help="输出 JSON")

    sp = sub.add_parser("svc-register", help="登记编码配置 (校验任意 k 行可逆)")
    sp.add_argument("root")
    sp.add_argument("--k", type=int, required=True)
    sp.add_argument("--n", type=int, required=True)
    sp.add_argument("--L", type=int, required=True)
    sp.add_argument("--rows", help='编码行二维 JSON 数组; 省略则生成 Cauchy MDS 行')
    add_json(sp)
    sp.set_defaults(func=cmd_svc_register)

    sp = sub.add_parser("svc-configs", help="列出已登记配置")
    sp.add_argument("root")
    add_json(sp)
    sp.set_defaults(func=cmd_svc_configs)

    sp = sub.add_parser("svc-encode", help="独立编码: k*L 字节 -> n 个片")
    sp.add_argument("root")
    sp.add_argument("--config-digest", required=True)
    sp.add_argument("--input", required=True)
    sp.add_argument("--out-dir", required=True)
    add_json(sp)
    sp.set_defaults(func=cmd_svc_encode)

    sp = sub.add_parser("svc-verify", help="独立核验片目录")
    sp.add_argument("root")
    sp.add_argument("--dir", required=True)
    sp.add_argument("--stripe-id", default=None)
    add_json(sp)
    sp.set_defaults(func=cmd_svc_verify)

    sp = sub.add_parser("svc-recover", help="独立恢复条带 (任意 >=k 合法片)")
    sp.add_argument("root")
    sp.add_argument("--dir", required=True)
    sp.add_argument("--output", required=True)
    sp.add_argument("--stripe-id", default=None)
    add_json(sp)
    sp.set_defaults(func=cmd_svc_recover)

    sp = sub.add_parser("archive", help="对象归档 (任意长度, 末条带补零)")
    sp.add_argument("repo")
    sp.add_argument("--input", required=True)
    sp.add_argument("--name", required=True)
    add_json(sp)
    sp.set_defaults(func=cmd_archive)

    sp = sub.add_parser("repo-list", help="列出仓内对象清单")
    sp.add_argument("repo")
    add_json(sp)
    sp.set_defaults(func=cmd_repo_list)

    sp = sub.add_parser("inspect", help="逐条带检视恢复目录")
    sp.add_argument("repo")
    sp.add_argument("--recovery-dir", required=True)
    add_json(sp)
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("recover", help="从恢复目录恢复对象")
    sp.add_argument("repo")
    sp.add_argument("--recovery-dir", required=True)
    sp.add_argument("--output", required=True)
    add_json(sp)
    sp.set_defaults(func=cmd_recover)

    sp = sub.add_parser("read-range", help="半开字节范围 [start,end) 读取")
    sp.add_argument("repo")
    sp.add_argument("--recovery-dir", required=True)
    sp.add_argument("--start", type=int, required=True)
    sp.add_argument("--end", type=int, required=True)
    sp.add_argument("--output", required=True)
    add_json(sp)
    sp.set_defaults(func=cmd_read_range)

    sp = sub.add_parser("rerun", help="复跑: inspect + 完整恢复核对")
    sp.add_argument("repo")
    sp.add_argument("--recovery-dir", required=True)
    sp.add_argument("--output", required=True)
    add_json(sp)
    sp.set_defaults(func=cmd_rerun)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (codec.ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
