"""Concurrency & lifecycle: atomic confirm, cancel, depart, replace."""
import threading

import pytest

from coldchain import (InvalidStateError, PlanStatus, ReservationConflictError,
                       VersionConflictError)
from coldchain.models import InfeasibilityReport
from conftest import make_sealed_box


@pytest.fixture()
def three_boxes(svc):
    svc.register_box_type("M", 6, 6, 2, 50, {"Z"})
    svc.register_item("U1", 2, 2, 5, "Z")
    svc.register_item("U2", 2, 2, 5, "Z")
    svc.register_item("U3", 2, 2, 5, "Z")
    svc.register_shift("T1", {"Z"}, 50, 5, 10, 2)
    svc.register_shift("T2", {"Z"}, 50, 5, 10, 2)
    svc.register_shift("T3", {"Z"}, 50, 5, 10, 2)
    b1 = make_sealed_box(svc, "M", "Z", [("U1", 0, 0, 0)])
    b2 = make_sealed_box(svc, "M", "Z", [("U2", 0, 0, 0)])
    b3 = make_sealed_box(svc, "M", "Z", [("U3", 0, 0, 0)])
    return svc, b1, b2, b3


def _plan(svc, shipments, shifts=None):
    tp = svc.plan_transport(shipments, shifts)
    assert not isinstance(tp, InfeasibilityReport)
    return tp


def test_concurrent_confirm_same_box_exactly_one_wins(three_boxes):
    svc, b1, b2, b3 = three_boxes
    pa = _plan(svc, {b1.sealed_box_id: "S1", b2.sealed_box_id: "S1"},
               ["T1", "T2"])
    pb = _plan(svc, {b2.sealed_box_id: "S2", b3.sealed_box_id: "S2"},
               ["T2", "T3"])
    barrier = threading.Barrier(2)
    results, errors = [], []

    def confirm(pid):
        try:
            barrier.wait(timeout=10)
            results.append(svc.confirm(pid, expected_version=1))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ta = threading.Thread(target=confirm, args=(pa.plan_id,))
    tb = threading.Thread(target=confirm, args=(pb.plan_id,))
    ta.start(); tb.start(); ta.join(20); tb.join(20)

    assert len(results) == 1 and len(errors) == 1
    assert isinstance(errors[0], ReservationConflictError)
    assert b2.sealed_box_id in errors[0].boxes
    # loser holds nothing: exactly one plan CONFIRMED, other still PLANNED
    with svc.repo.read() as con:
        plans = svc.repo.all_transport_plans(con)
    status = {p.plan_id: p.status for p in plans}
    assert sorted(s.value for s in status.values()) == [
        PlanStatus.CONFIRMED.value, PlanStatus.PLANNED.value]
        # loser's reservations absent
    loser = pb.plan_id if results[0].plan_id == pa.plan_id else pa.plan_id
    with svc.repo.read() as con:
        rows = list(con.execute(
            "SELECT plan_id FROM box_reservations"))
    assert {r[0] for r in rows} == {results[0].plan_id}
    assert loser not in {r[0] for r in rows}


def test_concurrent_confirm_shared_shift_only(three_boxes):
    svc, b1, b2, b3 = three_boxes
    pa = _plan(svc, {b1.sealed_box_id: "S1"}, ["T1"])
    pb = _plan(svc, {b2.sealed_box_id: "S2"}, ["T1"])  # same shift
    barrier = threading.Barrier(2)
    results, errors = [], []

    def confirm(pid):
        try:
            barrier.wait(timeout=10)
            results.append(svc.confirm(pid, expected_version=1))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ta = threading.Thread(target=confirm, args=(pa.plan_id,))
    tb = threading.Thread(target=confirm, args=(pb.plan_id,))
    ta.start(); tb.start(); ta.join(20); tb.join(20)
    assert len(results) == 1 and len(errors) == 1
    assert isinstance(errors[0], ReservationConflictError)
    assert errors[0].shifts == ["T1"]


def test_non_overlapping_plans_both_confirm(three_boxes):
    svc, b1, b2, b3 = three_boxes
    pa = _plan(svc, {b1.sealed_box_id: "S1"}, ["T1"])
    pb = _plan(svc, {b2.sealed_box_id: "S2"}, ["T2"])
    svc.confirm(pa.plan_id, 1)
    svc.confirm(pb.plan_id, 1)
    assert svc.get_transport_plan(pa.plan_id).status == PlanStatus.CONFIRMED
    assert svc.get_transport_plan(pb.plan_id).status == PlanStatus.CONFIRMED


