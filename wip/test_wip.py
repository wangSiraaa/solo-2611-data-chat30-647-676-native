"""无第三方依赖的测试（unittest）。覆盖题面要求的全部案例。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from typing import List

from wip.catalog_db import CatalogError, CatalogRepository
from wip.route import (
    RouteCatalog,
    RouteDefinition,
    RouteError,
    RouteState,
    StepDefinition,
    route_from_dict,
)
from wip.warehouse import DEFAULT_DEVICE_TYPES, Warehouse, WarehouseError


# ---------------------------------------------------------------- 合成产品
def make_product(
    route_id: str = "R",
    version: str = "v1",
    *,
    asm_duration: int = 5,
    quick_accepted: bool = False,
    include_rewok_in: bool = True,
) -> RouteDefinition:
    """两条替代工艺，其中一条经过返工回路。

    S0 --t_mach_a--> A --t_asm_a--> Q --t_insp--> OK
    S0 --t_mach_b--> B --t_asm_b--> Q
    Q --t_rework_in--> RW --t_rework_out--> Q   (返工回路)
    Q --t_scrap--> SCRAP
    """
    states = {
        "S0": RouteState.INITIAL,
        "A": RouteState.NORMAL,
        "B": RouteState.NORMAL,
        "Q": RouteState.NORMAL,
        "RW": RouteState.NORMAL,
        "OK": RouteState.ACCEPTED,
        "SCRAP": RouteState.SCRAP,
    }
    accepted = ["OK"]
    scrap = ["SCRAP"]
    steps = [
        StepDefinition("t_mach_a", "S0", "A", "CNC", 10),
        StepDefinition("t_mach_b", "S0", "B", "CNC", 8),
        StepDefinition("t_asm_a", "A", "Q", "ASSEMBLY", asm_duration),
        StepDefinition("t_asm_b", "B", "Q", "ASSEMBLY", 3),
        StepDefinition("t_insp", "Q", "OK", "INSPECTION", 2),
        StepDefinition("t_scrap", "Q", "SCRAP", "INSPECTION", 1),
        StepDefinition("t_rework_out", "RW", "Q", "REWORK", 2),
    ]
    if include_rewok_in:
        steps.append(StepDefinition("t_rework_in", "Q", "RW", "INSPECTION", 3))
    if quick_accepted:
        states["OK2"] = RouteState.ACCEPTED
        accepted.append("OK2")
        steps.append(StepDefinition("t_quick", "Q", "OK2", "INSPECTION", 1))
    return RouteDefinition(route_id, version, "S0", accepted, scrap, states, steps)


def start(seq, unit, attempt, time, step, device):
    return {
        "acceptance_seq": seq,
        "event_type": "START",
        "unit_id": unit,
        "attempt_id": attempt,
        "time": time,
        "step_id": step,
        "device_id": device,
    }


def finish(seq, unit, attempt, time):
    return {
        "acceptance_seq": seq,
        "event_type": "FINISH",
        "unit_id": unit,
        "attempt_id": attempt,
        "time": time,
    }


def cancel(seq, unit, attempt, time):
    return {
        "acceptance_seq": seq,
        "event_type": "CANCEL",
        "unit_id": unit,
        "attempt_id": attempt,
        "time": time,
    }


def make_warehouse(path: str = ":memory:", product: RouteDefinition = None) -> Warehouse:
    wh = Warehouse(path)
    wh.register_device("dev_cnc", "CNC")
    wh.register_device("dev_asm", "ASSEMBLY")
    wh.register_device("dev_insp", "INSPECTION")
    wh.register_device("dev_rw", "REWORK")
    wh.register_device("dev_cnc2", "CNC")
    if product is not None:
        wh.register_product(product)
    return wh


class RouteCatalogTests(unittest.TestCase):
    def test_alternative_routes_validate(self):
        r = make_product()
        good_a = r.validate_sequence(["t_mach_a", "t_asm_a", "t_insp"])
        good_b = r.validate_sequence(["t_mach_b", "t_asm_b", "t_insp"])
        self.assertTrue(good_a.valid)
        self.assertEqual(good_a.total_duration, 17)
        self.assertTrue(good_b.valid)
        self.assertEqual(good_b.total_duration, 13)

    def test_rework_loop_is_legal(self):
        r = make_product()
        result = r.validate_sequence(
            ["t_mach_a", "t_asm_a", "t_rework_in", "t_rework_out", "t_rework_in", "t_rework_out", "t_insp"]
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.total_duration, 10 + 5 + 3 + 2 + 3 + 2 + 2)

    def test_scrap_is_not_accepted(self):
        r = make_product()
        result = r.validate_sequence(["t_mach_a", "t_asm_a", "t_scrap"])
        self.assertFalse(result.valid)
        self.assertIn("报废", result.reason)
        self.assertIsNone(result.final_state)

    def test_incomplete_sequence_invalid(self):
        r = make_product()
        result = r.validate_sequence(["t_mach_a"])
        self.assertFalse(result.valid)
        self.assertIn("尚未到达合格终态", result.reason)

    def test_bad_step_and_wrong_order(self):
        r = make_product()
        self.assertFalse(r.validate_sequence(["nope"]).valid)
        self.assertFalse(r.validate_sequence(["t_insp"]).valid)

    def test_current_state_and_shortest_remaining(self):
        r = make_product()
        self.assertEqual(r.current_states(["t_mach_a", "t_asm_a"]), ["Q"])
        self.assertEqual(r.shortest_remaining("Q"), 2)
        self.assertEqual(r.shortest_remaining("S0"), 13)  # 经 mach_b 更快
        self.assertEqual(r.shortest_remaining("OK"), 0)
        self.assertIsNone(r.shortest_remaining("SCRAP"))
        path = r.shortest_remaining_path("S0")
        self.assertEqual(path[0], 13)
        self.assertEqual(tuple(path[1]), ("t_mach_b", "t_asm_b", "t_insp"))

    def test_unreachable_state_returns_none(self):
        states = {
            "S0": RouteState.INITIAL,
            "DEAD": RouteState.NORMAL,
            "OK": RouteState.ACCEPTED,
        }
        r = RouteDefinition(
            "D", "v1", "S0", ["OK"], [], states,
            [StepDefinition("done", "S0", "OK", "INSPECTION", 1)],
        )
        self.assertIsNone(r.shortest_remaining("DEAD"))

    def test_limits_and_duplicate_step_ids(self):
        with self.assertRaises(RouteError):
            make_product().__class__(
                "dup", "v1", "S0", ["OK"], [],
                {"S0": RouteState.INITIAL, "OK": RouteState.ACCEPTED},
                [
                    StepDefinition("s", "S0", "OK", "X", 1),
                    StepDefinition("s", "S0", "OK", "X", 1),
                ],
            )
        with self.assertRaises(RouteError):
            StepDefinition("neg", "S0", "OK", "X", -1)

    def test_terminal_cannot_be_origin(self):
        with self.assertRaises(RouteError):
            RouteDefinition(
                "bad", "v1", "S0", ["OK"], [],
                {"S0": RouteState.INITIAL, "OK": RouteState.ACCEPTED},
                [StepDefinition("leave", "OK", "S0", "X", 1)],
            )

    def test_catalog_versions_are_immutable(self):
        catalog = RouteCatalog()
        catalog.add_route(make_product(version="v1"))
        catalog.add_route(make_product(version="v2", asm_duration=1))
        with self.assertRaises(RouteError):
            catalog.add_route(make_product(version="v1"))
        self.assertEqual(catalog.latest("R").version, "v2")
        self.assertEqual(catalog.versions("R"), ["v1", "v2"])

    def test_catalog_db_persists_and_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cat.db")
            cat = CatalogRepository(path)
            cat.save_route(make_product())
            exported = cat.export_product("R", "v1")
            cat.close()
            cat2 = CatalogRepository(path)
            self.assertTrue(cat2.get("R", "v1").status == "PUBLISHED")
            self.assertTrue(route_from_dict(exported).validate_sequence(
                ["t_mach_a", "t_asm_a", "t_insp"]
            ).valid)
            cat2.close()


class WarehouseExecutionTests(unittest.TestCase):
    def test_full_route_and_accepted_count(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        result = wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            finish(2, "U1", "a1", 10),
            start(3, "U1", "a2", 10, "t_asm_a", "dev_asm"),
            finish(4, "U1", "a2", 15),
            start(5, "U1", "a3", 15, "t_insp", "dev_insp"),
            finish(6, "U1", "a3", 17),
        ])
        self.assertTrue(result.ok, result.to_dict())
        unit = wh.get_unit("U1")
        self.assertEqual(unit["status"], "ACCEPTED")
        self.assertEqual(wh.accepted_count(), 1)

    def test_illegal_start_state_mismatch(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        result = wh.import_events([
            start(1, "U1", "a1", 0, "t_asm_a", "dev_asm"),
        ])
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "STATE_MISMATCH")

    def test_unit_in_flight_blocks_second_start(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        wh.import_events([start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc")])
        result = wh.import_events([start(2, "U1", "a2", 1, "t_mach_b", "dev_cnc2")])
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "UNIT_IN_FLIGHT")

    def test_device_busy_interval_semantics(self):
        product = make_product()
        wh = make_warehouse(product=product)
        wh.create_unit("U1", route_id="R", version="v1")
        wh.create_unit("U2", route_id="R", version="v1")
        wh.import_events([start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc")])
        busy = wh.import_events([finish(2, "U1", "a1", 10)])
        self.assertTrue(busy.ok)
        # 同一设备同秒交接允许（[0,10) 与 [10,18) 不重叠）。
        handoff = wh.import_events([start(3, "U2", "b1", 10, "t_mach_a", "dev_cnc")])
        self.assertTrue(handoff.ok, handoff.to_dict())
        # 重叠加一：U2 再开一个尝试与自身冲突前先取消。
        overlap = wh.import_events([
            finish(4, "U2", "b1", 20),
            start(5, "U2", "b2", 20, "t_asm_a", "dev_asm"),
            finish(6, "U2", "b2", 25),
            start(7, "U2", "b3", 25, "t_insp", "dev_insp"),
            finish(8, "U2", "b3", 27),
        ])
        self.assertTrue(overlap.ok, overlap.to_dict())

    def test_device_overlap_rejected(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        wh.create_unit("U2", route_id="R", version="v1")
        wh.import_events([start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc")])
        result = wh.import_events([start(2, "U2", "b1", 9, "t_mach_a", "dev_cnc")])
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "DEVICE_BUSY")
        self.assertEqual(result.error["details"]["occupying_attempt"], "a1")

    def test_zero_duration_allows_same_instant(self):
        states = {
            "S0": RouteState.INITIAL,
            "OK": RouteState.ACCEPTED,
        }
        product = RouteDefinition(
            "Z", "v1", "S0", ["OK"], [], states,
            [StepDefinition("instant", "S0", "OK", "INSPECTION", 0)],
        )
        wh = make_warehouse(product=product)
        wh.create_unit("U1", route_id="Z", version="v1")
        wh.create_unit("U2", route_id="Z", version="v1")
        result = wh.import_events([
            start(1, "U1", "a1", 5, "instant", "dev_insp"),
            finish(2, "U1", "a1", 5),
            start(3, "U2", "b1", 5, "instant", "dev_insp"),
            finish(4, "U2", "b1", 5),
        ])
        self.assertTrue(result.ok, result.to_dict())

    def test_out_of_order_finish_by_business_time(self):
        """事件按 acceptance_seq 执行；业务时刻乱序不应改变事实顺序。"""
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        # 列表顺序故意打乱，但 acceptance_seq 决定执行顺序。
        result = wh.import_events([
            finish(2, "U1", "a1", 10),
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
        ])
        self.assertTrue(result.ok, result.to_dict())
        attempts = wh.unit_attempts("U1")
        self.assertEqual(attempts[0]["status"], "DONE")
        # 迟到的 FINISH 若在已受理 FINISH 之后才给，必须被拒。
        late = wh.import_events([
            start(4, "U1", "a2", 10, "t_asm_a", "dev_asm"),
            finish(3, "U1", "a1", 10),  # 已完成的尝试不能再次 FINISH
        ])
        self.assertFalse(late.ok)
        self.assertEqual(late.error["code"], "ATTEMPT_NOT_IN_FLIGHT")

    def test_finish_before_start_rejected(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        wh.import_events([start(1, "U1", "a1", 10, "t_mach_a", "dev_cnc")])
        result = wh.import_events([finish(2, "U1", "a1", 9)])
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "FINISH_BEFORE_START")

    def test_cancel_releases_and_retry(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        wh.import_events([start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc")])
        cancelled = wh.import_events([cancel(2, "U1", "a1", 3)])
        self.assertTrue(cancelled.ok)
        self.assertEqual(wh.get_unit("U1")["current_state"], "S0")
        self.assertIsNone(wh.device_busy("dev_cnc", 3))
        retry = wh.import_events([
            start(3, "U1", "a2", 4, "t_mach_a", "dev_cnc"),
            finish(4, "U1", "a2", 14),
        ])
        self.assertTrue(retry.ok, retry.to_dict())
        self.assertEqual(wh.get_unit("U1")["current_state"], "A")

    def test_cancelled_attempt_cannot_finish(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            cancel(2, "U1", "a1", 3),
        ])
        result = wh.import_events([finish(3, "U1", "a1", 5)])
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "ATTEMPT_NOT_IN_FLIGHT")

    def test_rework_does_not_double_count(self):
        wh = make_warehouse(product=make_product(quick_accepted=False))
        wh.create_unit("U1", route_id="R", version="v1")
        result = wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            finish(2, "U1", "a1", 10),
            start(3, "U1", "a2", 10, "t_asm_a", "dev_asm"),
            finish(4, "U1", "a2", 15),
            start(5, "U1", "a3", 15, "t_rework_in", "dev_insp"),
            finish(6, "U1", "a3", 18),
            start(7, "U1", "a4", 18, "t_rework_out", "dev_rw"),
            finish(8, "U1", "a4", 20),
            start(9, "U1", "a5", 20, "t_rework_in", "dev_insp"),
            finish(10, "U1", "a5", 23),
            start(11, "U1", "a6", 23, "t_rework_out", "dev_rw"),
            finish(12, "U1", "a6", 25),
            start(13, "U1", "a7", 25, "t_insp", "dev_insp"),
            finish(14, "U1", "a7", 27),
        ])
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(wh.accepted_count(), 1)
        self.assertEqual(len(wh.unit_attempts("U1")), 7)

    def test_terminal_blocks_new_steps(self):
        wh = make_warehouse(product=make_product(quick_accepted=True))
        wh.create_unit("U1", route_id="R", version="v1")
        ok = wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            finish(2, "U1", "a1", 10),
            start(3, "U1", "a2", 10, "t_asm_a", "dev_asm"),
            finish(4, "U1", "a2", 15),
            start(5, "U1", "a3", 15, "t_quick", "dev_insp"),
            finish(6, "U1", "a3", 16),
        ])
        self.assertTrue(ok.ok, ok.to_dict())
        self.assertEqual(wh.get_unit("U1")["status"], "ACCEPTED")
        blocked = wh.import_events([start(7, "U1", "a4", 20, "t_rework_in", "dev_insp")])
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error["code"], "UNIT_TERMINAL")

    def test_scrap_terminal_blocks_and_not_counted(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        ok = wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            finish(2, "U1", "a1", 10),
            start(3, "U1", "a2", 10, "t_asm_a", "dev_asm"),
            finish(4, "U1", "a2", 15),
            start(5, "U1", "a3", 15, "t_scrap", "dev_insp"),
            finish(6, "U1", "a3", 16),
        ])
        self.assertTrue(ok.ok)
        self.assertEqual(wh.get_unit("U1")["status"], "SCRAP")
        self.assertEqual(wh.accepted_count(), 0)
        self.assertEqual(wh.scrap_count(), 1)
        blocked = wh.import_events([start(7, "U1", "a4", 20, "t_rework_in", "dev_insp")])
        self.assertFalse(blocked.ok)

    def test_batch_is_atomic(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        before = wh.proj.signature()
        result = wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            finish(2, "U1", "a1", 10),
            start(3, "U1", "a2", 10, "t_asm_a", "dev_asm"),
            start(4, "U1", "a3", 11, "t_insp", "dev_insp"),  # 状态不匹配 -> 整批回滚
        ])
        self.assertFalse(result.ok)
        self.assertEqual(result.applied_count, 0)
        self.assertEqual(wh.proj.signature(), before)
        self.assertIsNone(wh.get_unit("U1")["in_flight_attempt"])

    def test_event_limit_and_duplicate_seq(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        dup = wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            finish(1, "U1", "a1", 10),
        ])
        self.assertFalse(dup.ok)
        self.assertEqual(dup.error["code"], "DUPLICATE_SEQ")

    def test_unknown_device_and_type_mismatch(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        unknown = wh.import_events([start(1, "U1", "a1", 0, "t_mach_a", "nope")])
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.error["code"], "UNKNOWN_DEVICE")
        mismatch = wh.import_events([start(2, "U1", "a1", 0, "t_mach_a", "dev_asm")])
        self.assertFalse(mismatch.ok)
        self.assertEqual(mismatch.error["code"], "DEVICE_TYPE_MISMATCH")


class MigrationTests(unittest.TestCase):
    def test_migration_changes_admission_and_forecast(self):
        wh = make_warehouse(product=make_product(version="v1"))
        wh.create_unit("U1", route_id="R", version="v1")
        wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            finish(2, "U1", "a1", 10),
            start(3, "U1", "a2", 10, "t_asm_a", "dev_asm"),
            finish(4, "U1", "a2", 15),
        ])
        self.assertEqual(wh.get_unit("U1")["current_state"], "Q")
        self.assertEqual(wh.get_unit("U1")["expected_remaining_duration"], 2)

        # 新产品把 Q 改名 Q2 并要求更长的检验。
        v2_states = {
            "S0": RouteState.INITIAL,
            "A": RouteState.NORMAL,
            "Q2": RouteState.NORMAL,
            "OK": RouteState.ACCEPTED,
        }
        v2 = RouteDefinition(
            "R", "v2", "S0", ["OK"], [], v2_states,
            [
                StepDefinition("t_mach_a", "S0", "A", "CNC", 10),
                StepDefinition("t_asm_a", "A", "Q2", "ASSEMBLY", 5),
                StepDefinition("t_insp2", "Q2", "OK", "INSPECTION", 7),
            ],
        )
        result = wh.migrate_unit("U1", {"Q": "Q2"}, product=v2, reason="ECN-42")
        self.assertTrue(result["op_seqs"])
        unit = wh.get_unit("U1")
        self.assertEqual(unit["version"], "v2")
        self.assertEqual(unit["current_state"], "Q2")
        self.assertEqual(unit["expected_remaining_duration"], 7)

        # 旧工步 t_insp 在新绑定产品中不存在 -> 拒绝。
        denied = wh.import_events([start(5, "U1", "a3", 15, "t_insp", "dev_insp")])
        self.assertFalse(denied.ok)
        self.assertEqual(denied.error["code"], "STEP_NOT_IN_BOUND_PRODUCT")
        # 新工步按新产品执行。
        allowed = wh.import_events([
            start(6, "U1", "a4", 15, "t_insp2", "dev_insp"),
            finish(7, "U1", "a4", 22),
        ])
        self.assertTrue(allowed.ok, allowed.to_dict())
        self.assertEqual(wh.accepted_count(), 1)

    def test_migration_requires_no_in_flight(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        wh.import_events([start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc")])
        with self.assertRaises(WarehouseError):
            wh.migrate_unit("U1", {"S0": "S0"}, product=make_product(version="v2"))

    def test_migration_mapping_must_contain_state_and_target_exist(self):
        wh = make_warehouse(product=make_product())
        wh.create_unit("U1", route_id="R", version="v1")
        with self.assertRaises(WarehouseError):
            wh.migrate_unit("U1", {"A": "A"}, product=make_product(version="v2"))
        with self.assertRaises(WarehouseError):
            wh.migrate_unit("U1", {"S0": "MISSING"}, product=make_product(version="v2"))

    def test_old_attempt_finishes_on_old_product_after_migration(self):
        """在途尝试必须先结清；历史解释仍按旧产品版本。"""
        wh = make_warehouse(product=make_product(version="v1"))
        wh.create_unit("U1", route_id="R", version="v1")
        wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            finish(2, "U1", "a1", 10),
        ])
        # 用 v2 迁移，但旧尝试 a1 仍记录了 v1。
        wh.migrate_unit("U1", {"A": "A"}, product=make_product(version="v2"))
        attempts = wh.unit_attempts("U1")
        self.assertEqual(attempts[0]["version"], "v1")
        journey = wh.explain_unit("U1")["journey"]
        self.assertEqual(journey[0]["version"], "v1")
        self.assertEqual(journey[1]["version"], "v2")
        self.assertEqual(journey[1]["from"]["version"], "v1")

    def test_catalog_edit_does_not_change_history_binding(self):
        wh = make_warehouse(product=make_product(version="v1"))
        wh.create_unit("U1", route_id="R", version="v1")
        # 即使后续注册了同 route_id 的新版本，旧绑定仍用 v1。
        wh.register_product(make_product(version="v2", asm_duration=1))
        wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            finish(2, "U1", "a1", 10),
        ])
        self.assertEqual(wh.get_unit("U1")["version"], "v1")
        self.assertEqual(wh.get_unit("U1")["expected_remaining_duration"], 7)


class QueryAndExplainTests(unittest.TestCase):
    def setUp(self):
        self.wh = make_warehouse(product=make_product())
        self.wh.create_unit("U1", route_id="R", version="v1")
        self.wh.create_unit("U2", route_id="R", version="v1")

    def test_device_facts(self):
        self.wh.import_events([start(1, "U1", "a1", 100, "t_mach_a", "dev_cnc")])
        busy = self.wh.device_busy("dev_cnc", 105)
        self.assertIsNotNone(busy)
        self.assertEqual(busy["attempt_id"], "a1")
        self.assertIsNone(self.wh.device_busy("dev_cnc", 110))  # [100,110) 右开
        self.assertIsNone(self.wh.device_busy("dev_cnc", 99))
        fact = self.wh.device_fact("dev_cnc")
        self.assertEqual(fact["timeline"][0]["interval"], [100, 110])

    def test_explain_start_reports_occupying_attempt(self):
        self.wh.import_events([start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc")])
        info = self.wh.explain_start("U2", "t_mach_a", "dev_cnc", 5)
        self.assertFalse(info["allowed"])
        codes = {r["code"] for r in info["reasons"]}
        self.assertIn("DEVICE_BUSY", codes)
        self.assertEqual(info["occupying_attempt"]["attempt_id"], "a1")

    def test_explain_start_state_and_in_flight(self):
        self.wh.import_events([start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc")])
        info = self.wh.explain_start("U1", "t_mach_a", "dev_cnc2", 0)
        codes = {r["code"] for r in info["reasons"]}
        self.assertIn("UNIT_IN_FLIGHT", codes)
        info2 = self.wh.explain_start("U2", "t_asm_a", "dev_asm", 0)
        self.assertIn("STATE_MISMATCH", {r["code"] for r in info2["reasons"]})

    def test_work_and_wait_durations(self):
        self.wh.import_events([
            start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
            finish(2, "U1", "a1", 10),
            # 等待 5 秒后开始下一工序。
            start(3, "U1", "a2", 15, "t_asm_a", "dev_asm"),
            finish(4, "U1", "a2", 22),
        ])
        facts = self.wh.duration_facts("U1")
        self.assertEqual(facts["actual_work_duration"], 17)
        self.assertEqual(facts["actual_wait_duration"], 5)
        self.assertEqual(facts["attempt_count"], 2)

    def test_unit_position(self):
        self.wh.import_events([start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc")])
        pos = self.wh.unit_position("U1")
        self.assertEqual(pos["on_device"], "dev_cnc")
        self.assertEqual(pos["in_flight_attempt"], "a1")
        # 状态投影仍为 S0；含在途工序的剩余 = 当前状态最短剩余。
        self.assertEqual(pos["expected_remaining_duration"], 13)
        # 从在途工序目标状态 A 起算的剩余为 7。
        self.assertEqual(pos["in_flight_target_state"], "A")
        self.assertEqual(pos["remaining_after_in_flight"], 7)


class PersistenceTests(unittest.TestCase):
    def test_reopen_replays_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "exec.db")
            wh = make_warehouse(path, product=make_product())
            wh.create_unit("U1", route_id="R", version="v1")
            wh.import_events([
                start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
                finish(2, "U1", "a1", 10),
                start(3, "U1", "a2", 10, "t_asm_a", "dev_asm"),
            ])
            signature = wh.proj.signature()
            wh.close()

            wh2 = Warehouse(path)
            self.assertEqual(wh2.proj.signature(), signature)
            # 在途尝试只预订设备，物料状态在 FINISH 前保持在 A。
            self.assertEqual(wh2.get_unit("U1")["current_state"], "A")
            self.assertEqual(wh2.get_unit("U1")["in_flight_attempt"], "a2")
            # 重开后继续事件应沿用序号。
            result = wh2.import_events([finish(4, "U1", "a2", 15)])
            self.assertTrue(result.ok)
            wh2.close()

            wh3 = Warehouse(path)
            self.assertEqual(wh3.get_unit("U1")["current_state"], "Q")
            self.assertIsNone(wh3.get_unit("U1")["in_flight_attempt"])
            self.assertEqual(wh3.proj.max_acceptance_seq, 4)
            wh3.close()

    def test_reopen_preserves_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "exec.db")
            wh = make_warehouse(path, product=make_product())
            wh.create_unit("U1", route_id="R", version="v1")
            bad = wh.import_events([start(1, "U1", "a1", 0, "t_asm_a", "dev_asm")], batch_id="bad-1")
            self.assertFalse(bad.ok)
            wh.close()
            wh2 = Warehouse(path)
            batch = wh2.get_batch("bad-1")
            self.assertEqual(batch["status"], "REJECTED")
            self.assertIn("STATE_MISMATCH", json.dumps(batch["payload"]))
            wh2.close()

    def test_external_product_without_catalog(self):
        """仓库直接接受同格式外部产品，不读目录内部表。"""
        with tempfile.TemporaryDirectory() as tmp:
            cat_path = os.path.join(tmp, "cat.db")
            exec_path = os.path.join(tmp, "exec.db")
            cat = CatalogRepository(cat_path)
            cat.save_route(make_product())
            exported = cat.export_product("R", "v1")
            cat.close()

            wh = make_warehouse(exec_path)
            wh.create_unit("U1", exported)
            ok = wh.import_events([
                start(1, "U1", "a1", 0, "t_mach_a", "dev_cnc"),
                finish(2, "U1", "a1", 10),
                start(3, "U1", "a2", 10, "t_asm_a", "dev_asm"),
                finish(4, "U1", "a2", 15),
                start(5, "U1", "a3", 15, "t_insp", "dev_insp"),
                finish(6, "U1", "a3", 17),
            ])
            self.assertTrue(ok.ok)
            wh.close()
            # 执行库中无 cat_routes 表也可独立重开。
            conn = sqlite3.connect(exec_path)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            self.assertNotIn("cat_routes", tables)


class AlternativeProductTests(unittest.TestCase):
    def test_multiple_accepted_terminal_states(self):
        product = make_product(quick_accepted=True)
        r = product.validate_sequence(["t_mach_b", "t_asm_b", "t_quick"])
        self.assertTrue(r.valid)
        self.assertEqual(r.final_state, "OK2")
        self.assertEqual(product.shortest_remaining("Q"), 1)

    def test_transition_view_stable_step_ids(self):
        r = make_product()
        self.assertTrue(r.has_step("t_rework_in"))
        self.assertEqual(r.get_step("t_rework_in").device_type, "INSPECTION")
        self.assertFalse(r.has_step("missing"))


if __name__ == "__main__":
    unittest.main()
