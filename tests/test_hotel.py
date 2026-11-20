import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hotel.bookings import BookingStore
from hotel.db import Database
from hotel.errors import (
    AlreadyExists,
    NotFound,
    RecordRevoked,
    RevisionConflict,
    StaleRevision,
    ValidationError,
)
from hotel.fulfilment import Fulfilment
from hotel.orchestration import Hotel
from hotel.state import RoomStateService, covers, merge_intervals, subtract_intervals

from hotel.oracle import brute_force


HORIZON = (0, 12)


def fresh() -> tuple[Database, RoomStateService, BookingStore, Fulfilment]:
    db = Database()
    state = RoomStateService(db, horizon=HORIZON)
    bookings = BookingStore(db)
    fulfilment = Fulfilment(db)
    return db, state, bookings, fulfilment


def assignment_pairs(plan):
    return sorted((a.booking_id, a.room_id) for a in plan.assignments)


# ---------------------------------------------------------------------------
# Interval primitives


def test_interval_union_and_subtraction():
    assert merge_intervals([(0, 2), (2, 4), (7, 8)]) == [(0, 4), (7, 8)]
    assert merge_intervals([(5, 7), (1, 3)]) == [(1, 3), (5, 7)]
    assert subtract_intervals((0, 10), [(2, 4), (7, 8)]) == [
        (0, 2), (4, 7), (8, 10)
    ]
    assert subtract_intervals((0, 5), [(0, 5)]) == []
    assert covers([(1, 4)], 2, 3)
    assert not covers([(1, 3)], 2, 4)


# ---------------------------------------------------------------------------
# Room-state service semantics


def test_room_registration_is_fixed_and_idempotent():
    db, state, _, _ = fresh()
    v1 = state.register_room("R1", 1, 2, 0)
    assert state.register_room("R1", 1, 2, 0) == v1
    with pytest.raises(AlreadyExists):
        state.register_room("R1", 2, 2, 0)
    # Registering a room is a state version, and the front desk needs no bookings.
    assert state.current_version() == 1
    assert state.rooms()[0]["room_id"] == "R1"


def test_maintenance_revision_semantics():
    db, state, _, _ = fresh()
    state.register_room("R1", 1, 2, 0)

    receipt = state.add_maintenance("M1", "R1", 2, 4)
    assert receipt["committed_version"] == 2
    # Same revision, same content -> no-op.
    replay = state.add_maintenance("M1", "R1", 2, 4)
    assert replay["replayed"] is True
    assert replay["committed_version"] == receipt["committed_version"]
    assert state.current_version() == 2

    # Same revision, different content -> rejected.
    with pytest.raises(RevisionConflict):
        state.set_maintenance("M1", 1, "add", "R1", 3, 5)

    # Older revision -> rejected.
    state.replace_maintenance("M1", 3, "R1", 5, 7)
    with pytest.raises(StaleRevision):
        state.replace_maintenance("M1", 2, "R1", 2, 4)

    # Revocation is terminal.
    state.revoke_maintenance("M1", 4)
    with pytest.raises(RecordRevoked):
        state.replace_maintenance("M1", 5, "R1", 0, 1)
    with pytest.raises(RecordRevoked):
        state.add_maintenance("M1", "R1", 0, 1, revision=5)

    history = state.maintenance_history("M1")
    assert [(h["revision"], h["op"]) for h in history] == [
        (1, "add"), (3, "replace"), (4, "revoke")
    ]
    assert state.product().available_intervals("R1") == [(0, 12)]


def test_maintenance_requires_existing_room_and_valid_intervals():
    db, state, _, _ = fresh()
    with pytest.raises(NotFound):
        state.add_maintenance("M1", "NOPE", 0, 1)
    state.register_room("R1", 1, 2, 0)
    state.register_room("R2", 1, 2, 0)
    with pytest.raises(ValidationError):
        state.add_maintenance("M2", "R1", 3, 3)
    with pytest.raises(NotFound):
        state.replace_maintenance("M9", 2, "R1", 0, 1)
    state.add_maintenance("M3", "R1", 0, 1)
    # A maintenance record stays on its room for its whole life.
    with pytest.raises(ValidationError):
        state.replace_maintenance("M3", 2, "R2", 0, 1)
    with pytest.raises(ValidationError):
        state.revoke_maintenance("M3", 2, room_id="R2")


