"""命令行入口：目录编辑/发布、批事件导入、查询、迁移、验证、重开数据库。

所有子命令共享 ``--db``（执行仓）与 ``--catalog-db``（目录仓）；两者可以是
同一个 SQLite 文件，也可以不同，仓库只通过产品字典交换数据。

示例::

    python -m wip.cli --db wip/demo.db init
    python -m wip.cli --db wip/demo.db catalog save --file product.json --publish
    python -m wip.cli --db wip/demo.db unit create --unit U1 --route R --version v1
    python -m wip.cli --db wip/demo.db events import --file events.json
    python -m wip.cli --db wip/demo.db unit show U1
    python -m wip.cli --db wip/demo.db unit explain U1
    python -m wip.cli --db wip/demo.db unit migrate U1 --route R --version v2 --map Q=Q2
    python -m wip.cli --db wip/demo.db route validate R v1 --steps t_mach_a,t_asm_a,t_insp
    python -m wip.cli --db wip/demo.db device show dev_cnc
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional

from .catalog_db import CatalogError, CatalogRepository
from .route import RouteError, route_from_dict
from .warehouse import DEFAULT_DEVICES, Warehouse, WarehouseError


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dump(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wip", description="替代工艺路线目录 + 执行事件事实仓")
    p.add_argument("--db", default="wip/wip.db", help="执行仓 SQLite 路径")
    p.add_argument("--catalog-db", default=None, help="目录 SQLite 路径（默认同 --db）")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化执行仓与目录表")

    cat = sub.add_parser("catalog", help="目录编辑/发布")
    cat_sub = cat.add_subparsers(dest="cat_command", required=True)
    save = cat_sub.add_parser("save", help="保存产品定义（同 RouteDefinition.to_dict 格式）")
    save.add_argument("--file", required=True, help="JSON 文件或 - 表示 stdin")
    save.add_argument("--publish", action="store_true", help="保存后立即发布")
    pub = cat_sub.add_parser("publish", help="发布草稿版本")
    pub.add_argument("route_id")
    pub.add_argument("version")
    lst = cat_sub.add_parser("list", help="列出目录产品版本")
    lst.add_argument("route_id", nargs="?")
    exp = cat_sub.add_parser("export", help="导出产品定义字典")
    exp.add_argument("route_id")
    exp.add_argument("version")
    edit = cat_sub.add_parser("edit", help="从既有版本复制出新的草稿版本")
    edit.add_argument("route_id")
    edit.add_argument("base_version")
    edit.add_argument("new_version")
    dev = cat_sub.add_parser("device", help="登记设备种类")
    dev.add_argument("device_id")
    dev.add_argument("device_type")

    rout = sub.add_parser("route", help="独立路线校验/剩余工时")
    rout.add_argument("route_id")
    rout.add_argument("version")
    rout.add_argument("--steps", default="", help="逗号分隔的 step 序列")
    rout.add_argument("--state", default=None, help="查询从该状态起的最短剩余工时")

    unit = sub.add_parser("unit", help="物料查询/创建/迁移")
    unit_sub = unit.add_subparsers(dest="unit_command", required=True)
    create = unit_sub.add_parser("create")
    create.add_argument("--unit", required=True)
    create.add_argument("--route", required=True)
    create.add_argument("--version", required=True)
    create.add_argument("--file", default=None, help="可选：外部产品 JSON（同 catalog save 格式）")
    show = unit_sub.add_parser("show")
    show.add_argument("unit_id")
    expl = unit_sub.add_parser("explain")
    expl.add_argument("unit_id")
    pos = unit_sub.add_parser("position")
    pos.add_argument("unit_id")
    att = unit_sub.add_parser("attempts")
    att.add_argument("unit_id")
    bind = unit_sub.add_parser("bindings")
    bind.add_argument("unit_id")
    dur = unit_sub.add_parser("duration")
    dur.add_argument("unit_id", nargs="?")
    mig = unit_sub.add_parser("migrate")
    mig.add_argument("unit_id")
    mig.add_argument("--route", required=True)
    mig.add_argument("--version", required=True)
    mig.add_argument("--map", required=True, help="old=new[,old2=new2]")
    mig.add_argument("--reason", default="explicit migration")
    mig.add_argument("--file", default=None, help="可选：外部产品 JSON")
    estart = unit_sub.add_parser("explain-start")
    estart.add_argument("unit_id")
    estart.add_argument("--step", required=True)
    estart.add_argument("--device", required=True)
    estart.add_argument("--time", required=True, type=int)

    ev = sub.add_parser("events", help="批事件导入")
    ev.add_argument("events_action", nargs="?", default="import", choices=["import"],
                    help="可选动作（默认 import）")
    ev.add_argument("--file", required=True, help="事件 JSON 数组文件或 -")
    ev.add_argument("--batch-id", default=None)

    evl = sub.add_parser("events-list", help="列出批次结果")
    evl.add_argument("--batch-id", default=None)

    devp = sub.add_parser("device", help="设备事实查询")
    dev_sub = devp.add_subparsers(dest="device_command", required=True)
    dshow = dev_sub.add_parser("show")
    dshow.add_argument("device_id")
    dshow.add_argument("--at", type=int, default=None)
    dreg = dev_sub.add_parser("register")
    dreg.add_argument("device_id")
    dreg.add_argument("device_type")
    dlist = dev_sub.add_parser("list")

    summ = sub.add_parser("summary", help="仓库总览（产出、工时、事件数）")
    pass

    units = sub.add_parser("units", help="列出全部物料")
    pass

    return p


def _parse_map(text: str) -> dict:
    mapping = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise WarehouseError(f"迁移映射片段非法: {part!r}，应为 old=new")
        old, new = part.split("=", 1)
        mapping[old.strip()] = new.strip()
    if not mapping:
        raise WarehouseError("迁移映射不能为空")
    return mapping


def _open(args) -> tuple[Warehouse, CatalogRepository]:
    wh = Warehouse(args.db)
    cat = CatalogRepository(args.catalog_db or args.db)
    return wh, cat


def _sync_products(wh: Warehouse, cat: CatalogRepository) -> None:
    """把目录里已发布的同名产品登记进执行仓（作为外部产品字典）。"""
    for product in cat.all_products():
        try:
            wh.register_product(product.payload)
        except WarehouseError:
            # 执行仓已有同版本但内容不同（例如先注册了外部产品）时保留执行仓历史。
            continue


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        wh, cat = _open(args)
    except Exception as exc:  # noqa: BLE001 - 打开数据库失败统一报错
        print(f"打开数据库失败: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "init":
            for device_id, dt in DEFAULT_DEVICES.items():
                if wh.device_type(device_id) is None:
                    wh.register_device(device_id, dt)
                cat.register_device(device_id, dt)
            _dump({"ok": True, "db": args.db, "catalog_db": args.catalog_db or args.db,
                   "devices": DEFAULT_DEVICES})
            return 0

        if args.command == "catalog":
            if args.cat_command == "save":
                payload = _load_json(args.file)
                route = route_from_dict(payload)
                if args.publish:
                    prod = cat.save_route(route)
                else:
                    prod = cat.save_draft(route)
                _sync_products(wh, cat)
                _dump({"route_id": prod.route_id, "version": prod.version, "status": prod.status})
            elif args.cat_command == "publish":
                prod = cat.publish(args.route_id, args.version)
                _sync_products(wh, cat)
                _dump({"route_id": prod.route_id, "version": prod.version, "status": prod.status})
            elif args.cat_command == "list":
                rows = cat.versions(args.route_id) if args.route_id else cat.all_products()
                _dump([
                    {"route_id": p.route_id, "version": p.version, "status": p.status,
                     "published_at": p.published_at}
                    for p in rows
                ])
            elif args.cat_command == "export":
                _dump(cat.export_product(args.route_id, args.version))
            elif args.cat_command == "edit":
                prod = cat.edit_as_new_version(args.route_id, args.base_version, args.new_version)
                _dump({"route_id": prod.route_id, "version": prod.version, "status": prod.status})
            elif args.cat_command == "device":
                cat.register_device(args.device_id, args.device_type)
                if wh.device_type(args.device_id) != args.device_type:
                    wh.register_device(args.device_id, args.device_type)
                _dump({"device_id": args.device_id, "device_type": args.device_type})
            return 0

        if args.command == "route":
            route = cat.get_route(args.route_id, args.version)
            out: dict = {"route_id": args.route_id, "version": args.version}
            if args.steps:
                steps = [s.strip() for s in args.steps.split(",") if s.strip()]
                out["validation"] = route.validate_sequence(steps).to_dict()
                out["reachable_states"] = route.current_states(steps)
            if args.state:
                out["state"] = args.state
                out["shortest_remaining"] = route.shortest_remaining(args.state)
                path = route.shortest_remaining_path(args.state)
                out["shortest_remaining_path"] = (
                    {"duration": path[0], "steps": list(path[1])} if path else None
                )
            _dump(out)
            return 0

        if args.command == "unit":
            cmd = args.unit_command
            if cmd == "create":
                if args.file:
                    product = _load_json(args.file)
                    result = wh.create_unit(args.unit, product)
                else:
                    cat_route = cat.get_route(args.route, args.version)
                    result = wh.create_unit(args.unit, cat_route.to_dict())
                _dump({"created": args.unit, **result})
            elif cmd == "show":
                _dump(wh.get_unit(args.unit_id))
            elif cmd == "explain":
                _dump(wh.explain_unit(args.unit_id))
            elif cmd == "position":
                _dump(wh.unit_position(args.unit_id))
            elif cmd == "attempts":
                _dump(wh.unit_attempts(args.unit_id))
            elif cmd == "bindings":
                _dump(wh.unit_bindings(args.unit_id))
            elif cmd == "duration":
                _dump(wh.duration_facts(args.unit_id))
            elif cmd == "migrate":
                product = _load_json(args.file) if args.file else cat.get_route(args.route, args.version).to_dict()
                result = wh.migrate_unit(
                    args.unit_id, _parse_map(args.map), product=product, reason=args.reason
                )
                _dump({"migrated": args.unit_id, **result})
            elif cmd == "explain-start":
                _dump(wh.explain_start(args.unit_id, args.step, args.device, args.time))
            return 0

        if args.command == "events":
            events = _load_json(args.file)
            if not isinstance(events, list):
                raise WarehouseError("事件文件必须是 JSON 数组")
            result = wh.import_events(events, batch_id=args.batch_id)
            _dump(result.to_dict())
            return 0 if result.ok else 1

        if args.command == "events-list":
            if args.batch_id:
                _dump(wh.get_batch(args.batch_id))
            else:
                _dump(wh.list_batches())
            return 0

        if args.command == "device":
            if args.device_command == "show":
                _dump({"fact": wh.device_fact(args.device_id), "busy": wh.device_busy(args.device_id, args.at)})
            elif args.device_command == "register":
                wh.register_device(args.device_id, args.device_type)
                cat.register_device(args.device_id, args.device_type)
                _dump({"device_id": args.device_id, "device_type": args.device_type})
            elif args.device_command == "list":
                _dump(wh.list_devices())
            return 0

        if args.command == "summary":
            _dump(wh.summary())
            return 0

        if args.command == "units":
            _dump(wh.list_units())
            return 0

        _dump({"error": f"未知命令 {args.command}"})
        return 2
    except (WarehouseError, CatalogError, RouteError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        wh.close()
        cat.close()


if __name__ == "__main__":
    raise SystemExit(main())
