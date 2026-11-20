"""Transport optimizer: cost model, tie-breaks, constraints, reports."""
from coldchain import Shift
from coldchain.models import InfeasibilityReport
from coldchain.transport import ShippableBox, optimize_transport
from conftest import naive_best_transport


def sh(shift_id, zones, max_w, max_n, fixed, stop):
    return Shift(shift_id=shift_id, allowed_zones=frozenset(zones),
                 max_gross_weight=max_w, max_boxes=max_n,
                 fixed_fee=fixed, stop_fee=stop)


def bx(bid, zone, gross, store):
    return ShippableBox(sealed_box_id=bid, zone=zone, gross_weight=gross,
                        store_id=store)


def naive_shifts(shifts):
    return [(s.shift_id, set(s.allowed_zones), s.max_gross_weight,
             s.max_boxes, s.fixed_fee, s.stop_fee) for s in shifts]


def naive_boxes(boxes):
    return [(b.sealed_box_id, b.zone, b.gross_weight, b.store_id)
            for b in boxes]


def test_cost_is_fixed_plus_distinct_stops():
    boxes = [bx("B1", "Z", 1, "ST1"), bx("B2", "Z", 1, "ST1"),
             bx("B3", "Z", 1, "ST2")]
    shifts = [sh("T", {"Z"}, 100, 10, 50, 7)]
    sol = optimize_transport(boxes, shifts)
    assert sol.total_cost == 50 + 2 * 7  # two distinct stores
    assert sol.vehicle_count == 1


def test_cheaper_two_vehicles_beat_one_expensive():
    boxes = [bx("B1", "Z", 10, "ST1"), bx("B2", "Z", 10, "ST2")]
    shifts = [sh("BIG", {"Z"}, 20, 2, 100, 5),
              sh("C1", {"Z"}, 10, 1, 10, 5),
              sh("C2", {"Z"}, 10, 1, 10, 5)]
    sol = optimize_transport(boxes, shifts)
    # 2 cheap vehicles: (10+5)+(10+5) = 30 < 100+2*5 = 110
    assert sol.total_cost == 30
    assert sol.vehicle_count == 2


def test_fewer_vehicles_breaks_cost_tie():
    boxes = [bx("B1", "Z", 10, "ST1"), bx("B2", "Z", 10, "ST1")]
    shifts = [sh("ONE", {"Z"}, 20, 2, 20, 0),   # 20 for both
              sh("X1", {"Z"}, 10, 1, 10, 0),
              sh("X2", {"Z"}, 10, 1, 10, 0)]    # 10+10 = 20 tie
    sol = optimize_transport(boxes, shifts)
    assert sol.total_cost == 20
    assert sol.vehicle_count == 1
    assert sol.shift_vector() == ("ONE", "ONE")


def test_shift_vector_breaks_remaining_tie():
    # identical shifts A and B (same fees): vector ("A","B") < ("B","A")
    boxes = [bx("B1", "Z", 10, "ST1"), bx("B2", "Z", 10, "ST1")]
    shifts = [sh("SA", {"Z"}, 10, 1, 10, 0),
              sh("SB", {"Z"}, 10, 1, 10, 0)]
    sol = optimize_transport(boxes, shifts)
    assert sol.vehicle_count == 2
    assert sol.shift_vector() == ("SA", "SB")  # boxes sorted by id


def test_zone_weight_and_count_constraints():
    boxes = [bx("B1", "FROZEN", 15, "ST1"), bx("B2", "CHILLED", 10, "ST1")]
    shifts = [sh("T1", {"CHILLED"}, 100, 5, 10, 1),
              sh("T2", {"FROZEN"}, 20, 5, 10, 1)]
    sol = optimize_transport(boxes, shifts)
    by_box = {a.sealed_box_id: a.shift_id for a in sol.assignments}
    assert by_box == {"B1": "T2", "B2": "T1"}


def test_max_boxes_constraint():
    boxes = [bx(f"B{i}", "Z", 1, "ST1") for i in range(4)]
    shifts = [sh("T1", {"Z"}, 100, 2, 10, 0),
              sh("T2", {"Z"}, 100, 2, 10, 0)]
    sol = optimize_transport(boxes, shifts)
    assert sol.vehicle_count == 2


def test_infeasible_report_covers_every_box():
    boxes = [bx("B1", "FROZEN", 50, "ST1"),
             bx("B2", "CHILLED", 10, "ST2"),
             bx("B3", "DEEP", 5, "ST1")]
    shifts = [sh("T1", {"CHILLED"}, 20, 5, 10, 1)]
    rep = optimize_transport(boxes, shifts)
    assert isinstance(rep, InfeasibilityReport)
    assert {p.sealed_box_id for p in rep.per_box} == {"B1", "B2", "B3"}
    assert rep.unmet_constraints
    # B1 too heavy, B3 zone not accepted anywhere
    b1 = next(p for p in rep.per_box if p.sealed_box_id == "B1")
    b3 = next(p for p in rep.per_box if p.sealed_box_id == "B3")
    assert b1.compatible_shifts == () and b1.blocking
    assert b3.compatible_shifts == () and b3.blocking


def test_gross_weight_includes_empty_box_weight():
    # capacity fits net item weight (10) but not gross (10+6 empty)
    boxes = [bx("B1", "Z", 16, "ST1")]  # gross already includes empty=6
    shifts = [sh("T", {"Z"}, 15, 5, 10, 1)]
    rep = optimize_transport(boxes, shifts)
    assert isinstance(rep, InfeasibilityReport)
    assert rep.total_gross_weight == 16
    # and with adequate capacity it ships
    shifts2 = [sh("T", {"Z"}, 16, 5, 10, 1)]
    sol = optimize_transport(boxes, shifts2)
    assert sol.total_cost == 11


def test_cross_check_against_independent_enumeration(scoped_svc):
    plan = scoped_svc.plan_packing()
    sealed = scoped_svc.sealing.seal(plan)
    stores = {}
    for b in sealed:
        stores[b.sealed_box_id] = {"CHILLED": "ST-1", "FROZEN": "ST-2",
                                   "DEEP": "ST-3"}[b.zone]
    tp = scoped_svc.plan_transport(stores)
    assert not isinstance(tp, InfeasibilityReport)

    boxes = [bx(b.sealed_box_id, b.zone, b.gross_weight,
                stores[b.sealed_box_id]) for b in sealed]
    with scoped_svc.repo.read() as con:
        shifts = scoped_svc.repo.all_shifts(con)
    naive = naive_best_transport(naive_boxes(boxes), naive_shifts(shifts))
    assert naive is not None
    assert (tp.total_cost, tp.vehicle_count, tp.shift_vector()) == naive
    # every box exactly once
    assert sorted(a.sealed_box_id for a in tp.assignments) == sorted(stores)