def test_products_are_immutable_and_versioned():
    db, state, _, _ = fresh()
    state.register_room("R1", 1, 2, 0)
    state.add_maintenance("M1", "R1", 2, 4)
    v2 = state.current_version()
    old = state.product(v2)
    assert old.available_intervals("R1") == [(0, 2), (4, 12)]

    state.add_maintenance("M2", "R1", 6, 8)
    new = state.product()
    assert new.version > old.version
    assert new.available_intervals("R1") == [(0, 2), (4, 6), (8, 12)]

    # Old product did not change.
    again = state.product(v2)
    assert again.digest == old.digest
    assert again.available_intervals("R1") == [(0, 2), (4, 12)]

    # Arbitrary sub-window queries are clipped from the same blocked set.
    assert again.available_intervals("R1", 3, 7) == [(4, 7)]
    assert not again.is_available("R1", 1, 3)
    assert again.is_available("R1", 0, 2)

    # Products are persisted and self-describing.
    listed = state.products()
    assert any(p["version"] == v2 and p["digest"] == old.digest for p in listed)


def test_maintenance_replacement_not_revival():
    db, state, _, _ = fresh()
    state.register_room("R1", 1, 2, 0)
    state.add_maintenance("M1", "R1", 2, 4)
    state.revoke_maintenance("M1", 2)
    assert state.product().available_intervals("R1") == [(0, 12)]
    # Reusing an add at a later revision on the same id is still a revival.
    with pytest.raises(RecordRevoked):
        state.set_maintenance("M1", 3, "add", "R1", 2, 4)


# ---------------------------------------------------------------------------
# Booking record semantics


def test_booking_revision_semantics_and_history():
    db, _, bookings, _ = fresh()
    r1 = bookings.submit("B1", 1, 0, 2, 2, 1, 2)
    assert not r1["replayed"]
    assert bookings.submit("B1", 1, 0, 2, 2, 1, 2)["replayed"]
    with pytest.raises(RevisionConflict):
        bookings.submit("B1", 1, 1, 3, 2, 1, 2)
    # Replaying revision 1 with its original content stays a pure no-op.
    replay = bookings.submit("B1", 1, 0, 2, 2, 1, 2)
    assert replay["replayed"] is True
    # Revisions may skip numbers; a never-seen revision below the head is stale.
    bookings.correct("B1", 3, 1, 3, 2, 1, 2)
    with pytest.raises(StaleRevision):
        bookings.correct("B1", 2, 0, 2, 2, 1, 2)
    bookings.correct("B1", 4, 2, 4, 2, 1, 2)
    bookings.cancel("B1", 5)
    with pytest.raises(RecordRevoked):
        bookings.correct("B1", 6, 0, 2, 2, 1, 2)
    with pytest.raises(RecordRevoked):
        bookings.cancel("B1", 6)

    history = bookings.history("B1")
    assert [(h["revision"], h["action"]) for h in history] == [
        (1, "active"), (3, "active"), (4, "active"), (5, "cancelled")
    ]
    assert bookings.snapshot().records == ()


def test_booking_cancel_cannot_change_stay_and_needs_existence():
    db, _, bookings, _ = fresh()
    with pytest.raises(NotFound):
        bookings.cancel("B1", 1)
    bookings.submit("B1", 1, 0, 2, 2, 1, 2)
    with pytest.raises(ValidationError):
        bookings.commit("B1", 2, "cancelled", arrival=5)


# ---------------------------------------------------------------------------
# Core fulfilment cases


def test_daily_capacity_can_be_enough_without_one_continuous_room():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 2, 0)
    state.register_room("R2", 1, 2, 0)
    # One room is free each night, but no single room is free for all four.
    state.add_maintenance("M1", "R1", 0, 2)
    state.add_maintenance("M2", "R2", 2, 4)
    bookings.submit("B1", 1, 0, 4, 1, 1, 2)

    product = state.product()
    assert len(product.available_intervals("R1")) == 1
    plan = fulfil.publish(product, bookings.snapshot())
    assert plan.summary()["satisfied"] == 0
    unsat = plan.unsatisfied[0]
    assert unsat.reason == "no_legal_room"
    assert unsat.detail["rooms_rejected_by"]["maintenance"] == ["R1", "R2"]

