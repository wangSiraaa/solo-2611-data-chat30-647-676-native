"""End-to-end demonstration.

Run with::

    .venv/bin/python demo.py

It builds a 12-room / 20-booking / 30-day instance in SQLite, publishes plans,
drives re-planning from a real maintenance change, verifies that old plans
survive a database reopen, and runs an independent exhaustive check on a
4-room / 6-booking sub-instance.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hotel.bookings import BookingStore
from hotel.db import Database
from hotel.errors import RecordRevoked, RevisionConflict, StaleRevision
from hotel.fulfilment import Fulfilment
from hotel.oracle import brute_force
from hotel.state import RoomStateService

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
DB_PATH = RESULTS / "hotel.sqlite3"
HORIZON = (0, 30)

LINE = "=" * 78


def heading(text: str) -> None:
    print(f"\n{LINE}\n{text}\n{LINE}")


def show_plan(plan, title: str) -> None:
    print(f"\n{title}: {plan.summary()}")
    for item in plan.assignments:
        print(
            f"  {item.booking_id:<5} -> {item.room_id:<4} "
            f"days [{item.arrival:>2},{item.departure:>2}) "
            f"level {item.room_level} (requested {item.min_level}-{item.max_level}, "
            f"upgrade {item.upgrade_delta})"
        )
    for item in plan.unsatisfied:
        print(f"  {item.booking_id:<5} -> UNSATISFIED ({item.reason})")


def build_main_instance(db: Database) -> tuple[RoomStateService, BookingStore, Fulfilment]:
    state = RoomStateService(db, horizon=HORIZON)
    rooms = [
        ("R01", 1, 2, 1),
        ("R02", 1, 2, 1),
        ("R03", 1, 2, 0),
        ("R04", 1, 2, 2),
        ("R05", 2, 3, 1),
        ("R06", 2, 3, 1),
        ("R07", 2, 2, 0),
        ("R08", 2, 3, 2),
        ("R09", 3, 4, 1),
        ("R10", 3, 4, 1),
        ("R11", 3, 2, 2),
        ("R12", 3, 4, 0),
    ]
    for room_id, level, capacity, cleaning in rooms:
        state.register_room(room_id, level, capacity, cleaning)

    # 20 bookings over 30 days, submitted deliberately out of order.
    bookings = [
        ("BK20", 4, 7, 2, 2, 2),
        ("BK01", 0, 3, 2, 1, 1),
        ("BK11", 13, 16, 2, 1, 2),
        ("BK02", 0, 2, 3, 1, 2),
        ("BK19", 22, 26, 4, 2, 3),
        ("BK03", 1, 4, 1, 1, 3),
        ("BK18", 20, 24, 1, 3, 3),
        ("BK04", 2, 5, 2, 2, 2),
        ("BK12", 14, 17, 1, 1, 1),
        ("BK05", 3, 6, 4, 3, 3),
        ("BK16", 18, 21, 2, 1, 1),
        ("BK06", 5, 8, 2, 1, 2),
        ("BK15", 17, 20, 3, 2, 3),
        ("BK07", 6, 9, 3, 2, 2),
        ("BK14", 16, 19, 1, 1, 2),
        ("BK08", 8, 11, 2, 1, 1),
        ("BK13", 15, 18, 4, 3, 3),
        ("BK09", 9, 12, 1, 2, 2),
        ("BK17", 19, 23, 2, 2, 2),
        ("BK10", 11, 14, 2, 1, 3),
    ]
    store = BookingStore(db)
    for booking_id, arrival, departure, party, low, high in bookings:
        store.submit(booking_id, 1, arrival, departure, party, low, high)

    # Planned repairs, committed before the first product is published.
    maintenance = [
        ("MX01", "R04", 2, 4),
        ("MX02", "R08", 10, 12),
        ("MX03", "R11", 20, 22),
    ]
    for mid, room_id, start, end in maintenance:
        state.add_maintenance(mid, room_id, start, end)

    return state, store, Fulfilment(db)


def scenario_gallery() -> dict[str, object]:
    """Focused cases, each on its own throwaway in-memory database."""

    results: dict[str, object] = {}

    # 1. Every night has enough capacity, yet the whole stay has no room.
    db = Database()
    state = RoomStateService(db, horizon=(0, 8))
    state.register_room("G1", 1, 2, 0)
    state.register_room("G2", 1, 2, 0)
    state.add_maintenance("GA", "G1", 0, 2)
    state.add_maintenance("GB", "G2", 2, 4)
    store = BookingStore(db)
    store.submit("GAP-1", 1, 0, 4, 1, 1, 1)
    plan = Fulfilment(db).publish(state.product(), store.snapshot())
    results["capacity_without_continuity"] = {
        "nightly_availability": {
            "G1": state.product().available_intervals("G1"),
            "G2": state.product().available_intervals("G2"),
        },
        "satisfied": plan.objective["satisfied"],
        "reason": plan.unsatisfied[0].reason,
        "detail": plan.unsatisfied[0].detail,
    }
    db.close()

    # 2. Maintenance is [2, 5): arrival on day 2 is blocked, day 5 is fine.
    db = Database()
    state = RoomStateService(db, horizon=(0, 6))
    state.register_room("B1R", 1, 2, 0)
    state.add_maintenance("BND", "B1R", 2, 5)
    store = BookingStore(db)
    store.submit("BOUND-BEFORE", 1, 0, 2, 1, 1, 1)
    store.submit("BOUND-START", 1, 2, 3, 1, 1, 1)
    store.submit("BOUND-END", 1, 5, 6, 1, 1, 1)
    plan = Fulfilment(db).publish(state.product(), store.snapshot())
    results["half_open_boundary"] = {
        "maintenance": [2, 5],
        "satisfied": sorted(a.booking_id for a in plan.assignments),
        "unsatisfied": sorted(plan.unsatisfied_map()),
    }
    db.close()

    # 3. Cleaning days: R has cleaning 2, so departure 2 releases day 4.
    db = Database()
    state = RoomStateService(db, horizon=(0, 8))
    state.register_room("CLR", 1, 4, 2)
    store = BookingStore(db)
    store.submit("CLEAN-A", 1, 0, 2, 2, 1, 1)
    store.submit("CLEAN-B", 1, 3, 5, 2, 1, 1)  # too early: 2+2 > 3
    store.submit("CLEAN-C", 1, 4, 6, 2, 1, 1)  # fine
    plan = Fulfilment(db).publish(state.product(), store.snapshot())
    results["cleaning_interval"] = {
        "cleaning_days": 2,
        "satisfied": sorted(a.booking_id for a in plan.assignments),
        "unsatisfied": {k: v.reason for k, v in plan.unsatisfied_map().items()},
        "note": "one room, so 0-2 and 4-6 fit and 3-5 must be dropped",
    }
    db.close()

    # 4. Upgrade preemption: the flexible booking must not steal the suite.
    db = Database()
    state = RoomStateService(db, horizon=(0, 4))
    state.register_room("UP1", 1, 2, 0)
    state.register_room("UP2", 2, 2, 0)
    store = BookingStore(db)
    store.submit("UP-FLEX", 1, 0, 2, 1, 1, 2)
    store.submit("UP-SUITE", 1, 0, 2, 1, 2, 2)
    plan = Fulfilment(db).publish(state.product(), store.snapshot())
    results["upgrade_preemption"] = {
        "assignments": {a.booking_id: a.room_id for a in plan.assignments},
        "upgrade_sum": plan.objective["upgrade_sum"],
    }
    db.close()

    # 5. Equal-cost ties: two identical level-1 rooms, two overlapping
    #    bookings, submission order reversed.
    db = Database()
    state = RoomStateService(db, horizon=(0, 4))
    state.register_room("TIE-B", 1, 2, 0)
    state.register_room("TIE-A", 1, 2, 0)
    store = BookingStore(db)
    store.submit("TIE-2", 1, 0, 2, 1, 1, 1)
    store.submit("TIE-1", 1, 0, 2, 1, 1, 1)
    plan = Fulfilment(db).publish(state.product(), store.snapshot())
    results["equal_cost_tie"] = {
        "assignments": {a.booking_id: a.room_id for a in plan.assignments},
        "lexicographic_vector": plan.objective["lexicographic_room_vector"],
        "note": "sorted booking ids decide, not submission order",
    }
    db.close()

    # 6. Maintenance revocation restores capacity and triggers a new plan.
    db = Database()
    state = RoomStateService(db, horizon=(0, 6))
    state.register_room("REV-R", 1, 2, 0)
    store = BookingStore(db)
    store.submit("REV-1", 1, 0, 3, 1, 1, 1)
    state.add_maintenance("REV-M", "REV-R", 0, 3)
    plan_blocked = Fulfilment(db).publish(state.product(), store.snapshot())
    state.revoke_maintenance("REV-M", 2)
    plan_restored = Fulfilment(db).publish(state.product(), store.snapshot())
    results["maintenance_revocation"] = {
        "blocked_satisfied": plan_blocked.objective["satisfied"],
        "after_revoke_satisfied": plan_restored.objective["satisfied"],
        "blocked_reason": plan_blocked.unsatisfied[0].reason,
        "old_plan_unchanged": plan_blocked.objective["satisfied"] == 0,
    }
    db.close()

    # 7. A plan consumes its explicit product, not today's room totals.
    db = Database()
    state = RoomStateService(db, horizon=(0, 4))
    state.register_room("OLD-1", 1, 2, 0)
    store = BookingStore(db)
    for index in range(1, 3):
        store.submit(f"OLD-B{index}", 1, 0, 2, 1, 1, 1)
    old_product = state.product()
    old_slice = store.snapshot()
    old_plan = Fulfilment(db).publish(old_product, old_slice, label="old product")
    # New rooms are registered afterwards; they must not leak into old_plan.
    state.register_room("NEW-1", 1, 2, 0)
    state.register_room("NEW-2", 1, 2, 0)
    new_plan = Fulfilment(db).publish(
        state.product(), store.snapshot(), label="new product"
    )
    results["explicit_product"] = {
        "old_satisfied": old_plan.objective["satisfied"],
        "new_satisfied": new_plan.objective["satisfied"],
        "old_rooms": sorted(
            {a.room_id for a in old_plan.assignments}
        ),
        "old_product_digest": old_plan.product_digest,
        "new_product_digest": new_plan.product_digest,
        "old_plan_reloaded_satisfied": None,
    }
    # Re-read the persisted old plan after the state grew.
    results["explicit_product"]["old_plan_reloaded_satisfied"] = (
        Fulfilment(db).plan(old_plan.plan_id).objective["satisfied"]
    )
    db.close()
    return results


def small_oracle_check() -> dict[str, object]:
    """4 rooms / 6 bookings, solver vs. exhaustive enumeration.

    Uses its own database so the exhaustive search only sees these four rooms.
    """

    db = Database()
    state = RoomStateService(db, horizon=(0, 12))
    rooms = [
        ("O1", 1, 2, 0),
        ("O2", 1, 2, 1),
        ("O3", 2, 3, 0),
        ("O4", 2, 1, 2),
    ]
    for args in rooms:
        state.register_room(*args)
    state.add_maintenance("O-M1", "O1", 2, 4)
    state.add_maintenance("O-M2", "O3", 6, 8)
    store = BookingStore(db)
    entries = [
        ("O-A", 0, 3, 2, 1, 2),
        ("O-B", 1, 4, 1, 1, 2),
        ("O-C", 2, 5, 3, 2, 2),
        ("O-D", 5, 8, 1, 1, 2),
        ("O-E", 6, 9, 2, 1, 2),
        ("O-F", 0, 2, 1, 2, 2),
    ]
    for entry in entries:
        store.submit(entry[0], 1, *entry[1:])
    product = state.product()
    slice_ = store.snapshot()
    plan = Fulfilment(db).publish(product, slice_, label="oracle check")
    oracle = brute_force(product, slice_)
    result = {
        "rooms": len(rooms),
        "bookings": len(entries),
        "solver_satisfied": plan.objective["satisfied"],
        "solver_upgrade_sum": plan.objective["upgrade_sum"],
        "solver_assignments": {
            a.booking_id: a.room_id for a in plan.assignments
        },
        "oracle_satisfied": oracle.satisfied,
        "oracle_upgrade_sum": oracle.upgrade_sum,
        "oracle_assignments": dict(oracle.assignments),
        "match": (
            plan.objective["satisfied"] == oracle.satisfied
            and plan.objective["upgrade_sum"] == oracle.upgrade_sum
            and sorted(
                (a.booking_id, a.room_id) for a in plan.assignments
            )
            == list(oracle.assignments)
        ),
    }
    db.close()
    return result


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(DB_PATH) + suffix)
        if extra.exists():
            extra.unlink()

    heading("0. Fresh SQLite database")
    db = Database(DB_PATH)
    print(f"database: {DB_PATH}")
    print("The room-state service works without any booking; the fulfilment")
    print("vault only sees products and booking slices it is handed.")

    heading("1. Focused scenario gallery")
    gallery = scenario_gallery()
    print(json.dumps(gallery, indent=2, ensure_ascii=False))

    heading("2. 12 rooms / 20 bookings / 30 days")
    state = RoomStateService(db, horizon=HORIZON)
    store = BookingStore(db)
    fulfil = Fulfilment(db)
    state, store, fulfil = build_main_instance(db)
    product_1 = state.product()
    slice_1 = store.snapshot()
    plan_1 = fulfil.publish(product_1, slice_1, label="initial")
    show_plan(plan_1, "Initial plan")

    # A real maintenance change: R01 goes out of service for [5, 8).  Only
    # BK06's stay touches that window, yet the new plan may legitimately move
    # bookings whose stays are nowhere near it, because the whole plan is
    # re-optimised against a new product.
    repair = state.add_maintenance("MX04", "R01", 5, 8)
    product_2 = state.product()
    plan_2 = fulfil.publish(product_2, store.snapshot(), label="after repair")
    show_plan(plan_2, "After repair on R01 [5,8)")

    moves = []
    old_map = {a.booking_id: a.room_id for a in plan_1.assignments}
    new_map = {a.booking_id: a.room_id for a in plan_2.assignments}
    for booking_id in sorted(set(old_map) | set(new_map)):
        before = old_map.get(booking_id)
        after = new_map.get(booking_id)
        if before != after:
            moves.append(
                {
                    "booking_id": booking_id,
                    "before": before,
                    "after": after,
                }
            )
    print("\nMoves caused by the repair:")
    for move in moves:
        print(
            f"  {move['booking_id']}: {move['before']} -> {move['after']}"
        )
    assert plan_2.objective["satisfied"] == plan_1.objective["satisfied"] == 20

    # Prove that at least one moved booking does not overlap the repair at
    # all -- it moved purely because plans are global, not per-night.
    slice_by_id = store.snapshot().by_id()
    untouched_moved = [
        move
        for move in moves
        if move["before"] is not None
        and move["after"] is not None
        and not (
            slice_by_id[move["booking_id"]].arrival < repair["end"]
            and slice_by_id[move["booking_id"]].departure > repair["start"]
        )
    ]
    print(
        "\nMoved bookings whose own stay does not intersect the repair "
        "(global re-optimisation, not first-night checks):"
    )
    for move in untouched_moved:
        stay = slice_by_id[move["booking_id"]]
        print(
            f"  {move['booking_id']} stay [{stay.arrival},{stay.departure}) "
            f"did not touch [{repair['start']},{repair['end']}) "
            f"but moved {move['before']} -> {move['after']}"
        )
    assert untouched_moved, "demo expects at least one indirectly moved booking"

    heading("3. Explanations, calendars, unsatisfied reasons")
    sample_booking = plan_2.booking_ids()[0]
    print(json.dumps(plan_2.explain(sample_booking), indent=2, ensure_ascii=False))
    calendar = plan_2.room_calendar("R01")
    print("\nCalendar for R01:")
    print(json.dumps(calendar, indent=2, ensure_ascii=False))
    if plan_2.unsatisfied:
        print("\nUnsatisfied reasons:")
        print(json.dumps(plan_2.unsatisfied_reasons(), indent=2, ensure_ascii=False))

    heading("4. Reopen the database; old plan must be byte-for-byte stable")
    before = {
        "plan_id": plan_1.plan_id,
        "product_digest": plan_1.product_digest,
        "slice_digest": plan_1.slice_digest,
        "summary": plan_1.summary(),
        "assignments": [
            (a.booking_id, a.room_id, a.arrival, a.departure)
            for a in plan_1.assignments
        ],
        "explanation": plan_1.explain(plan_1.booking_ids()[0]),
    }
    db.close()

    db = Database(DB_PATH)
    state = RoomStateService(db, horizon=HORIZON)
    store = BookingStore(db)
    fulfil = Fulfilment(db)
    plan_1_again = fulfil.plan(plan_1.plan_id)
    after = {
        "plan_id": plan_1_again.plan_id,
        "product_digest": plan_1_again.product_digest,
        "slice_digest": plan_1_again.slice_digest,
        "summary": plan_1_again.summary(),
        "assignments": [
            (a.booking_id, a.room_id, a.arrival, a.departure)
            for a in plan_1_again.assignments
        ],
        "explanation": plan_1_again.explain(plan_1_again.booking_ids()[0]),
    }
    stable = before == after
    print(f"old plan stable across reopen: {stable}")
    assert stable
    print(
        "old product still answers with its own availability: "
        f"{state.product_by_digest(plan_1.product_digest).available_intervals('R01')}"
    )
    print(f"plan count after reopen: {len(fulfil.plans())} (old plans are kept)")

    heading("5. Independent exhaustive check: 4 rooms / 6 bookings")
    oracle_result = small_oracle_check()
    print(json.dumps(oracle_result, indent=2, ensure_ascii=False))
    assert oracle_result["match"]

    heading("6. Revision rules on the live database")
    rules = {}
    try:
        state.add_maintenance("MX04", "R01", repair["start"], repair["end"])
        rules["replay_same_revision"] = "no-op"
    except Exception as exc:  # pragma: no cover - demo visibility
        rules["replay_same_revision"] = f"unexpected {exc!r}"
    try:
        state.set_maintenance(
            "MX04", repair["revision"], "replace", "R01", 9, 10
        )
        rules["same_revision_different_content"] = "unexpectedly accepted"
    except RevisionConflict as exc:
        rules["same_revision_different_content"] = f"rejected: {exc}"
    state.replace_maintenance("MX04", 3, "R01", 9, 10)
    try:
        state.replace_maintenance("MX04", 2, "R01", 10, 11)
        rules["older_revision"] = "unexpectedly accepted"
    except StaleRevision as exc:
        rules["older_revision"] = f"rejected: {exc}"
    try:
        state.revoke_maintenance("MX04", 4)
        state.set_maintenance("MX04", 5, "add", "R01", 10, 11)
        rules["revived_after_revoke"] = "unexpectedly accepted"
    except RecordRevoked as exc:
        rules["revived_after_revoke"] = f"rejected: {exc}"
    print(json.dumps(rules, indent=2, ensure_ascii=False))

    with sqlite3.connect(DB_PATH) as conn:
        plan_count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        assignment_count = conn.execute(
            "SELECT COUNT(*) FROM plan_assignments"
        ).fetchone()[0]
        product_count = conn.execute(
            "SELECT COUNT(*) FROM state_products"
        ).fetchone()[0]
    print(
        f"\npersisted: {plan_count} plans, {assignment_count} assignments, "
        f"{product_count} immutable state products"
    )

    report = {
        "scope": {
            "rooms": len(state.rooms()),
            "bookings": len(store.snapshot().records),
            "days": HORIZON[1] - HORIZON[0],
        },
        "gallery": gallery,
        "initial_plan": plan_1.summary(),
        "replanned_plan": plan_2.summary(),
        "moves": moves,
        "untouched_bookings_that_moved": untouched_moved,
        "old_plan_stable": stable,
        "oracle": oracle_result,
        "revision_rules": rules,
        "persisted": {
            "plans": plan_count,
            "assignments": assignment_count,
            "state_products": product_count,
        },
    }
    (RESULTS / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    db.close()
    print(f"\nwrote {RESULTS / 'report.json'}")


if __name__ == "__main__":
    main()