def test_cancel_releases_reservations(three_boxes):
    svc, b1, b2, b3 = three_boxes
    pa = _plan(svc, {b1.sealed_box_id: "S1"}, ["T1"])
    svc.confirm(pa.plan_id, 1)
    svc.cancel(pa.plan_id, 2)
    assert svc.get_transport_plan(pa.plan_id).status == PlanStatus.CANCELLED
    # the same box and shift are free again
    pb = _plan(svc, {b1.sealed_box_id: "S9"}, ["T1"])
    svc.confirm(pb.plan_id, 1)
    assert svc.get_transport_plan(pb.plan_id).status == PlanStatus.CONFIRMED


def test_departed_plan_cannot_be_cancelled_or_reconfirmed(three_boxes):
    svc, b1, b2, b3 = three_boxes
    pa = _plan(svc, {b1.sealed_box_id: "S1"}, ["T1"])
    svc.confirm(pa.plan_id, 1)
    svc.depart(pa.plan_id, 2)
    assert svc.get_transport_plan(pa.plan_id).status == PlanStatus.DEPARTED
    with pytest.raises(InvalidStateError):
        svc.cancel(pa.plan_id, 3)
    # departed box stays reserved: a new plan using it cannot confirm
    pb = _plan(svc, {b1.sealed_box_id: "S2"}, ["T2"])
    with pytest.raises(ReservationConflictError):
        svc.confirm(pb.plan_id, 1)


def test_version_conflict_detected(three_boxes):
    svc, b1, b2, b3 = three_boxes
    pa = _plan(svc, {b1.sealed_box_id: "S1"}, ["T1"])
    with pytest.raises(VersionConflictError):
        svc.confirm(pa.plan_id, 99)
    svc.confirm(pa.plan_id, 1)
    with pytest.raises(VersionConflictError):
        svc.depart(pa.plan_id, 1)  # version already advanced to 2


def test_replace_propagates_weight_and_box_count(scoped_svc):
    svc = scoped_svc
    plan = svc.plan_packing()
    sealed = svc.sealing.seal(plan)
    assert len(sealed) == 6
    stores = {b.sealed_box_id: "ST-1" for b in sealed}
    tp = svc.plan_transport(stores)
    svc.confirm(tp.plan_id, 1)

    # change the goods: slimmer chilled items + heavier frozen ones.
    # The old plan becomes stale; a new packing product has 5 boxes
    # (all chilled now share one S box) with different gross weights.
    for uid in ("A1", "A2", "B1", "B2"):
        svc.update_item_dims(uid, length=2, width=1)
    svc.update_item_dims("C1", weight=6)
    svc.update_item_dims("C2", weight=6)
    new_plan = svc.plan_packing()
    new_sealed = svc.sealing.seal(new_plan)
    assert len(new_sealed) == 5
    assert {b.sealed_box_id for b in new_sealed} != \
           {b.sealed_box_id for b in sealed}

    new_stores = {b.sealed_box_id: "ST-1" for b in new_sealed}
    replaced = svc.replace(tp.plan_id, new_stores, expected_version=2)
    assert replaced.status == PlanStatus.CONFIRMED
    assert len(replaced.assignments) == 5  # box-count change propagated
    assert svc.get_transport_plan(tp.plan_id).status == PlanStatus.REPLACED
    # new plan reads the new sealed gross weights
    with svc.repo.read() as con:
        for a in replaced.assignments:
            box = svc.repo.get_sealed_box(con, a.sealed_box_id)
            assert a.gross_weight == box.gross_weight
            assert box.gross_weight == box.empty_weight + sum(
                i.weight for i in box.items)
    # heavier frozen boxes really weigh more now
    new_frozen = [b for b in new_sealed if b.zone == "FROZEN"]
    assert all(b.gross_weight == 12 for b in new_frozen)
    # old sealed boxes are still intact and readable
    for b in sealed:
        assert svc.sealing.get(b.sealed_box_id).gross_weight == \
               b.gross_weight


def test_replace_departed_plan_rejected(scoped_svc):
    svc = scoped_svc
    plan = svc.plan_packing()
    sealed = svc.sealing.seal(plan)
    stores = {b.sealed_box_id: "ST-1" for b in sealed}
    tp = svc.plan_transport(stores)
    svc.confirm(tp.plan_id, 1)
    svc.depart(tp.plan_id, 2)
    with pytest.raises(InvalidStateError):
        svc.replace(tp.plan_id, stores, expected_version=3)