def test_maintenance_end_boundary_is_half_open():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 4, 0)
    state.add_maintenance("M1", "R1", 2, 5)
    bookings.submit("B_BEFORE", 1, 0, 2, 1, 1, 1)
    bookings.submit("B_AT_START", 1, 2, 3, 1, 1, 1)
    bookings.submit("B_AT_END", 1, 5, 6, 1, 1, 1)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    assigned = plan.assignment_map()
    assert "B_BEFORE" in assigned and "B_AT_END" in assigned
    assert "B_AT_START" not in assigned
    assert plan.unsatisfied_map()["B_AT_START"].reason == "no_legal_room"


def test_cleaning_days_between_adjacent_bookings():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 4, 2)
    state.register_room("R2", 1, 1, 0)  # capacity 1, cannot host a party of 2
    bookings.submit("B1", 1, 0, 2, 2, 1, 1)
    bookings.submit("B2", 1, 4, 6, 2, 1, 1)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    # R1 free from day 2, but the 2 cleaning days push the earliest next
    # arrival to day 4; R2 has enough days but not enough capacity.
    assigned = plan.assignment_map()
    assert set(assigned) == {"B1", "B2"}
    assert {a.room_id for a in assigned.values()} == {"R1"}

    # One day earlier would break the cleaning rule.
    bookings.correct("B2", 2, 3, 5, 2, 1, 1)
    plan2 = fulfil.publish(state.product(), bookings.snapshot())
    assert plan2.summary()["satisfied"] == 1
    loser = plan2.unsatisfied[0]
    # R1 alone could host B2, so the loss is a competition for the only
    # suitable room, not an inherently illegal booking.
    assert loser.reason == "competition"
    assert loser.booking_id == "B2"


def test_cleaning_window_may_coincide_with_maintenance():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 4, 2)
    state.register_room("R2", 1, 1, 0)  # too small for a party of 2
    state.add_maintenance("M1", "R1", 2, 3)  # sits inside [2, 4)
    bookings.submit("B1", 1, 0, 2, 2, 1, 1)
    bookings.submit("B2", 1, 4, 6, 2, 1, 1)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    # Cleaning occupies [2, 4); maintenance [2, 3) simply overlaps that
    # already-unusable window, so B2 can arrive on day 4.  R1 serves both.
    assert plan.summary()["satisfied"] == 2
    product = state.product()
    assert fulfil.pair_compatible_in_room(
        product,
        bookings.snapshot().by_id()["B1"],
        bookings.snapshot().by_id()["B2"],
        "R1",
    )


def test_capacity_and_level_bounds():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 2, 0)
    state.register_room("R2", 3, 5, 0)
    bookings.submit("BIG", 1, 0, 2, 4, 1, 3)
    bookings.submit("TOO_HIGH", 1, 0, 2, 1, 2, 2)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    assigned = plan.assignment_map()
    assert assigned["BIG"].room_id == "R2"
    assert plan.unsatisfied_map()["TOO_HIGH"].reason == "no_legal_room"
    assert set(plan.unsatisfied_map()["TOO_HIGH"].detail["rooms_rejected_by"]["level"]) == {
        "R1", "R2"
    }


def test_upgrade_preemption_preserves_suite_booking():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 2, 0)
    state.register_room("R2", 2, 2, 0)
    # A flexible booking could take the suite, but the suite-only booking
    # must keep it; otherwise one more booking would be lost.
    bookings.submit("A", 1, 0, 2, 1, 1, 2)
    bookings.submit("B", 1, 0, 2, 1, 2, 2)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    assert assignment_pairs(plan) == [("A", "R1"), ("B", "R2")]
    assert plan.objective["upgrade_sum"] == 0

    # If every room is a suite, both bookings are satisfied and the upgrade
    # cost is acknowledged rather than hidden.
    db2, state2, bookings2, fulfil2 = fresh()
    state2.register_room("S1", 2, 2, 0)
    state2.register_room("S2", 2, 2, 0)
    bookings2.submit("A", 1, 0, 2, 1, 1, 2)
    bookings2.submit("B", 1, 0, 2, 1, 2, 2)
    plan2 = fulfil2.publish(state2.product(), bookings2.snapshot())
    assert plan2.objective["satisfied"] == 2
    assert plan2.objective["upgrade_sum"] == 1
    a = plan2.assignment_map()["A"]
    assert a.room_id == "S1" and a.upgrade_delta == 1


