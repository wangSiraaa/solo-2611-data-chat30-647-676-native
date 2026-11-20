"""Independent brute-force oracle used by the tests.

No solver, no heuristics: enumerate every booking-to-room choice (or leave the
booking unsatisfied), reject illegal combinations, then pick the minimum of
the documented lexicographic key.  Only intended for tiny instances.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Sequence

from hotel.bookings import BookingRecord, BookingSlice
from hotel.state import StateProduct, intersects


@dataclass(frozen=True)
class BruteResult:
    assignments: tuple[tuple[str, str], ...]
    unsatisfied: tuple[str, ...]
    satisfied: int
    upgrade_sum: int
    key: tuple[Any, ...]


def _eligible(product: StateProduct, booking: BookingRecord, room_id: str) -> bool:
    room = product.rooms[room_id]
    if int(room["capacity"]) < booking.party_size:
        return False
    if not (booking.min_level <= int(room["level"]) <= booking.max_level):
        return False
    hstart, hend = product.horizon
    if booking.arrival < hstart or booking.departure > hend:
        return False
    blocked = [(int(s), int(e)) for s, e in room["blocked"]]
    return not intersects(blocked, booking.arrival, booking.departure)


def _pair_ok(
    product: StateProduct,
    a: BookingRecord,
    b: BookingRecord,
    room_id: str,
) -> bool:
    cleaning = int(product.rooms[room_id]["cleaning_days"])
    first, second = (a, b) if a.arrival <= b.arrival else (b, a)
    return first.departure + cleaning <= second.arrival


def brute_force(product: StateProduct, bookings: BookingSlice) -> BruteResult:
    records: Sequence[BookingRecord] = sorted(
        bookings.records, key=lambda b: b.booking_id
    )
    room_ids = sorted(product.rooms)
    rank = {rid: i for i, rid in enumerate(room_ids)}
    sentinel = len(room_ids)

    choices = [
        [None] + [rid for rid in room_ids if _eligible(product, record, rid)]
        for record in records
    ]

    best_key: tuple[Any, ...] | None = None
    best: BruteResult | None = None

    for combo in itertools.product(*choices):
        by_room: dict[str, list[int]] = {}
        legal = True
        for index, rid in enumerate(combo):
            if rid is None:
                continue
            by_room.setdefault(rid, []).append(index)
        for rid, indexes in by_room.items():
            for i in range(len(indexes)):
                for j in range(i + 1, len(indexes)):
                    if not _pair_ok(
                        product, records[indexes[i]], records[indexes[j]], rid
                    ):
                        legal = False
                        break
                if not legal:
                    break
            if not legal:
                break
        if not legal:
            continue

        satisfied = sum(1 for rid in combo if rid is not None)
        upgrade = sum(
            int(product.rooms[rid]["level"]) - records[index].min_level
            for index, rid in enumerate(combo)
            if rid is not None
        )
        vector = tuple(
            sentinel if rid is None else rank[rid] for rid in combo
        )
        key = (-satisfied, upgrade, vector)
        if best_key is None or key < best_key:
            best_key = key
            assigned = tuple(
                (records[index].booking_id, rid)
                for index, rid in enumerate(combo)
                if rid is not None
            )
            unsat = tuple(
                records[index].booking_id
                for index, rid in enumerate(combo)
                if rid is None
            )
            best = BruteResult(
                assignments=assigned,
                unsatisfied=unsat,
                satisfied=satisfied,
                upgrade_sum=upgrade,
                key=key,
            )
    assert best is not None, "there is always the all-unsatisfied solution"
    return best
