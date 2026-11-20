"""可复跑的合成演示：生成产品/事件 JSON，执行题面要求的全部案例。

运行::

    python -m wip.demo --out wip/demo_out

会写出 *产品定义、事件批次、迁移后事件* 与一份 ``report.json``，
并打印关键结果。所有输出都是确定性的（固定 ID/时刻）。
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List

from .catalog_db import CatalogRepository
from .route import RouteDefinition, RouteState, StepDefinition
from .warehouse import DEFAULT_DEVICES, Warehouse

DEVICES = DEFAULT_DEVICES


def product_v1() -> RouteDefinition:
    states = {
        "S0": RouteState.INITIAL,
        "A": RouteState.NORMAL,
        "B": RouteState.NORMAL,
        "Q": RouteState.NORMAL,
        "RW": RouteState.NORMAL,
        "OK": RouteState.ACCEPTED,
        "SCRAP": RouteState.SCRAP,
    }
    steps = [
        StepDefinition("t_mach_a", "S0", "A", "CNC", 10),
        StepDefinition("t_mach_b", "S0", "B", "CNC", 8),
        StepDefinition("t_asm_a", "A", "Q", "ASSEMBLY", 5),
        StepDefinition("t_asm_b", "B", "Q", "ASSEMBLY", 3),
        StepDefinition("t_insp", "Q", "OK", "INSPECTION", 2),
        StepDefinition("t_scrap", "Q", "SCRAP", "INSPECTION", 1),
        StepDefinition("t_rework_in", "Q", "RW", "INSPECTION", 3),
        StepDefinition("t_rework_out", "RW", "Q", "REWORK", 2),
    ]
    return RouteDefinition("R", "v1", "S0", ["OK"], ["SCRAP"], states, steps, description="基线两条替代工艺")


def product_v2() -> RouteDefinition:
    """ECN 后的产品：Q 改为 Q2，检验改为 t_insp2 且标准工时 7；报废步改名。"""
    states = {
        "S0": RouteState.INITIAL,
        "A": RouteState.NORMAL,
        "B": RouteState.NORMAL,
        "Q2": RouteState.NORMAL,
        "RW": RouteState.NORMAL,
        "OK": RouteState.ACCEPTED,
        "SCRAP": RouteState.SCRAP,
    }
    steps = [
        StepDefinition("t_mach_a", "S0", "A", "CNC", 10),
        StepDefinition("t_mach_b", "S0", "B", "CNC", 8),
        StepDefinition("t_asm_a", "A", "Q2", "ASSEMBLY", 5),
        StepDefinition("t_asm_b", "B", "Q2", "ASSEMBLY", 3),
        StepDefinition("t_insp2", "Q2", "OK", "INSPECTION", 7),
        StepDefinition("t_scrap2", "Q2", "SCRAP", "INSPECTION", 1),
        StepDefinition("t_rework_in", "Q2", "RW", "INSPECTION", 3),
        StepDefinition("t_rework_out", "RW", "Q2", "REWORK", 2),
    ]
    return RouteDefinition("R", "v2", "S0", ["OK"], ["SCRAP"], states, steps, description="ECN-42")


def _ev(seq, etype, unit, attempt, time, step=None, device=None) -> dict:
    d = {
        "acceptance_seq": seq,
        "event_type": etype,
        "unit_id": unit,
        "attempt_id": attempt,
        "time": time,
    }
    if step is not None:
        d["step_id"] = step
    if device is not None:
        d["device_id"] = device
    return d


class _Seq:
    """单调整数分配器，保证同一事件批内 acceptance_seq 严格递增。"""

    def __init__(self, start: int) -> None:
        self.value = start

    def take(self) -> int:
        current = self.value
        self.value += 1
        return current


def build_cases() -> Dict[str, object]:
    """返回演示用的命名事件列表（acceptance_seq 单调递增，互不重叠）。"""
    cases: Dict[str, object] = {}

    # 1) 替代路线：U_ALT 走 B 分支；U_ALT2 走 A 分支（时间窗互不重叠）
    seq = _Seq(1)
    alt: List[dict] = []
    for unit, mach, base in (("U_ALT", "t_mach_b", 0), ("U_ALT2", "t_mach_a", 100)):
        asm = "t_asm_b" if mach == "t_mach_b" else "t_asm_a"
        mach_dur = 8 if mach == "t_mach_b" else 10
        asm_dur = 3 if mach == "t_mach_b" else 5
        mach_end = base + mach_dur
        asm_end = mach_end + asm_dur
        alt.append(_ev(seq.take(), "START", unit, f"{unit}-1", base, mach, "dev_cnc"))
        alt.append(_ev(seq.take(), "FINISH", unit, f"{unit}-1", mach_end))
        alt.append(_ev(seq.take(), "START", unit, f"{unit}-2", mach_end, asm, "dev_asm"))
        alt.append(_ev(seq.take(), "FINISH", unit, f"{unit}-2", asm_end))
        alt.append(_ev(seq.take(), "START", unit, f"{unit}-3", asm_end, "t_insp", "dev_insp"))
        alt.append(_ev(seq.take(), "FINISH", unit, f"{unit}-3", asm_end + 2))
    cases["alternative_routes"] = alt

    # 2) 合法返工回路：U_RW 在 Q -> RW -> Q 往返两次后合格
    seq = _Seq(100)
    base = 200
    cases["legal_rework"] = [
        _ev(seq.take(), "START", "U_RW", "rw-1", base, "t_mach_a", "dev_cnc"),
        _ev(seq.take(), "FINISH", "U_RW", "rw-1", base + 10),
        _ev(seq.take(), "START", "U_RW", "rw-2", base + 10, "t_asm_a", "dev_asm"),
        _ev(seq.take(), "FINISH", "U_RW", "rw-2", base + 15),
        _ev(seq.take(), "START", "U_RW", "rw-3", base + 15, "t_rework_in", "dev_insp"),
        _ev(seq.take(), "FINISH", "U_RW", "rw-3", base + 18),
        _ev(seq.take(), "START", "U_RW", "rw-4", base + 18, "t_rework_out", "dev_rw"),
        _ev(seq.take(), "FINISH", "U_RW", "rw-4", base + 20),
        _ev(seq.take(), "START", "U_RW", "rw-5", base + 20, "t_rework_in", "dev_insp"),
        _ev(seq.take(), "FINISH", "U_RW", "rw-5", base + 23),
        _ev(seq.take(), "START", "U_RW", "rw-6", base + 23, "t_rework_out", "dev_rw"),
        _ev(seq.take(), "FINISH", "U_RW", "rw-6", base + 25),
        _ev(seq.take(), "START", "U_RW", "rw-7", base + 25, "t_insp", "dev_insp"),
        _ev(seq.take(), "FINISH", "U_RW", "rw-7", base + 27),
    ]

    # 3) 设备同秒交接：[300,310) 结束后 310 秒另一件立刻开始
    cases["same_second_handoff"] = [
        _ev(200, "START", "U_HAND1", "h1", 300, "t_mach_a", "dev_cnc"),
        _ev(201, "FINISH", "U_HAND1", "h1", 310),
        _ev(202, "START", "U_HAND2", "h2", 310, "t_mach_a", "dev_cnc"),
        _ev(203, "FINISH", "U_HAND2", "h2", 320),
    ]

    # 4) 乱序 FINISH：列表按业务时刻排（FINISH 410 在 START 400 前），仓库按序号执行
    cases["out_of_order_finish"] = [
        _ev(301, "FINISH", "U_OOO", "o1", 410),
        _ev(300, "START", "U_OOO", "o1", 400, "t_mach_a", "dev_cnc"),
    ]

    # 5) 取消后重试：CANCEL 释放，同设备重试成功
    cases["cancel_then_retry"] = [
        _ev(400, "START", "U_RETRY", "r1", 500, "t_mach_a", "dev_cnc"),
        _ev(401, "CANCEL", "U_RETRY", "r1", 503),
        _ev(402, "START", "U_RETRY", "r2", 503, "t_mach_a", "dev_cnc"),
        _ev(403, "FINISH", "U_RETRY", "r2", 513),
    ]

    # 6) 迁移后旧工步拒绝：U_MIG 在 Q 处迁移到 v2，随后尝试旧 t_insp 被拒
    cases["migration_old_step_rejected"] = [
        _ev(500, "START", "U_MIG", "m1", 600, "t_mach_a", "dev_cnc"),
        _ev(501, "FINISH", "U_MIG", "m1", 610),
        _ev(502, "START", "U_MIG", "m2", 610, "t_asm_a", "dev_asm"),
        _ev(503, "FINISH", "U_MIG", "m2", 615),
        _ev(504, "START", "U_MIG", "m3", 615, "t_insp", "dev_insp"),  # 迁移后应被拒
        _ev(505, "FINISH", "U_MIG", "m3", 617),
    ]
    return cases


def run(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    v1, v2 = product_v1(), product_v2()

    catalog_path = os.path.join(out_dir, "catalog.db")
    exec_path = os.path.join(out_dir, "exec.db")
    for p in (catalog_path, exec_path):
        if os.path.exists(p):
            os.remove(p)

    cat = CatalogRepository(catalog_path)
    cat.save_route(v1)
    cat.save_route(v2)
    for device_id, device_type in DEVICES.items():
        cat.register_device(device_id, device_type)

    wh = Warehouse(exec_path)
    for device_id, device_type in DEVICES.items():
        wh.register_device(device_id, device_type)

    report: dict = {"route_checks": {}, "cases": {}}

    # 目录独立校验 + 最短剩余
    report["route_checks"]["v1_alternative_A"] = v1.validate_sequence(
        ["t_mach_a", "t_asm_a", "t_insp"]
    ).to_dict()
    report["route_checks"]["v1_alternative_B"] = v1.validate_sequence(
        ["t_mach_b", "t_asm_b", "t_insp"]
    ).to_dict()
    report["route_checks"]["v1_rework"] = v1.validate_sequence(
        ["t_mach_a", "t_asm_a", "t_rework_in", "t_rework_out", "t_insp"]
    ).to_dict()
    report["route_checks"]["v1_scrap_not_accepted"] = v1.validate_sequence(
        ["t_mach_a", "t_asm_a", "t_scrap"]
    ).to_dict()
    report["route_checks"]["v1_shortest_from_S0"] = v1.shortest_remaining("S0")
    report["route_checks"]["v1_shortest_from_Q"] = v1.shortest_remaining("Q")
    report["route_checks"]["v2_shortest_from_Q2"] = v2.shortest_remaining("Q2")

    cases = build_cases()

    # 创建物料（绑定 v1），并写出产品定义
    units = [
        "U_ALT", "U_ALT2", "U_RW", "U_HAND1", "U_HAND2",
        "U_OOO", "U_RETRY", "U_MIG",
    ]
    for uid in units:
        wh.create_unit(uid, v1.to_dict())

    # 迁移前将 U_MIG 的路线记录留待后续迁移
    case_results = {}
    for name in ("alternative_routes", "legal_rework", "same_second_handoff", "out_of_order_finish", "cancel_then_retry"):
        res = wh.import_events(cases[name], batch_id=f"demo-{name}")
        case_results[name] = res.to_dict()
    # 迁移相关案例分两步：先完成 Q，再迁移，再导入旧工步（应拒绝）
    pre = cases["migration_old_step_rejected"][:4]
    res = wh.import_events(pre, batch_id="demo-migration-pre")
    case_results["migration_pre"] = res.to_dict()
    mig = wh.migrate_unit("U_MIG", {"Q": "Q2"}, product=v2.to_dict(), reason="ECN-42 演示")
    case_results["migration"] = mig
    post = cases["migration_old_step_rejected"][4:]
    res = wh.import_events(post, batch_id="demo-migration-post")
    case_results["migration_old_step_rejected"] = res.to_dict()

    report["cases"] = case_results

    # 查询事实
    report["summary"] = wh.summary()
    report["units"] = {uid: wh.get_unit(uid) for uid in units}
    report["positions"] = {uid: wh.unit_position(uid) for uid in units}
    report["durations"] = wh.duration_facts()
    report["device_facts"] = {d: wh.device_fact(d) for d in DEVICES}
    report["unit_explain_U_MIG"] = wh.explain_unit("U_MIG")
    report["explain_start_after_migration"] = wh.explain_start(
        "U_MIG", "t_insp", "dev_insp", 20
    )
    report["explain_start_new_step"] = wh.explain_start(
        "U_MIG", "t_insp2", "dev_insp", 20
    )

    # 写出合成输入，便于用 CLI 复跑
    with open(os.path.join(out_dir, "product_v1.json"), "w", encoding="utf-8") as fh:
        json.dump(v1.to_dict(), fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "product_v2.json"), "w", encoding="utf-8") as fh:
        json.dump(v2.to_dict(), fh, ensure_ascii=False, indent=2)
    for name, events in cases.items():
        if name == "migration_old_step_rejected":
            # 迁移前后必须分成两批，中间插入 migrate 操作。
            with open(os.path.join(out_dir, "events_migration_pre.json"), "w", encoding="utf-8") as fh:
                json.dump(events[:4], fh, ensure_ascii=False, indent=2)
            with open(os.path.join(out_dir, "events_migration_post.json"), "w", encoding="utf-8") as fh:
                json.dump(events[4:], fh, ensure_ascii=False, indent=2)
            continue
        with open(os.path.join(out_dir, f"events_{name}.json"), "w", encoding="utf-8") as fh:
            json.dump(events, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)

    wh.close()
    cat.close()
    return report


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="wip.demo")
    parser.add_argument("--out", default="wip/demo_out")
    args = parser.parse_args(argv)
    report = run(args.out)
    print(json.dumps({
        "out": args.out,
        "route_checks": report["route_checks"],
        "case_status": {k: v.get("status") for k, v in report["cases"].items() if isinstance(v, dict) and "status" in v},
        "summary": report["summary"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