def test_equal_cost_ties_use_sorted_booking_ids_and_lex_rooms():
    db, state, bookings, fulfil = fresh()
    for rid in ("R2", "R1"):
        state.register_room(rid, 1, 2, 0)
    # Submission order is intentionally reversed; the optimal vector must
    # not depend on it.
    bookings.submit("B2", 1, 3, 5, 1, 1, 1)
    bookings.submit("B1", 1, 0, 2, 1, 1, 1)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    # Both bookings can share R1, so lexicographic minimisation puts both
    # there (vectors [R1, R1] beat [R1, R2] and [R2, ...]).
    assert assignment_pairs(plan) == [("B1", "R1"), ("B2", "R1")]

    # Same problem with a conflicting overlap: B1 and B2 cannot share, so
    # the lexicographic solution is B1->R1, B2->R2.
    bookings.correct("B2", 2, 0, 2, 1, 1, 1)
    plan2 = fulfil.publish(state.product(), bookings.snapshot())
    assert assignment_pairs(plan2) == [("B1", "R1"), ("B2", "R2")]
    assert plan2.objective["lexicographic_room_vector"] == [
        ["B1", "R1"], ["B2", "R2"]
    ]


def test_unsatisfied_sorts_after_all_room_ids_but_never_beats_extra_satisfaction():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 1, 0)
    # Two overlapping bookings, one room: one must be unsatisfied.
    # B1 sorts first and must take R1, leaving B2 unsatisfied.
    bookings.submit("B2", 1, 0, 2, 1, 1, 1)
    bookings.submit("B1", 1, 0, 2, 1, 1, 1)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    assert plan.summary()["satisfied"] == 1
    assert plan.assignment_map()["B1"].room_id == "R1"
    assert plan.unsatisfied_map()["B2"].reason == "competition"
    assert plan.unsatisfied_map()["B2"].detail["competing_bookings"] == ["B1"]
    assert plan.objective["unsatisfied_sorts_after_all_room_ids"] is True


def test_no_legal_room_detail_reports_every_rejection_reason():
    db, state, bookings, fulfil = fresh()
    state.register_room("SMALL", 1, 1, 0)
    state.register_room("LOW", 1, 3, 0)
    state.register_room("HIGH", 3, 3, 0)
    state.add_maintenance("M1", "HIGH", 0, 12)
    # Party of 4, minimum level 2: SMALL fails capacity+level, LOW fails
    # capacity+level, HIGH fails maintenance.
    bookings.submit("B1", 1, 0, 2, 4, 2, 3)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    detail = plan.unsatisfied_map()["B1"].detail
    assert "SMALL" in detail["rooms_rejected_by"]["capacity"]
    assert "SMALL" in detail["rooms_rejected_by"]["level"]
    assert detail["rooms_rejected_by"]["maintenance"] == ["HIGH"]


def test_competition_is_distinguished_from_no_legal_room():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 2, 0)
    state.register_room("R2", 1, 2, 0)
    state.add_maintenance("M1", "R2", 0, 12)  # R2 is legal but never available
    bookings.submit("B1", 1, 0, 3, 1, 1, 1)
    bookings.submit("B2", 1, 1, 4, 1, 1, 1)
    bookings.submit("B3", 1, 6, 8, 1, 1, 1)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    # B3 fits R1 (and R2 if it were not under repair); B1/B2 fight for R1.
    unsatisfied = plan.unsatisfied_map()
    assert "B1" in unsatisfied or "B2" in unsatisfied
    loser = "B2" if "B1" in plan.assignment_map() else "B1"
    assert unsatisfied[loser].reason == "competition"
    assert "R1" in unsatisfied[loser].detail["eligible_rooms"]


