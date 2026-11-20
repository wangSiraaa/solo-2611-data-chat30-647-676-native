"""The five scenario cases required by the task statement, end to end:

1. area sufficient but shape does not fit
2. rotation is required to fit
3. temperature-zone conflict
4. empty-box weight causes vehicle/box overload
5. two plans compete for the same sealed box
"""
import threading

import pytest

from coldchain import (BoxType, Item, PackingInfeasibleError,
                       ReservationConflictError, optimize)
from coldchain.models import InfeasibilityReport
from coldchain.transport import ShippableBox, optimize_transport
from conftest import make_sealed_box


def test_scenario_area_sufficient_but_shape_infeasible():
    # 5x5 box has area 25 >= 24, yet two 4x3 items cannot share it.
    items = [Item("P1", 4, 3, 1, "Z"), Item("P2", 4, 3, 1, "Z")]
    types = [BoxType("B", 5, 5, 0, 100, frozenset({"Z"}))]
    plan = optimize(items, types)
    assert plan.box_count == 2
    assert plan.total_area == 50  # not 25: area-only reasoning would say 1


def test_scenario_rotation_required(svc):
    svc.register_box_type("B", 3, 5, 1, 100, {"Z"})
    svc.register_item("R1", 5, 2, 2, "Z")
    plan = svc.plan_packing()
    assert plan.box_count == 1
    pl = plan.boxes[0].placements[0]
    assert pl.rotated == 1  # only fits rotated
    sealed = svc.sealing.seal(plan)
    assert sealed[0].items[0].rotated == 1


def test_scenario_zone_conflict(svc):
    svc.register_box_type("B", 6, 6, 1, 100, {"CHILLED", "FROZEN"})
    svc.register_item("Z1", 2, 2, 2, "CHILLED")
    svc.register_item("Z2", 2, 2, 2, "FROZEN")
    plan = svc.plan_packing()
    # same box type could hold both physically, but zones must not mix
    assert plan.box_count == 2
    assert {b.zone for b in plan.boxes} == {"CHILLED", "FROZEN"}
    # and a zone no box type allows makes packing infeasible
    svc.register_item("Z3", 2, 2, 2, "DEEP")
    with pytest.raises(PackingInfeasibleError):
        svc.plan_packing()


def test_scenario_empty_box_weight_overload():
    # vehicle capacity 21 fits the net item weight (2x10=20) but not the
    # gross weight including empty boxes (2x(10+6)=32); even one box
    # (10+6=16) fits, so exactly one vehicle is overloaded away.
    boxes = [ShippableBox("B1", "Z", 16, "ST1"),
             ShippableBox("B2", "Z", 16, "ST1")]
    from coldchain import Shift
    one = [Shift("T", frozenset({"Z"}), 21, 5, 10, 1)]
    rep = optimize_transport(boxes, one)
    assert isinstance(rep, InfeasibilityReport)
    assert rep.total_gross_weight == 32
    two = [Shift("T1", frozenset({"Z"}), 21, 5, 10, 1),
           Shift("T2", frozenset({"Z"}), 21, 5, 10, 1)]
    sol = optimize_transport(boxes, two)
    assert sol.vehicle_count == 2


def test_scenario_empty_box_weight_forces_more_boxes(svc):
    # box max gross 15, empty weight 6: two 5-weight items (10+6=16)
    # cannot share one box although their net weight 10 <= 15.
    svc.register_box_type("B", 8, 8, 6, 15, {"Z"})
    svc.register_item("W1", 2, 2, 5, "Z")
    svc.register_item("W2", 2, 2, 5, "Z")
    plan = svc.plan_packing()
    assert plan.box_count == 2
    sealed = svc.sealing.seal(plan)
    assert all(b.gross_weight == 11 for b in sealed)  # 5 + 6 empty


def test_scenario_two_plans_compete_for_same_box(svc):
    svc.register_box_type("B", 6, 6, 2, 50, {"Z"})
    svc.register_item("U1", 2, 2, 5, "Z")
    svc.register_item("U2", 2, 2, 5, "Z")
    svc.register_shift("T1", {"Z"}, 50, 5, 10, 2)
    svc.register_shift("T2", {"Z"}, 50, 5, 10, 2)
    shared = make_sealed_box(svc, "B", "Z", [("U1", 0, 0, 0)])
    other = make_sealed_box(svc, "B", "Z", [("U2", 0, 0, 0)])

    pa = svc.plan_transport({shared.sealed_box_id: "S1"}, ["T1"])
    pb = svc.plan_transport({shared.sealed_box_id: "S2",
                             other.sealed_box_id: "S2"}, ["T2"])
    barrier = threading.Barrier(2)
    outcomes = {"ok": [], "conflict": []}

    def confirm(pid):
        try:
            barrier.wait(timeout=10)
            svc.confirm(pid, expected_version=1)
            outcomes["ok"].append(pid)
        except ReservationConflictError:
            outcomes["conflict"].append(pid)

    t1 = threading.Thread(target=confirm, args=(pa.plan_id,))
    t2 = threading.Thread(target=confirm, args=(pb.plan_id,))
    t1.start(); t2.start(); t1.join(20); t2.join(20)

    assert len(outcomes["ok"]) == 1
    assert len(outcomes["conflict"]) == 1
    # the shared box is reserved exactly once — by the winner
    with svc.repo.read() as con:
        rows = list(con.execute(
            "SELECT plan_id FROM box_reservations WHERE sealed_box_id=?",
            (shared.sealed_box_id,)))
    assert len(rows) == 1
    assert rows[0][0] == outcomes["ok"][0]
