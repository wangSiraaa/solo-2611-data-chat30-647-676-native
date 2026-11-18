#!/usr/bin/env python3
"""独立列目录工具：只输出 PCAPNG 结构/时间事实，不做任何协议统计。

用法::

    python3 catalog_list.py <file.pcapng> [--pretty]

该入口只导入 :mod:`pcapng_kit.catalog`，从不导入 traffic 模块，因此
不会产生任何业务协议结论。
"""

from __future__ import annotations

import argparse
import json

from pcapng_kit.catalog import Catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="List PCAPNG structure catalog")
    parser.add_argument("pcapng")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--body-buffer-limit",
        type=int,
        default=1 << 20,
        help="single body read limit in bytes (default 1 MiB)",
    )
    args = parser.parse_args()

    with Catalog.open(args.pcapng, body_buffer_limit=args.body_buffer_limit) as cat:
        listing = cat.listing()
    print(json.dumps(listing, indent=2 if args.pretty else None, ensure_ascii=False))


if __name__ == "__main__":
    main()