def test_no_splitting_across_rooms():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 2, 0)  # free [0,2) only
    state.register_room("R2", 1, 2, 0)  # free [2,4) only
    state.add_maintenance("M1", "R1", 2, 12)
    state.add_maintenance("M2", "R2", 0, 2)
    bookings.submit("B1", 1, 0, 4, 1, 1, 1)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    assert plan.summary()["satisfied"] == 0
    assert plan.unsatisfied[0].reason == "no_legal_room"


# ---------------------------------------------------------------------------
# Brute-force cross-checks (small instances)


def build_instance(
    rooms: list[tuple[str, int, int, int]],
    maintenance: list[tuple[str, str, int, int]],
    bookings: list[tuple[str, int, int, int, int, int]],
) -> tuple[RoomStateService, BookingStore, Fulfilment]:
    db = Database()
    state = RoomStateService(db, horizon=HORIZON)
    for rid, level, capacity, cleaning in rooms:
        state.register_room(rid, level, capacity, cleaning)
    for mid, rid, start, end in maintenance:
        state.add_maintenance(mid, rid, start, end)
    store = BookingStore(db)
    for bid, arrival, departure, party, min_level, max_level in bookings:
        store.submit(bid, 1, arrival, departure, party, min_level, max_level)
    return state, store, Fulfilment(db)


@pytest.mark.parametrize(
    "rooms,maintenance,bookings",
    [
        # Four rooms, six overlapping bookings, cleaning and maintenance.
        (
            [("R1", 1, 2, 0), ("R2", 1, 2, 1), ("R3", 2, 3, 0), ("R4", 2, 1, 2)],
            [("M1", "R1", 2, 4), ("M2", "R3", 6, 8)],
            [
                ("A", 0, 3, 2, 1, 2),
                ("B", 1, 4, 1, 1, 2),
                ("C", 2, 5, 3, 2, 2),
                ("D", 5, 8, 1, 1, 2),
                ("E", 6, 9, 2, 1, 2),
                ("F", 0, 2, 1, 2, 2),
            ],
        ),
        # Upgrade competition with equal costs.
        (
            [("R1", 1, 2, 0), ("R2", 2, 2, 0), ("R3", 2, 2, 0), ("R4", 1, 3, 1)],
            [],
            [
                ("A", 0, 2, 1, 1, 2),
                ("B", 0, 2, 1, 2, 2),
                ("C", 1, 3, 3, 1, 2),
                ("D", 4, 6, 2, 1, 2),
                ("E", 5, 7, 1, 2, 2),
                ("F", 0, 4, 1, 1, 1),
            ],
        ),
        # Maintenance boundaries plus cleaning gaps.
        (
            [("R1", 1, 2, 1), ("R2", 1, 2, 2), ("R3", 2, 2, 0)],
            [("M1", "R1", 4, 6), ("M2", "R2", 0, 2)],
            [
                ("A", 0, 2, 2, 1, 2),
                ("B", 2, 4, 1, 1, 2),
                ("C", 4, 6, 2, 1, 2),
                ("D", 6, 8, 2, 1, 2),
                ("E", 8, 10, 1, 1, 1),
                ("F", 1, 5, 3, 2, 2),
            ],
        ),
        # Infeasible-by-level and by-capacity bookings mixed in.
        (
            [("R1", 1, 1, 0), ("R2", 2, 2, 0), ("R3", 3, 4, 0), ("R4", 2, 1, 0)],
            [("M1", "R2", 3, 5)],
            [
                ("A", 0, 3, 1, 1, 3),
                ("B", 2, 4, 2, 2, 2),
                ("C", 3, 6, 4, 1, 3),
                ("D", 0, 2, 3, 2, 3),
                ("E", 5, 7, 1, 3, 3),
                ("F", 5, 8, 2, 1, 1),
            ],
        ),
    ],
)
def test_solver_matches_exhaustive_oracle(rooms, maintenance, bookings):
    state, store, fulfil = build_instance(rooms, maintenance, bookings)
    product = state.product()
    slice_ = store.snapshot()
    plan = fulfil.publish(product, slice_)
    oracle = brute_force(product, slice_)

    assert plan.objective["satisfied"] == oracle.satisfied
    assert plan.objective["upgrade_sum"] == oracle.upgrade_sum
    assert assignment_pairs(plan) == sorted(oracle.assignments)
    assert sorted(plan.unsatisfied_map()) == sorted(oracle.unsatisfied)
    vector = [
        (bid, rid)
        for bid, rid in plan.objective["lexicographic_room_vector"]
        if rid is not None
    ]
    assert vector == list(oracle.assignments)


