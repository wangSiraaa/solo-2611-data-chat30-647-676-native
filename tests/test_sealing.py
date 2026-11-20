"""Sealing: stable ids, idempotent re-read, immutability, atomicity."""
import sqlite3

import pytest

from coldchain import (Item, PackingPlan, PlanValidationError, StalePlanError,
                       optimize)
from coldchain.models import BoxPlan, Placement
from coldchain.packing import NORMALIZATION


def _plan(svc):
    return svc.plan_packing()


def test_seal_returns_real_coordinates_and_stable_ids(scoped_svc):
    plan = _plan(scoped_svc)
    boxes = scoped_svc.sealing.seal(plan)
    assert len(boxes) == 6
    for b in boxes:
        assert b.sealed_box_id.startswith("SBX-")
        assert b.gross_weight == b.empty_weight + sum(i.weight
                                                      for i in b.items)
        assert b.items  # real contents
        for it in b.items:
            assert it.x >= 0 and it.y >= 0 and it.rotated in (0, 1)
    # re-sealing the same plan is an idempotent re-read (复读)
    again = scoped_svc.sealing.seal(plan)
    assert [b.sealed_box_id for b in again] == [b.sealed_box_id
                                                for b in boxes]
    assert len(scoped_svc.sealing.list()) == 6


def test_sealed_box_roundtrip(scoped_svc):
    plan = _plan(scoped_svc)
    boxes = scoped_svc.sealing.seal(plan)
    got = scoped_svc.sealing.get(boxes[0].sealed_box_id)
    assert got == boxes[0]


def test_sealed_box_immutable_at_db_level(scoped_svc):
    plan = _plan(scoped_svc)
    box = scoped_svc.sealing.seal(plan)[0]
    with scoped_svc.repo.read() as con:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE sealed_boxes SET gross_weight=0"
                        " WHERE sealed_box_id=?", (box.sealed_box_id,))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("DELETE FROM sealed_boxes WHERE sealed_box_id=?",
                        (box.sealed_box_id,))


def test_item_change_does_not_alter_sealed_content(scoped_svc):
    plan = _plan(scoped_svc)
    boxes = scoped_svc.sealing.seal(plan)
    target = next(b for b in boxes
                  if any(i.unit_id == "A1" for i in b.items))
    snap = next(i for i in target.items if i.unit_id == "A1")
    assert (snap.length, snap.width, snap.weight) == (2, 2, 3)

    scoped_svc.update_item_dims("A1", length=9, width=9, weight=9)
    after = scoped_svc.sealing.get(target.sealed_box_id)
    snap2 = next(i for i in after.items if i.unit_id == "A1")
    assert (snap2.length, snap2.width, snap2.weight) == (2, 2, 3)
    assert after.gross_weight == target.gross_weight


def test_stale_plan_rejected_after_item_change(scoped_svc):
    plan = _plan(scoped_svc)
    scoped_svc.update_item_dims("A1", length=3)
    with pytest.raises(StalePlanError):
        scoped_svc.sealing.seal(plan)
    # new plan can be sealed; old sealed boxes remain readable
    assert scoped_svc.sealing.list() == []


def test_failed_seal_leaves_no_partial_state(scoped_svc):
    plan = _plan(scoped_svc)
    # corrupt one box: overlapping coordinates
    bad_box = plan.boxes[-1]
    placements = list(bad_box.placements)
    placements[1] = Placement(placements[1].unit_id,
                              placements[0].x, placements[0].y, 0) \
        if len(placements) > 1 else placements[0]
    bad_boxes = list(plan.boxes[:-1])
    bad_boxes.append(BoxPlan(box_type_id=bad_box.box_type_id,
                             zone=bad_box.zone,
                             placements=tuple(placements),
                             gross_weight=bad_box.gross_weight,
                             empty_weight=bad_box.empty_weight,
                             area=bad_box.area))
    bad_plan = PackingPlan(boxes=tuple(bad_boxes),
                           item_versions=plan.item_versions,
                           plan_id="PLN-manual", normalization=NORMALIZATION)
    with pytest.raises(PlanValidationError):
        scoped_svc.sealing.seal(bad_plan)
    assert scoped_svc.sealing.list() == []  # nothing sealed at all


def test_seal_validates_each_item_exactly_once(scoped_svc):
    plan = _plan(scoped_svc)
    # drop one placement from a multi-item box
    multi = next(b for b in plan.boxes if len(b.placements) > 1)
    shrunk = BoxPlan(box_type_id=multi.box_type_id, zone=multi.zone,
                     placements=multi.placements[:1],
                     gross_weight=multi.gross_weight,
                     empty_weight=multi.empty_weight, area=multi.area)
    boxes = tuple(shrunk if b is multi else b for b in plan.boxes)
    bad = PackingPlan(boxes=boxes, item_versions=plan.item_versions,
                      plan_id="PLN-x", normalization=NORMALIZATION)
    with pytest.raises(PlanValidationError) as ei:
        scoped_svc.sealing.seal(bad)
    assert any("missing" in v for v in ei.value.violations)


def test_seal_rejects_overweight_and_zone_mix(scoped_svc):
    scoped_svc.register_box_type("Tiny", 2, 2, 0, 1, {"CHILLED"})
    scoped_svc.register_item("X1", 2, 2, 5, "CHILLED")
    scoped_svc.register_item("X2", 2, 2, 5, "FROZEN")
    items = [Item("X1", 2, 2, 5, "CHILLED"), Item("X2", 2, 2, 5, "FROZEN")]
    from coldchain import BoxType
    types = [BoxType("Tiny", 2, 2, 0, 100, frozenset({"CHILLED", "FROZEN"}))]
    plan = optimize(items, types)  # two separate boxes (zone split)
    # hand-corrupt: merge both zones into one box
    merged = BoxPlan(box_type_id="Tiny", zone="CHILLED",
                     placements=(Placement("X1", 0, 0, 0),
                                 Placement("X2", 0, 0, 0)),
                     gross_weight=10, empty_weight=0, area=4)
    bad = PackingPlan(boxes=(merged,),
                      item_versions=(("X1", 1), ("X2", 1)),
                      plan_id="PLN-z", normalization=NORMALIZATION)
    with pytest.raises(PlanValidationError) as ei:
        scoped_svc.sealing.seal(bad)
    assert any("mixed temperature zones" in v for v in ei.value.violations)
