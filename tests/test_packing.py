"""Packing optimizer: optimality hierarchy, canonical form, cross-checks."""
import random

import pytest

from coldchain import BoxType, Item, PackingInfeasibleError, optimize
from conftest import naive_best_packing


def mk_items(specs):
    return [Item(uid, l, w, wt, zone) for uid, l, w, wt, zone in specs]


def test_min_box_count_first():
    # all four fit one M box; optimizer must not use two smaller boxes
    items = mk_items([("A", 2, 2, 1, "Z"), ("B", 2, 2, 1, "Z"),
                      ("C", 2, 2, 1, "Z"), ("D", 2, 2, 1, "Z")])
    types = [BoxType("S", 2, 2, 0, 100, frozenset({"Z"})),
             BoxType("M", 4, 4, 0, 100, frozenset({"Z"}))]
    plan = optimize(items, types)
    assert plan.box_count == 1
    assert plan.boxes[0].box_type_id == "M"


def test_area_minimized_given_box_count():
    # weight forces two boxes (60+60 > 100); S has smaller area than M
    items = mk_items([("A", 3, 3, 60, "Z"), ("B", 3, 3, 60, "Z")])
    types = [BoxType("S", 3, 3, 0, 100, frozenset({"Z"})),
             BoxType("M", 6, 6, 0, 100, frozenset({"Z"}))]
    plan = optimize(items, types)
    assert plan.box_count == 2
    assert all(b.box_type_id == "S" for b in plan.boxes)
    assert plan.total_area == 18


def test_each_item_exactly_once_and_weights():
    items = mk_items([("A", 2, 2, 3, "Z"), ("B", 3, 2, 4, "Z"),
                      ("C", 1, 2, 5, "Z")])
    types = [BoxType("M", 6, 6, 7, 100, frozenset({"Z"}))]
    plan = optimize(items, types)
    placed = [p.unit_id for b in plan.boxes for p in b.placements]
    assert sorted(placed) == ["A", "B", "C"]
    for b in plan.boxes:
        expect = 7 + sum(it.weight for it in items
                         if it.unit_id in {p.unit_id
                                           for p in b.placements})
        assert b.gross_weight == expect


def test_zone_conflict_forces_split():
    # one box type accepts both zones, but a box may hold only one zone
    items = mk_items([("A", 2, 2, 1, "COLD"), ("B", 2, 2, 1, "FROZEN")])
    types = [BoxType("M", 8, 8, 0, 100, frozenset({"COLD", "FROZEN"}))]
    plan = optimize(items, types)
    assert plan.box_count == 2
    assert {b.zone for b in plan.boxes} == {"COLD", "FROZEN"}


def test_zone_not_allowed_by_any_box_is_infeasible():
    items = mk_items([("A", 2, 2, 1, "DEEP")])
    types = [BoxType("M", 8, 8, 0, 100, frozenset({"COLD"}))]
    with pytest.raises(PackingInfeasibleError) as ei:
        optimize(items, types)
    assert any("A" in u for u in ei.value.unmet)


def test_empty_box_weight_forces_extra_box():
    # items total 10; box max gross 15. Empty weight 6 -> one box holds
    # only 10+6=16 > 15, so two boxes are needed. With empty weight 5,
    # one box suffices (15 <= 15).
    items = mk_items([("A", 2, 2, 5, "Z"), ("B", 2, 2, 5, "Z")])
    heavy = [BoxType("M", 8, 8, 6, 15, frozenset({"Z"}))]
    light = [BoxType("M", 8, 8, 5, 15, frozenset({"Z"}))]
    assert optimize(items, heavy).box_count == 2
    assert optimize(items, light).box_count == 1
    for b in optimize(items, heavy).boxes:
        assert b.gross_weight <= 15  # empty weight really counted


def test_rotation_end_to_end():
    items = mk_items([("A", 5, 2, 1, "Z")])
    types = [BoxType("M", 3, 5, 0, 100, frozenset({"Z"}))]
    plan = optimize(items, types)
    assert plan.box_count == 1
    assert plan.boxes[0].placements[0].rotated == 1


def test_shape_infeasible_despite_area():
    # total item area 24 <= 25 but the shapes cannot tile a 5x5 box
    items = mk_items([("A", 4, 3, 1, "Z"), ("B", 4, 3, 1, "Z")])
    types = [BoxType("M", 5, 5, 0, 100, frozenset({"Z"}))]
    plan = optimize(items, types)
    assert plan.box_count == 2  # area alone would wrongly say 1


def test_canonical_determinism_and_input_order_invariance():
    specs = [("A", 2, 2, 1, "Z"), ("B", 3, 2, 1, "Z"),
             ("C", 2, 3, 1, "Z"), ("D", 1, 2, 1, "Z")]
    types = [BoxType("M", 6, 6, 0, 100, frozenset({"Z"})),
             BoxType("S", 3, 3, 0, 100, frozenset({"Z"}))]
    p1 = optimize(mk_items(specs), types)
    p2 = optimize(mk_items(list(reversed(specs))), types)
    assert p1.plan_id == p2.plan_id
    assert p1 == p2


def test_canonical_tiebreak_picks_lexicographically_smaller():
    # two symmetric boxes: the plan key must be the lexicographically
    # smallest assignment of items to (identical) boxes
    items = mk_items([("A", 2, 2, 1, "Z"), ("B", 2, 2, 1, "Z")])
    types = [BoxType("M", 2, 2, 0, 100, frozenset({"Z"}))]
    plan = optimize(items, types)
    assert plan.box_count == 2
    assert plan.boxes[0].placements[0].unit_id == "A"
    assert plan.boxes[1].placements[0].unit_id == "B"


def test_cross_check_against_independent_enumeration():
    rng = random.Random(20261118)
    for trial in range(30):
        n = rng.randint(2, 6)
        specs = []
        for i in range(n):
            specs.append((f"U{i}", rng.randint(1, 4), rng.randint(1, 4),
                          rng.randint(1, 5),
                          rng.choice(["Z1", "Z1", "Z2"])))
        types = [
            BoxType("T1", rng.randint(3, 6), rng.randint(3, 6),
                    rng.randint(0, 3), rng.randint(8, 20),
                    frozenset({"Z1", "Z2"})),
            BoxType("T2", rng.randint(3, 7), rng.randint(3, 7),
                    rng.randint(0, 3), rng.randint(8, 24),
                    frozenset({"Z1"})),
        ]
        items = mk_items(specs)
        naive = naive_best_packing(items, types)
        try:
            plan = optimize(items, types)
        except PackingInfeasibleError:
            assert naive is None, f"trial {trial}: optimizer infeasible " \
                                  f"but naive found {naive}"
            continue
        assert naive is not None, f"trial {trial}: naive infeasible"
        assert (plan.box_count, plan.total_area) == naive, \
            f"trial {trial}: got {(plan.box_count, plan.total_area)}, " \
            f"naive {naive}"