def test_random_small_instances_match_oracle():
    import random

    rng = random.Random(20261118)
    for case in range(25):
        room_count = rng.randint(1, 4)
        rooms = []
        for index in range(room_count):
            rid = f"R{index + 1}"
            rooms.append(
                (
                    rid,
                    rng.randint(1, 3),
                    rng.randint(1, 4),
                    rng.randint(0, 2),
                )
            )
        maintenance = []
        for index in range(rng.randint(0, 3)):
            rid, _, _, _ = rng.choice(rooms)
            start = rng.randint(0, 8)
            end = min(12, start + rng.randint(1, 3))
            maintenance.append((f"M{index + 1}", rid, start, end))
        bookings = []
        for index in range(rng.randint(1, 6)):
            arrival = rng.randint(0, 8)
            departure = min(12, arrival + rng.randint(1, 4))
            bookings.append(
                (
                    f"B{index + 1}",
                    arrival,
                    departure,
                    rng.randint(1, 4),
                    rng.randint(1, 3),
                    rng.randint(1, 3),
                )
            )
        # min_level must not exceed max_level.
        bookings = [
            (bid, a, d, p, lo, max(lo, hi))
            for bid, a, d, p, lo, hi in bookings
        ]
        state, store, fulfil = build_instance(rooms, maintenance, bookings)
        product = state.product()
        slice_ = store.snapshot()
        plan = fulfil.publish(product, slice_)
        oracle = brute_force(product, slice_)
        assert plan.objective["satisfied"] == oracle.satisfied, (case, rooms, bookings)
        assert plan.objective["upgrade_sum"] == oracle.upgrade_sum, (case, rooms, bookings)
        assert assignment_pairs(plan) == sorted(oracle.assignments), (case, rooms, bookings)


def test_input_order_does_not_change_the_plan():
    rooms = [("R1", 1, 2, 0), ("R2", 2, 2, 0), ("R3", 1, 1, 0)]
    bookings = [
        ("B3", 4, 6, 1, 1, 2),
        ("B1", 0, 3, 2, 1, 2),
        ("B2", 2, 4, 1, 2, 2),
        ("B4", 4, 6, 1, 1, 1),
        ("B5", 6, 8, 1, 1, 2),
        ("B6", 0, 2, 1, 1, 2),
    ]
    state_a, store_a, fulfil_a = build_instance(rooms, [], bookings)
    reversed_rooms = list(reversed(rooms))
    state_b, store_b, fulfil_b = build_instance(reversed_rooms, [], list(reversed(bookings)))
    plan_a = fulfil_a.publish(state_a.product(), store_a.snapshot())
    plan_b = fulfil_b.publish(state_b.product(), store_b.snapshot())
    assert assignment_pairs(plan_a) == assignment_pairs(plan_b)
    assert plan_a.objective["upgrade_sum"] == plan_b.objective["upgrade_sum"]


# ---------------------------------------------------------------------------
# Plan immutability, reopening, and state-change driven re-planning


def test_plan_survives_database_reopen(tmp_path):
    path = tmp_path / "hotel.sqlite3"
    db = Database(path)
    state = RoomStateService(db, horizon=HORIZON)
    store = BookingStore(db)
    fulfil = Fulfilment(db)
    state.register_room("R1", 1, 2, 0)
    state.register_room("R2", 2, 2, 0)
    store.submit("B1", 1, 0, 3, 1, 1, 2)
    store.submit("B2", 1, 1, 4, 1, 2, 2)
    db.close()

    db = Database(path)
    state = RoomStateService(db, horizon=HORIZON)
    store = BookingStore(db)
    fulfil = Fulfilment(db)
    plan1 = fulfil.publish(state.product(), store.snapshot(), label="opening")
    snapshot = (
        plan1.plan_id,
        plan1.product_digest,
        plan1.slice_digest,
        assignment_pairs(plan1),
        plan1.objective,
        plan1.explain("B1"),
    )
    db.close()

    # Reopen and mutate state; the old plan must read back byte-for-byte.
    db = Database(path)
    state = RoomStateService(db, horizon=HORIZON)
    store = BookingStore(db)
    fulfil = Fulfilment(db)
    state.add_maintenance("M1", "R1", 0, 3)
    state.add_maintenance("M2", "R2", 1, 4)
    store.submit("B3", 1, 0, 2, 1, 1, 2)
    plan_again = fulfil.plan(plan1.plan_id)
    assert (
        plan_again.plan_id,
        plan_again.product_digest,
        plan_again.slice_digest,
        assignment_pairs(plan_again),
        plan_again.objective,
        plan_again.explain("B1"),
    ) == snapshot
    # And the old product still answers with the old availability.
    old_product = state.product_by_digest(plan1.product_digest)
    assert old_product.available_intervals("R1") == [(0, 12)]
    assert old_product.digest == plan1.product_digest
    db.close()


