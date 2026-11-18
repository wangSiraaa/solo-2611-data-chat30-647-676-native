"""命令行入口：目录编辑/发布、降雨导入/修订、分析、来源解释、验证。

有理量一律以整数或 ``p/q`` 文本传入/输出，CLI 不接受小数。

示例
----
python -m hydro.cli validate
python -m hydro.cli catalog-node --db h.db --node a --area 2 --kernel 1 --publish
python -m hydro.cli catalog-edge --db h.db --from a --to b --delay 1 --proportion 1
python -m hydro.cli rain-import --db h.db --catalog <hash> --lo 0 --hi 2 \\
    --nodes a,b,c --cell a:0:1:0 --cell a:1:0:0 ...
python -m hydro.cli analyze --db h.db --catalog <hash> --rain <hash> \\
    --nodes a,b,c --lo 0 --hi 2
python -m hydro.cli explain --db h.db --catalog <hash> --rain <hash> \\
    --node c --time 1
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List, Tuple

from .catalog import Catalog, CatalogDraft
from .engine import AnalysisRequest, Engine
from .explain import SourceExplainer
from .rainfall import RainRepo
from .rational import frac
from .storage import Store


def _load_draft(store: Store, catalog_hash: str) -> Tuple[Catalog, CatalogDraft]:
    cat = store.load_catalog(catalog_hash)
    return cat, CatalogDraft.from_catalog(cat)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False))


def cmd_catalog_node(args) -> int:
    with Store(args.db) as store:
        if args.catalog:
            cat, draft = _load_draft(store, args.catalog)
        else:
            cat, draft = None, CatalogDraft()
        kernel = [frac(x) for x in args.kernel]
        if args.node in draft._nodes:
            if args.area is not None:
                draft.replace_area(args.node, args.area)
            draft.replace_kernel(args.node, kernel)
        else:
            area = args.area if args.area is not None else frac(1)
            draft.add_node(args.node, area, kernel)
        if args.publish:
            new_cat = draft.publish()
            store.save_catalog(new_cat)
            _print({"catalog_hash": new_cat.content_hash, "nodes": list(new_cat.node_ids)})
        else:
            _print({"draft_nodes": sorted(draft._nodes), "hint": "加 --publish 才会冻结发布"})
    return 0


def cmd_catalog_edge(args) -> int:
    with Store(args.db) as store:
        cat, draft = _load_draft(store, args.catalog)
        if args.remove:
            draft.remove_edge(args.from_, args.to, args.delay)
        else:
            draft.add_edge(args.from_, args.to, args.delay, frac(args.proportion))
        new_cat = draft.publish()
        store.save_catalog(new_cat)
        _print({
            "catalog_hash": new_cat.content_hash,
            "edges": [
                {"from": e.from_id, "to": e.to_id, "delay": e.delay,
                 "proportion": str(e.proportion)}
                for e in new_cat.edges
            ],
        })
    return 0


def cmd_catalog_import(args) -> int:
    with open(args.file, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    cat = Catalog.from_payload(payload, verify_hash=args.expect_hash)
    with Store(args.db) as store:
        store.save_catalog(cat)
    _print({"catalog_hash": cat.content_hash, "nodes": list(cat.node_ids)})
    return 0


def cmd_catalog_export(args) -> int:
    with Store(args.db) as store:
        cat = store.load_catalog(args.catalog)
    _print(cat.to_payload())
    return 0


def cmd_pulse(args) -> int:
    with Store(args.db) as store:
        cat = store.load_catalog(args.catalog)
    pulse = cat.unit_pulse(args.node)
    _print({
        "origin": args.node,
        "response": {
            nid: {str(t): str(v) for t, v in seq.items()}
            for nid, seq in pulse.items()
        },
    })
    return 0


def _parse_cells(items: List[str]) -> Dict[Tuple[str, int], Tuple]:
    cells: Dict[Tuple[str, int], Tuple] = {}
    for item in items:
        parts = item.split(":")
        if len(parts) != 4:
            raise ValueError(f"--cell 格式应为 node:t:rain:loss，收到 {item!r}")
        nid, t, rain, loss = parts
        cells[(nid, int(t))] = (frac(rain), frac(loss))
    return cells


def cmd_rain_import(args) -> int:
    with Store(args.db) as store:
        cat = store.load_catalog(args.catalog)
        repo = RainRepo(store, cat)
        cells = _parse_cells(args.cell)
        batch = repo.import_batch(args.lo, args.hi, args.nodes.split(","), cells)
    _print({"rain_hash": batch.rain_hash, "lo": batch.lo, "hi": batch.hi,
            "nodes": list(batch.nodes)})
    return 0


def cmd_rain_revise(args) -> int:
    with Store(args.db) as store:
        cat = store.load_catalog(args.catalog)
        repo = RainRepo(store, cat)
        parent = repo.load(args.rain)
        rain = frac(args.rain_value) if args.rain_value is not None else None
        loss = frac(args.loss_value) if args.loss_value is not None else None
        child = repo.revise_cell(parent, args.node, args.time, rain=rain, loss=loss)
    _print({"rain_hash": child.rain_hash, "parent_hash": child.parent_hash})
    return 0


def cmd_analyze(args) -> int:
    with Store(args.db) as store:
        cat = store.load_catalog(args.catalog)
        repo = RainRepo(store, cat)
        batch = repo.load(args.rain)
        report = Engine(cat).analyze(
            AnalysisRequest.make(args.nodes.split(","), args.lo, args.hi),
            batch, store,
        )
        _print(report.to_payload())
    return 0


def cmd_explain(args) -> int:
    with Store(args.db) as store:
        cat = store.load_catalog(args.catalog)
        repo = RainRepo(store, cat)
        batch = repo.load(args.rain)
        ex = SourceExplainer(cat, batch).explain(args.node, args.time)
    _print({
        "node": ex.node_id,
        "time": ex.time,
        "outflow": str(ex.outflow),
        "by_source": {f"{n}@t{t}": str(v) for (n, t), v in ex.by_source.items()},
        "paths": [
            {
                "origin": p.origin,
                "input_step": p.input_step,
                "path": list(p.path),
                "path_delay": p.path_delay,
                "path_proportion": str(p.path_proportion),
                "kernel_index": p.kernel_index,
                "volume": str(p.volume),
            }
            for p in ex.paths
        ],
    })
    return 0


def cmd_validate(args) -> int:
    from .validate import run_demo

    store, logs = run_demo(args.db)
    for line in logs:
        print(" ", line)
    print(f"OK: {len(logs)} 个案例全部通过")
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hydro", description="降雨响应核与河网汇流事实仓")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("validate", help="运行内置验证案例")
    sp.add_argument("--db", default=":memory:")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("catalog-node", help="新增/替换节点并可直接发布")
    sp.add_argument("--db", required=True)
    sp.add_argument("--catalog", help="基于已发布目录开草稿；缺省为空白草稿")
    sp.add_argument("--node", required=True)
    sp.add_argument("--area")
    sp.add_argument("--kernel", nargs="+", required=True, help="核系数，如 1/4 1/2 1/4")
    sp.add_argument("--publish", action="store_true")
    sp.set_defaults(func=cmd_catalog_node)

    sp = sub.add_parser("catalog-edge", help="增/删边并发布新版本")
    sp.add_argument("--db", required=True)
    sp.add_argument("--catalog", required=True)
    sp.add_argument("--from", dest="from_", required=True)
    sp.add_argument("--to", required=True)
    sp.add_argument("--delay", type=int, required=True)
    sp.add_argument("--proportion", default="1")
    sp.add_argument("--remove", action="store_true")
    sp.set_defaults(func=cmd_catalog_edge)

    sp = sub.add_parser("catalog-import", help="导入外部同格式目录产品")
    sp.add_argument("--db", required=True)
    sp.add_argument("--file", required=True)
    sp.add_argument("--expect-hash")
    sp.set_defaults(func=cmd_catalog_import)

    sp = sub.add_parser("catalog-export", help="导出目录规范 JSON")
    sp.add_argument("--db", required=True)
    sp.add_argument("--catalog", required=True)
    sp.set_defaults(func=cmd_catalog_export)

    sp = sub.add_parser("pulse", help="目录单位脉冲查询（不依赖降雨仓库）")
    sp.add_argument("--db", required=True)
    sp.add_argument("--catalog", required=True)
    sp.add_argument("--node", required=True)
    sp.set_defaults(func=cmd_pulse)

    sp = sub.add_parser("rain-import", help="导入完整矩形降雨/损失批次")
    sp.add_argument("--db", required=True)
    sp.add_argument("--catalog", required=True)
    sp.add_argument("--lo", type=int, required=True)
    sp.add_argument("--hi", type=int, required=True)
    sp.add_argument("--nodes", required=True, help="逗号分隔")
    sp.add_argument("--cell", action="append", default=[], help="node:t:rain:loss，可重复")
    sp.set_defaults(func=cmd_rain_import)

    sp = sub.add_parser("rain-revise", help="修订单格产生显式新版本")
    sp.add_argument("--db", required=True)
    sp.add_argument("--catalog", required=True)
    sp.add_argument("--rain", required=True)
    sp.add_argument("--node", required=True)
    sp.add_argument("--time", type=int, required=True)
    sp.add_argument("--rain-value", dest="rain_value")
    sp.add_argument("--loss-value", dest="loss_value")
    sp.set_defaults(func=cmd_rain_revise)

    sp = sub.add_parser("analyze", help="汇流分析与守恒对账")
    sp.add_argument("--db", required=True)
    sp.add_argument("--catalog", required=True)
    sp.add_argument("--rain", required=True)
    sp.add_argument("--nodes", required=True)
    sp.add_argument("--lo", type=int, required=True)
    sp.add_argument("--hi", type=int, required=True)
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("explain", help="某时刻出流的来源分解")
    sp.add_argument("--db", required=True)
    sp.add_argument("--catalog", required=True)
    sp.add_argument("--rain", required=True)
    sp.add_argument("--node", required=True)
    sp.add_argument("--time", type=int, required=True)
    sp.set_defaults(func=cmd_explain)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as ex:  # noqa: BLE001 - CLI 顶层统一领域错误输出
        print(f"错误: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
