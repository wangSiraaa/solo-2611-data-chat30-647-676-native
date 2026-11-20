#!/usr/bin/env python3
"""End-to-end demo of the cold-chain packing & transport services.

Builds the scoped dataset (8 items, 3 box types -> 6 sealed boxes, 4 shifts)
and walks the full pipeline: pack -> seal -> plan -> confirm -> depart ->
trace, then demonstrates the required scenario cases.

Usage:  python3 scripts/demo.py [db_path]   (default: ./coldchain_demo.db)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coldchain import ColdChainService, Repository  # noqa: E402
from coldchain.models import InfeasibilityReport  # noqa: E402


def hr(title):
    print("\n" + "=" * 68 + f"\n{title}\n" + "=" * 68)


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "./coldchain_demo.db"
    if os.path.exists(db):
        os.remove(db)
    svc = ColdChainService(Repository(db))

    hr("1. Master data: 3 box types, 8 items, 4 shifts")
    svc.register_box_type("S", 4, 4, 2, 20, {"CHILLED"})
    svc.register_box_type("M", 6, 5, 4, 11, {"CHILLED"})
    svc.register_box_type("L", 8, 6, 6, 12, {"CHILLED", "FROZEN", "DEEP"})
    print("box types: S(4x4,e2,max20,CHILLED) M(6x5,e4,max11,CHILLED)"
          " L(8x6,e6,max12,ALL)")
    for uid, l, w, wt, z in [
            ("A1", 2, 2, 3, "CHILLED"), ("A2", 2, 2, 3, "CHILLED"),
            ("B1", 3, 2, 4, "CHILLED"), ("B2", 3, 2, 4, "CHILLED"),
            ("C1", 2, 3, 5, "FROZEN"), ("C2", 2, 3, 5, "FROZEN"),
            ("D1", 2, 2, 2, "DEEP"), ("D2", 3, 3, 6, "DEEP")]:
        svc.register_item(uid, l, w, wt, z)
    print("items: A1,A2(2x2,3,CHILLED) B1,B2(3x2,4,CHILLED)"
          " C1,C2(2x3,5,FROZEN) D1(2x2,2,DEEP) D2(3x3,6,DEEP)")
    svc.register_shift("T1", {"CHILLED"}, 30, 4, 50, 10)
    svc.register_shift("T2", {"CHILLED", "FROZEN", "DEEP"}, 60, 6, 80, 12)
    svc.register_shift("T3", {"FROZEN", "DEEP"}, 40, 4, 40, 8)
    svc.register_shift("T4", {"CHILLED", "FROZEN", "DEEP"}, 25, 3, 30, 6)
    print("shifts: T1(CHILLED,30,4bx,50+10/stop)"
          " T2(ALL,60,6bx,80+12/stop) T3(F+D,40,4bx,40+8/stop)"
          " T4(ALL,25,3bx,30+6/stop)")

    hr("2. Packing (independent optimizer, canonical normalization)")
    plan = svc.plan_packing()
    print(f"plan {plan.plan_id}  boxes={plan.box_count}"
          f"  total_area={plan.total_area}  norm={plan.normalization}")
    for b in plan.boxes:
        inside = ", ".join(f"{p.unit_id}@({p.x},{p.y})r{p.rotated}"
                           for p in b.placements)
        print(f"  {b.box_type_id} zone={b.zone} gross={b.gross_weight}"
              f" (empty {b.empty_weight})  {inside}")

    hr("3. Sealing (atomic, immutable, stable ids)")
    sealed = svc.sealing.seal(plan)
    for b in sealed:
        print(f"  {b.sealed_box_id}  {b.box_type_id} {b.zone}"
              f"  gross={b.gross_weight}  items="
              f"{[i.unit_id for i in b.items]}")
    again = svc.sealing.seal(plan)
    print("re-seal (复读) returns identical ids:",
          [b.sealed_box_id for b in again] == [b.sealed_box_id
                                               for b in sealed])

    hr("4. Transport planning (cost = fixed + per-distinct-store stops)")
    stores = {b.sealed_box_id: {"CHILLED": "ST-1", "FROZEN": "ST-2",
                                "DEEP": "ST-3"}[b.zone] for b in sealed}
    tp = svc.plan_transport(stores)
    assert not isinstance(tp, InfeasibilityReport)
    print(f"plan {tp.plan_id}  cost={tp.total_cost}"
          f"  vehicles={tp.vehicle_count}  status={tp.status.value}"
          f"  v{tp.version}")
    for a in tp.assignments:
        print(f"  {a.sealed_box_id} -> {a.shift_id} store={a.store_id}"
              f"  gross={a.gross_weight}")

    hr("5. Confirm (atomic reservation) -> depart")
    tp = svc.confirm(tp.plan_id, expected_version=1)
    print(f"confirmed: {tp.status.value} v{tp.version}")
    tp = svc.depart(tp.plan_id, expected_version=2)
    print(f"departed:  {tp.status.value} v{tp.version}")

    hr("6. Per-item traceability")
    for uid in ("A1", "C1", "D2"):
        for e in svc.trace_unit(uid):
            leg = e["transport"][0]
            print(f"  {uid}: box {e['sealed_box_id']} ({e['box_type_id']},"
                  f" {e['zone']}, gross {e['gross_weight']})"
                  f" @({e['placement']['x']},{e['placement']['y']})"
                  f"r{e['placement']['rotated']}"
                  f" -> shift {leg['shift_id']} store {leg['store_id']}"
                  f" [{leg['status']}]")

    hr("7. Scenario: infeasible transport reports every box")
    svc.register_shift("T9", {"CHILLED"}, 5, 1, 1, 1)  # tiny capacity
    rep = svc.plan_transport(stores, ["T9"])
    assert isinstance(rep, InfeasibilityReport)
    print(f"unmet constraints ({len(rep.unmet_constraints)}):")
    for u in rep.unmet_constraints:
        print(f"  - {u}")
    print(f"per-box entries: {len(rep.per_box)} (all {len(sealed)} covered,"
          f" none omitted)")

    hr("8. Scenario: replacement propagates box count & gross weight")
    db2 = db + ".replace"
    if os.path.exists(db2):
        os.remove(db2)
    svc2 = ColdChainService(Repository(db2))
    svc2.register_box_type("S", 4, 4, 2, 20, {"CHILLED"})
    svc2.register_box_type("L", 8, 6, 6, 12,
                           {"CHILLED", "FROZEN", "DEEP"})
    for uid, l, w, wt, z in [("A1", 2, 2, 3, "CHILLED"),
                             ("A2", 2, 2, 3, "CHILLED"),
                             ("B1", 3, 2, 4, "CHILLED"),
                             ("B2", 3, 2, 4, "CHILLED")]:
        svc2.register_item(uid, l, w, wt, z)
    svc2.register_shift("T1", {"CHILLED"}, 60, 6, 40, 5)
    p1 = svc2.plan_packing()
    s1 = svc2.sealing.seal(p1)
    t1 = svc2.plan_transport({b.sealed_box_id: "ST-1" for b in s1})
    svc2.confirm(t1.plan_id, 1)
    print(f"old plan: {len(s1)} boxes, cost {t1.total_cost},"
          f" grosses {[b.gross_weight for b in s1]}")
    for uid in ("A1", "A2", "B1", "B2"):
        svc2.update_item_dims(uid, length=2, width=1)
    p2 = svc2.plan_packing()
    s2 = svc2.sealing.seal(p2)
    t2 = svc2.replace(t1.plan_id, {b.sealed_box_id: "ST-1" for b in s2},
                      expected_version=2)
    print(f"new plan: {len(s2)} boxes, cost {t2.total_cost},"
          f" grosses {[b.gross_weight for b in s2]}"
          f"  (old plan -> REPLACED, old sealed boxes kept)")
    print(f"old box still readable: "
          f"{svc2.sealing.get(s1[0].sealed_box_id).gross_weight}"
          f" == {s1[0].gross_weight}")

    print("\nDemo complete. Database:", db)


if __name__ == "__main__":
    main()