def test_maintenance_change_drives_new_plan_and_moves_an_untouched_booking():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 1, 0)
    state.register_room("R2", 1, 1, 0)
    state.register_room("R3", 2, 1, 0)
    bookings.submit("B1", 1, 0, 2, 1, 1, 1)
    bookings.submit("B2", 1, 0, 2, 1, 1, 2)

    product1 = state.product()
    plan1 = fulfil.publish(product1, bookings.snapshot(), label="before repair")
    assert assignment_pairs(plan1) == [("B1", "R1"), ("B2", "R2")]

    # Maintenance is on R1 and does not touch B2's stay at all.  Re-solving is
    # still mandatory: B1 now needs R2, and the cheapest legal answer pushes
    # B2 (untouched by the repair) into the level-2 room.
    state.add_maintenance("M1", "R1", 0, 2)
    product2 = state.product()
    plan2 = fulfil.publish(product2, bookings.snapshot(), label="after repair")
    assert assignment_pairs(plan1) == [("B1", "R1"), ("B2", "R2")]
    assert assignment_pairs(plan2) == [("B1", "R2"), ("B2", "R3")]
    assert plan2.objective["satisfied"] == 2
    assert plan2.objective["upgrade_sum"] == 1
    assert plan2.assignment_map()["B2"].upgrade_delta == 1
    assert plan2.explain("B2")["room_id"] == "R3"

    # Old plan is untouched and still explains itself against the old product.
    old = fulfil.plan(plan1.plan_id)
    assert assignment_pairs(old) == [("B1", "R1"), ("B2", "R2")]
    assert old.explain("B2")["room_id"] == "R2"
    assert old.state_version < plan2.state_version


def test_old_product_is_actually_consumed_not_latest_totals():
    db, state, bookings, fulfil = fresh()
    # Two level-1 rooms; level 2 rooms only appear later.  A plan made from
    # the old product must not "see" the later capacity.
    state.register_room("R1", 1, 1, 0)
    state.register_room("R2", 1, 1, 0)
    for index in range(1, 4):
        bookings.submit(f"B{index}", 1, 0, 2, 1, 1, 1)
    product_old = state.product()
    slice_old = bookings.snapshot()
    plan_old = fulfil.publish(product_old, slice_old, label="old capacity")
    assert plan_old.summary()["satisfied"] == 2
    assert len(plan_old.unsatisfied) == 1

    state.register_room("R3", 1, 1, 0)
    state.register_room("R4", 1, 1, 0)
    product_new = state.product()
    plan_new = fulfil.publish(product_new, bookings.snapshot(), label="new capacity")
    assert plan_new.summary()["satisfied"] == 3
    assert plan_old.summary()["satisfied"] == 2
    # The old plan records exactly the product digest it consumed.
    assert plan_old.product_digest == product_old.digest
    assert plan_old.product_digest != product_new.digest


def test_plan_rows_are_immutable_in_the_database():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 2, 0)
    bookings.submit("B1", 1, 0, 2, 1, 1, 1)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    import sqlite3

    with pytest.raises(sqlite3.DatabaseError):
        db.execute(
            "UPDATE plans SET label = 'tampered' WHERE plan_id = ?", (plan.plan_id,)
        )
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(
            "UPDATE plan_assignments SET room_id = 'R1' WHERE plan_id = ?",
            (plan.plan_id,),
        )


