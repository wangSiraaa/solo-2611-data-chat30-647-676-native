"""命令行：校验、构建、读取、来源回查、分区目录与差量报告。

示例
----
python -m heximg validate samples/base.hex
python -m heximg build --manifest app.json --repo ./store
python -m heximg read --repo ./store --name app --start 0x0 --length 0x200
python -m heximg sources --repo ./store --name app --address 0x1007
python -m heximg diff --repo ./store --a base --b app \\
    --dir partitions.json --view unknown --id base_app_unknown
python -m heximg report --repo ./store --id base_app_unknown
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .catalog import Manifest, ManifestEntry, build_product
from .delta import KINDS, VIEW_MATERIALIZED, VIEW_UNKNOWN, compare_images
from .errors import HexError
from .partition import Partition, PartitionDirectory
from .records import parse_hex_file
from .repo import Repository


def _hex_int(text: str) -> int:
    return int(text, 0)


def _print_err(exc: HexError) -> int:
    print(f"错误：{exc}", file=sys.stderr)
    return 2


def cmd_validate(args) -> int:
    rc = 0
    for path in args.files:
        try:
            parsed = parse_hex_file(path)
        except HexError as exc:
            print(f"[FAIL] {path}: {exc}")
            rc = 2
            continue
        print(
            f"[ OK ] {path}: data 记录 {len(parsed.writes)} 条，"
            f"定义 {parsed.total_bytes} 字节，"
            f"EOF 行 {parsed.eof_line}，原记录 {len(parsed.raw_records)} 条"
        )
    return rc


def _load_manifest(args) -> Manifest:
    if args.manifest:
        return Manifest.load(args.manifest)
    if not args.file:
        raise HexError("需要 --manifest 或至少一个 --file")
    layers = args.layer or [0] * len(args.file)
    if len(layers) != len(args.file):
        raise HexError("--file 与 --layer 数量必须一致")
    return Manifest(
        entries=tuple(ManifestEntry(f, int(l))
                      for f, l in zip(args.file, layers)),
        name=args.name or "product",
    )


def cmd_build(args) -> int:
    try:
        manifest = _load_manifest(args)
        base_dir = Path(args.manifest).parent if args.manifest else Path.cwd()
        product = build_product(manifest, base_dir=base_dir)
        repo = Repository(args.repo) if args.repo else None
        if repo is not None:
            path = repo.save_product(product, manifest_doc=manifest.to_dict())
            print(f"已保存产品 {product.name!r} → {path}")
        span = product.address_span()
        print(
            f"产品 {product.name!r}：段 {product.segment_count} 个，"
            f"定义 {product.defined_byte_count} 字节，"
            f"层次 {product.layer_count}，摘要 {product.digest()[:16]}…"
        )
        if span:
            print(f"地址跨度 {span[0]:#010x}..{span[1]:#010x}")
        return 0
    except HexError as exc:
        return _print_err(exc)


def cmd_info(args) -> int:
    try:
        repo = Repository(args.repo)
        image = repo.load_product(args.name)
        span = image.address_span()
        print(f"名称      : {image.name}")
        print(f"摘要      : {image.digest()}")
        print(f"段数      : {image.segment_count}")
        print(f"定义字节  : {image.defined_byte_count}")
        print(f"层次数    : {image.layer_count}")
        if span:
            print(f"地址跨度  : {span[0]:#010x}..{span[1]:#010x}")
        for sp in image.defined_spans()[: args.max_segments]:
            head = sp.data[:16].hex(" ")
            print(f"  段 {sp.address:#010x} 长度 {sp.length:#x} 头 {head}")
        return 0
    except HexError as exc:
        return _print_err(exc)


def cmd_read(args) -> int:
    try:
        repo = Repository(args.repo)
        image = repo.load_product(args.name)
        spans = image.read_spans(args.start, args.length)
        for sp in spans:
            if sp.is_hole:
                print(f"[空洞] {sp.address:#010x} 长度 {sp.length:#x}")
            else:
                files = sorted({s.file for s in sp.sources})
                print(
                    f"[定义] {sp.address:#010x} 长度 {sp.length:#x} "
                    f"数据 {sp.data.hex(' ')} 来源 {files}"
                )
        return 0
    except HexError as exc:
        return _print_err(exc)


def cmd_sources(args) -> int:
    try:
        repo = Repository(args.repo)
        image = repo.load_product(args.name)
        if not image.is_defined(args.address):
            print(f"地址 {args.address:#010x} 是空洞（未定义），无来源")
            return 1
        value = image.byte_at(args.address)
        print(f"地址 {args.address:#010x} 值 {value:#04x} 的全部来源：")
        for src in image.sources_at(args.address):
            print(
                f"  {src.file}:{src.line_no} "
                f"原字节范围 [{src.byte_range[0]}, {src.byte_range[1]})"
            )
        return 0
    except HexError as exc:
        return _print_err(exc)


def cmd_partition_check(args) -> int:
    try:
        directory = PartitionDirectory.load(args.dir)
        print(f"分区目录修订 {directory.revision}：{len(directory.partitions)} 区")
        for p in directory.partitions:
            fill = "禁止物化" if p.fill is None else f"fill={p.fill:#04x}"
            print(f"  {p.name:12s} {p.start:#010x}..{p.end:#010x} "
                  f"长度 {p.length:#x} {fill}")
        print(f"目录摘要 {directory.revision_digest()}")
        return 0
    except HexError as exc:
        return _print_err(exc)


def cmd_diff(args) -> int:
    try:
        repo = Repository(args.repo)
        image_a = repo.load_product(args.a)
        image_b = repo.load_product(args.b)
        directory = PartitionDirectory.load(args.dir)
        report = compare_images(image_a, image_b, directory, view=args.view)
        report_id = args.id or f"{args.a}_vs_{args.b}_{args.view}"
        path = repo.save_report(report, report_id)
        print(f"差量报告已保存 → {path}")
        print(json.dumps(report.summary(), indent=2, ensure_ascii=False))
        for pd in report.partitions:
            print(
                f"[{pd.partition}] "
                + " ".join(f"{k}={pd.counts[k]}" for k in KINDS)
                + f" 空洞统计={pd.hole_stats.to_dict()}"
            )
            if args.show_runs:
                for run in pd.runs:
                    print(f"    {run.kind:9s} {run.start:#010x} "
                          f"长度 {run.length:#x}")
        return 0
    except HexError as exc:
        return _print_err(exc)


def cmd_report(args) -> int:
    try:
        repo = Repository(args.repo)
        report = repo.load_report(args.id)
        print(report.to_json())
        return 0
    except HexError as exc:
        return _print_err(exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heximg",
        description="有限 Intel HEX 映像目录与分区差量核对仓（不连接硬件）",
    )
    parser.add_argument("--version", action="version",
                        version=f"heximg {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="校验 HEX 文件（行号+原字节范围）")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("build", help="按清单/层次构建映像产品")
    p.add_argument("--manifest")
    p.add_argument("--file", action="append", help="可重复，配合 --layer")
    p.add_argument("--layer", action="append", type=int,
                   help="与 --file 同序的层次号")
    p.add_argument("--name")
    p.add_argument("--repo", help="本地仓目录；不提供则只构建不落盘")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("info", help="查看产品摘要")
    p.add_argument("--repo", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--max-segments", type=int, default=20)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("read", help="范围读取（逐段区分定义/空洞，≤1MiB）")
    p.add_argument("--repo", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--start", type=_hex_int, required=True)
    p.add_argument("--length", type=_hex_int, required=True)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("sources", help="回查字节来源")
    p.add_argument("--repo", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--address", type=_hex_int, required=True)
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("partitions", help="校验并展示分区目录")
    p.add_argument("--dir", required=True)
    p.set_defaults(func=cmd_partition_check)

    p = sub.add_parser("diff", help="两产品按分区目录做差量")
    p.add_argument("--repo", required=True)
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--view", choices=[VIEW_UNKNOWN, VIEW_MATERIALIZED],
                   default=VIEW_UNKNOWN)
    p.add_argument("--id")
    p.add_argument("--show-runs", action="store_true")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("report", help="重开旧差量报告（绑定旧目录修订）")
    p.add_argument("--repo", required=True)
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