def test_room_calendar_reports_everything():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 2, 1)
    state.add_maintenance("M1", "R1", 4, 5)
    bookings.submit("B1", 1, 0, 2, 1, 1, 1)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    calendar = plan.room_calendar("R1")
    assert calendar["bookings"] == [
        {"booking_id": "B1", "arrival": 0, "departure": 2, "party_size": 1}
    ]
    assert calendar["cleaning_windows"] == [
        {"after_booking": "B1", "start": 2, "end": 3}
    ]
    assert calendar["maintenance"] == [
        {"maintenance_id": "M1", "revision": 1, "start": 4, "end": 5}
    ]
    # Cleaning days are a policy gap, not a physical block; the displayed
    # "free" intervals are physical availability minus stays and repairs.
    assert calendar["free"] == [[2, 4], [5, 12]]
    with pytest.raises(NotFound):
        plan.room_calendar("NOPE")


def test_plan_explanation_covers_all_bookings():
    db, state, bookings, fulfil = fresh()
    state.register_room("R1", 1, 1, 0)
    bookings.submit("B1", 1, 0, 2, 1, 1, 1)
    bookings.submit("B2", 1, 0, 2, 1, 1, 1)
    plan = fulfil.publish(state.product(), bookings.snapshot())
    explanations = {e["booking_id"]: e for e in plan.explanations()}
    assert explanations["B1"]["status"] == "satisfied"
    assert explanations["B2"]["status"] == "unsatisfied"
    assert explanations["B2"]["reason"] == "competition"
    assert plan.booking_ids() == ["B1", "B2"]


def test_facade_replans_only_when_consumed_inputs_change():
    path_db = Database()
    hotel = Hotel(path_db, horizon=HORIZON)
    hotel.register_room("R1", 1, 2, 0)
    hotel.register_room("R2", 1, 2, 0)
    hotel.submit("B1", 1, 0, 2, 1, 1, 1)
    first = hotel.publish(label="initial")
    assert first.plan_id == 1

    # No change -> no new plan, even after a pure replay.
    assert hotel.publish_if_changed() is None
    hotel.add_maintenance_and_replan("M1", "R1", 0, 2)
    assert len(hotel.plans()) == 2
    assert hotel.publish_if_changed() is None

    # A duplicate replay of the same maintenance revision is a no-op and
    # must not create a third plan.
    hotel.add_maintenance("M1", "R1", 0, 2)
    assert hotel.publish_if_changed() is None
    assert len(hotel.plans()) == 2

    # A revocation changes the product and therefore spawns a plan.
    hotel.revoke_maintenance_and_replan("M1", 2)
    assert len(hotel.plans()) == 3
    assert hotel.latest_plan().assignment_map()["B1"].room_id == "R1"


def test_empty_hotel_and_empty_booking_slice_are_valid():
    db, state, bookings, fulfil = fresh()
    # No rooms, no bookings: an empty plan is still a meaningful publication.
    plan = fulfil.publish(state.product(0), bookings.snapshot(), label="empty")
    assert plan.summary()["satisfied"] == 0
    assert plan.summary()["unsatisfied"] == 0
    assert plan.assignments == () and plan.unsatisfied == ()

    # Room but no bookings: calendars still work.
    state.register_room("R1", 1, 2, 0)
    plan2 = fulfil.publish(state.product(), bookings.snapshot())
    assert plan2.assignments == ()
    assert plan2.room_calendar("R1")["free"] == [[0, 12]]


def test_concurrent_like_separate_service_instances_share_file(tmp_path):
    path = tmp_path / "shared.sqlite3"
    state_db = Database(path)
    state = RoomStateService(state_db, horizon=HORIZON)
    state.register_room("R1", 1, 2, 0)
    state.add_maintenance("M1", "R1", 3, 4)
    state_db.close()

    booking_db = Database(path)
    store = BookingStore(booking_db)
    store.submit("B1", 1, 0, 3, 1, 1, 1)
    reward_db = Database(path)
    state2 = RoomStateService(reward_db, horizon=HORIZON)
    fulfil = Fulfilment(reward_db)
    plan = fulfil.publish(state2.product(), store.snapshot())
    assert plan.summary()["satisfied"] == 1
    booking_db.close()
    reward_db.close()
