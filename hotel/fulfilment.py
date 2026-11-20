"""Booking fulfilment vault.

The vault consumes *one explicit* :class:`~hotel.state.StateProduct` and one
explicit :class:`~hotel.bookings.BookingSlice`.  It never reads the latest
room-state tables, so an old product keeps producing exactly the old answer.

Optimisation is lexicographic:

1. maximise the number of satisfied bookings;
2. minimise the total upgrade (room level - requested minimum level);
3. among the remaining ties, lexicographically minimise the room-id vector
   ordered by booking id, with "unsatisfied" sorting after every room id.

The third rule is evaluated per booking in sorted order, so equal-cost
solutions cannot depend on submission order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ortools.sat.python import cp_model

from .bookings import BookingRecord, BookingSlice
from .db import Database, utcnow
from .errors import NotFound, ProductMismatch, ValidationError
from .identity import canonical_json, digest
from .state import StateProduct, intersects, subtract_intervals


# ---------------------------------------------------------------------------
# Value objects


@dataclass(frozen=True)
class Assignment:
    booking_id: str
    room_id: str
    arrival: int
    departure: int
    party_size: int
    min_level: int
    max_level: int
    room_level: int
    upgrade_delta: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "booking_id": self.booking_id,
            "room_id": self.room_id,
            "arrival": self.arrival,
            "departure": self.departure,
            "party_size": self.party_size,
            "min_level": self.min_level,
            "max_level": self.max_level,
            "room_level": self.room_level,
            "upgrade_delta": self.upgrade_delta,
        }


@dataclass(frozen=True)
class Unsatisfied:
    booking_id: str
    reason: str               # "no_legal_room" | "competition"
    detail: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "booking_id": self.booking_id,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class Plan:
    """A published, immutable fulfilment plan."""

    plan_id: int
    label: str | None
    state_version: int
    product_digest: str
    slice_digest: str
    slice_watermark: int | None
    objective: Mapping[str, Any]
    assignments: tuple[Assignment, ...]
    unsatisfied: tuple[Unsatisfied, ...]
    product_json: str
    slice_json: str
    created_at: str

    # -- convenience ------------------------------------------------------

    def assignment_map(self) -> dict[str, Assignment]:
        return {item.booking_id: item for item in self.assignments}

    def unsatisfied_map(self) -> dict[str, Unsatisfied]:
        return {item.booking_id: item for item in self.unsatisfied}

    def assigned_booking_ids(self) -> list[str]:
        return [item.booking_id for item in self.assignments]

    def summary(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "label": self.label,
            "state_version": self.state_version,
            "product_digest": self.product_digest,
            "slice_digest": self.slice_digest,
            "slice_watermark": self.slice_watermark,
            "objective": dict(self.objective),
            "satisfied": len(self.assignments),
            "unsatisfied": len(self.unsatisfied),
            "created_at": self.created_at,
        }

    # -- explanation ------------------------------------------------------

    def explain(self, booking_id: str) -> dict[str, Any]:
        product = _product_view(self.product_json)
        assignments = self.assignment_map()
        if booking_id in assignments:
            item = assignments[booking_id]
            room = product["rooms"][item.room_id]
            return {
                "plan_id": self.plan_id,
                "booking_id": booking_id,
                "status": "satisfied",
                "room_id": item.room_id,
                "room_level": item.room_level,
                "upgrade_delta": item.upgrade_delta,
                "arrival": item.arrival,
                "departure": item.departure,
                "nights": item.departure - item.arrival,
                "party_size": item.party_size,
                "capacity": room["capacity"],
                "cleaning_days_after_stay": room["cleaning_days"],
                "maintenance_in_room": list(room["maintenance"]),
                "why": (
                    f"one room for the whole stay; level {item.room_level} "
                    f"is within [{item.min_level}, {item.max_level}], "
                    f"capacity {room['capacity']} >= {item.party_size}, and "
                    "no maintenance overlaps the stay"
                ),
            }
        unsatisfied = self.unsatisfied_map()
        if booking_id in unsatisfied:
            item = unsatisfied[booking_id]
            detail = dict(item.detail)
            if item.reason == "no_legal_room":
                why = "no room in the consumed state product satisfies the stay"
            else:
                why = (
                    "rooms that could host this booking alone are taken by "
                    "other bookings in this maximum-satisfaction plan"
                )
            return {
                "plan_id": self.plan_id,
                "booking_id": booking_id,
                "status": "unsatisfied",
                "reason": item.reason,
                "why": why,
                "detail": detail,
            }
        raise NotFound(
            f"booking {booking_id!r} is not part of plan {self.plan_id}"
        )

    def explanations(self) -> list[dict[str, Any]]:
        return [self.explain(booking_id) for booking_id in self.booking_ids()]

    def booking_ids(self) -> list[str]:
        return sorted(
            [item.booking_id for item in self.assignments]
            + [item.booking_id for item in self.unsatisfied]
        )

    def room_calendar(self, room_id: str) -> dict[str, Any]:
        product = _product_view(self.product_json)
        if room_id not in product["rooms"]:
            raise NotFound(f"room {room_id!r} is not in the consumed product")
        room = product["rooms"][room_id]
        bookings = sorted(
            (item for item in self.assignments if item.room_id == room_id),
            key=lambda item: (item.arrival, item.booking_id),
        )
        occupied = [(item.arrival, item.departure) for item in bookings]
        blocked = [(int(s), int(e)) for s, e in room["blocked"]]
        cleaning = int(room["cleaning_days"])
        horizon = (int(product["horizon"][0]), int(product["horizon"][1]))
        return {
            "plan_id": self.plan_id,
            "room_id": room_id,
            "level": room["level"],
            "capacity": room["capacity"],
            "cleaning_days": room["cleaning_days"],
            "horizon": list(horizon),
            "maintenance": [
                {
                    "maintenance_id": m["maintenance_id"],
                    "revision": m["revision"],
                    "start": m["start"],
                    "end": m["end"],
                }
                for m in room["maintenance"]
            ],
            "bookings": [
                {
                    "booking_id": item.booking_id,
                    "arrival": item.arrival,
                    "departure": item.departure,
                    "party_size": item.party_size,
                }
                for item in bookings
            ],
            "cleaning_windows": [
                {
                    "after_booking": item.booking_id,
                    "start": item.departure,
                    "end": item.departure + cleaning,
                }
                for item in bookings
                if cleaning > 0
            ],
            "free": [
                [s, e]
                for s, e in subtract_intervals(horizon, blocked + occupied)
            ],
        }

    def room_calendars(self) -> list[dict[str, Any]]:
        product = _product_view(self.product_json)
        return [self.room_calendar(rid) for rid in sorted(product["rooms"])]

    def unsatisfied_reasons(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.unsatisfied]


# ---------------------------------------------------------------------------
# Fulfilment


class Fulfilment:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- feasibility -------------------------------------------------------

    @staticmethod
    def room_eligible(
        product: StateProduct, booking: BookingRecord, room_id: str
    ) -> bool:
        room = product.rooms.get(room_id)
        if room is None:
            return False
        if int(room["capacity"]) < booking.party_size:
            return False
        level = int(room["level"])
        if level < booking.min_level or level > booking.max_level:
            return False
        hstart, hend = product.horizon
        if booking.arrival < hstart or booking.departure > hend:
            return False
        return not intersects(
            [(int(s), int(e)) for s, e in room["blocked"]],
            booking.arrival,
            booking.departure,
        )

    @staticmethod
    def pair_compatible(
        a: BookingRecord, b: BookingRecord, cleaning_days: int
    ) -> bool:
        return (
            a.departure + cleaning_days <= b.arrival
            or b.departure + cleaning_days <= a.arrival
        )

    @classmethod
    def pair_compatible_in_room(
        cls,
        product: StateProduct,
        a: BookingRecord,
        b: BookingRecord,
        room_id: str,
    ) -> bool:
        """Can both bookings share *room_id*?

        Two stays may share a room only when the earlier departure plus the
        room's cleaning days fits before the later arrival.  Maintenance is
        checked separately against each stay; the gap itself may coincide with
        maintenance because the room is out of service either way.
        """

        cleaning = product.cleaning_days(room_id)
        first, second = (a, b) if a.arrival <= b.arrival else (b, a)
        return first.departure + cleaning <= second.arrival

    def eligible_rooms(
        self, product: StateProduct, booking: BookingRecord
    ) -> list[str]:
        return [
            rid
            for rid in product.room_ids()
            if self.room_eligible(product, booking, rid)
        ]

    # -- optimisation ------------------------------------------------------

    def solve(self, product: StateProduct, bookings: BookingSlice) -> dict[str, Any]:
        """Compute the lexicographically optimal assignment as a dict."""

        records = list(bookings.records)  # already sorted by booking_id
        room_ids = product.room_ids()
        rank = {rid: index for index, rid in enumerate(room_ids)}
        sentinel = len(room_ids)

        eligible: dict[str, list[str]] = {
            b.booking_id: self.eligible_rooms(product, b) for b in records
        }

        def build(
            fixed_satisfied: Mapping[str, str] | None = None,
            fixed_unsatisfied: Iterable[str] = (),
            total_satisfied: int | None = None,
            total_upgrade: int | None = None,
        ) -> tuple[cp_model.CpModel, dict[tuple[str, str], Any], dict[str, Any]]:
            model = cp_model.CpModel()
            x: dict[tuple[str, str], Any] = {}
            for booking in records:
                for rid in eligible[booking.booking_id]:
                    x[(booking.booking_id, rid)] = model.NewBoolVar(
                        f"x_{booking.booking_id}_{rid}"
                    )
            # At most one room for every booking; one booking per stay.
            for booking in records:
                vars_here = [x[(booking.booking_id, rid)] for rid in eligible[booking.booking_id]]
                if vars_here:
                    model.Add(sum(vars_here) <= 1)
            # Pairwise room conflicts (capacity/level/maintenance already
            # filtered per candidate).  Cleaning days and the cleaning
            # window's own maintenance boundary are part of this test.
            for rid in room_ids:
                hosts = [b for b in records if rid in eligible[b.booking_id]]
                for i in range(len(hosts)):
                    for j in range(i + 1, len(hosts)):
                        a, b = hosts[i], hosts[j]
                        if not self.pair_compatible_in_room(product, a, b, rid):
                            model.Add(x[(a.booking_id, rid)] + x[(b.booking_id, rid)] <= 1)
            if total_satisfied is not None:
                model.Add(
                    sum(x.values()) == int(total_satisfied)
                )
            if total_upgrade is not None:
                model.Add(
                    sum(
                        (product.level(rid) - booking.min_level) * x[(booking.booking_id, rid)]
                        for booking in records
                        for rid in eligible[booking.booking_id]
                    )
                    == int(total_upgrade)
                )
            if fixed_satisfied:
                for booking_id, rid in fixed_satisfied.items():
                    model.Add(x[(booking_id, rid)] == 1)
            for booking_id in fixed_unsatisfied:
                for rid in eligible[booking_id]:
                    model.Add(x[(booking_id, rid)] == 0)
            return model, x, {"rank": rank, "sentinel": sentinel}

        def solve_model(model: cp_model.CpModel) -> tuple[str, dict[tuple[str, str], int]]:
            solver = cp_model.CpSolver()
            solver.parameters.num_search_workers = 8
            solver.parameters.random_seed = 0
            solver.parameters.max_time_in_seconds = 120.0
            status = solver.Solve(model)
            name = solver.StatusName(status)
            if status != cp_model.OPTIMAL:
                return name, {}
            return name, {
                key: int(solver.Value(var))
                for key, var in x.items()
            }

        # Stage 1: maximise the number of satisfied bookings.
        model, x, meta = build()
        model.Maximize(sum(x.values()))
        status, values = solve_model(model)
        if status != "OPTIMAL":
            raise ValidationError(f"could not prove the optimal plan: {status}")
        best_count = sum(values.values())

        # Stage 2: among maximum-satisfaction plans, minimise upgrade cost.
        upgrade_expr = sum(
            (product.level(rid) - booking.min_level) * x[(booking.booking_id, rid)]
            for booking in records
            for rid in eligible[booking.booking_id]
        )
        model, x, meta = build(total_satisfied=best_count)
        model.Minimize(upgrade_expr)
        status, values = solve_model(model)
        if status != "OPTIMAL":
            raise ValidationError(
                f"could not prove the minimum-upgrade plan: {status}"
            )
        best_upgrade = sum(
            (product.level(rid) - booking.min_level)
            for booking in records
            for rid in eligible[booking.booking_id]
            if values[(booking.booking_id, rid)]
        )

        # Stage 3: lexicographic minimisation of the room-id vector.
        fixed_satisfied: dict[str, str] = {}
        fixed_unsatisfied: list[str] = []
        for booking in records:
            model, x, meta = build(
                fixed_satisfied=fixed_satisfied,
                fixed_unsatisfied=fixed_unsatisfied,
                total_satisfied=best_count,
                total_upgrade=best_upgrade,
            )
            booking_id = booking.booking_id
            rank_expr = meta["sentinel"] - sum(
                (meta["sentinel"] - rank[rid]) * x[(booking_id, rid)]
                for rid in eligible[booking_id]
            )
            model.Minimize(rank_expr)
            status, values = solve_model(model)
            if status != "OPTIMAL":
                raise ValidationError(
                    f"lexicographic tie-break failed for booking {booking_id!r}: {status}"
                )
            chosen = [
                rid for rid in eligible[booking_id] if values[(booking_id, rid)]
            ]
            if chosen:
                chosen_rank = min(rank[rid] for rid in chosen)
                chosen_room = next(rid for rid in chosen if rank[rid] == chosen_rank)
                fixed_satisfied[booking_id] = chosen_room
            else:
                fixed_unsatisfied.append(booking_id)

        assignments = tuple(
            Assignment(
                booking_id=booking.booking_id,
                room_id=fixed_satisfied[booking.booking_id],
                arrival=booking.arrival,
                departure=booking.departure,
                party_size=booking.party_size,
                min_level=booking.min_level,
                max_level=booking.max_level,
                room_level=product.level(fixed_satisfied[booking.booking_id]),
                upgrade_delta=product.level(fixed_satisfied[booking.booking_id])
                - booking.min_level,
            )
            for booking in records
            if booking.booking_id in fixed_satisfied
        )
        unsatisfied_ids = [
            booking.booking_id
            for booking in records
            if booking.booking_id not in fixed_satisfied
        ]
        return {
            "assignments": assignments,
            "unsatisfied_ids": unsatisfied_ids,
            "objective": {
                "satisfied": best_count,
                "total_bookings": len(records),
                "upgrade_sum": best_upgrade,
                "lexicographic_room_vector": [
                    [booking.booking_id, fixed_satisfied.get(booking.booking_id)]
                    for booking in records
                ],
                "unsatisfied_sorts_after_all_room_ids": True,
                "solver": "OR-Tools CP-SAT",
                "priority": [
                    "max_satisfied",
                    "min_upgrade_sum",
                    "lexicographic_room_ids_with_unsatisfied_last",
                ],
            },
        }

    # -- publishing --------------------------------------------------------

    def publish(
        self,
        product: StateProduct,
        bookings: BookingSlice,
        label: str | None = None,
    ) -> Plan:
        """Solve and persist a new immutable plan.

        The supplied product is embedded in the plan; no later room-state
        change can alter what this plan means.
        """

        result = self.solve(product, bookings)
        objective = result["objective"]
        assignments: tuple[Assignment, ...] = result["assignments"]
        self._verify(product, assignments)

        unsatisfied = tuple(
            self._explain_unsatisfied(
                product, bookings, assignments, booking_id
            )
            for booking_id in result["unsatisfied_ids"]
        )

        with self.db.tx():
            cursor = self.db.execute(
                "INSERT INTO plans(label, state_version, product_digest,"
                " product_json, slice_digest, slice_json, objective_json,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    label,
                    product.version,
                    product.digest,
                    product.product_json,
                    bookings.digest,
                    bookings.slice_json,
                    canonical_json(dict(objective)),
                    utcnow(),
                ),
            )
            plan_id = int(cursor.lastrowid)
            self.db.executemany(
                "INSERT INTO plan_assignments(plan_id, booking_id, room_id,"
                " arrival, departure, party_size, min_level, max_level,"
                " room_level, upgrade_delta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        plan_id,
                        item.booking_id,
                        item.room_id,
                        item.arrival,
                        item.departure,
                        item.party_size,
                        item.min_level,
                        item.max_level,
                        item.room_level,
                        item.upgrade_delta,
                    )
                    for item in assignments
                ],
            )
            self.db.executemany(
                "INSERT INTO plan_unsatisfied(plan_id, booking_id, reason,"
                " detail_json) VALUES (?, ?, ?, ?)",
                [
                    (
                        plan_id,
                        item.booking_id,
                        item.reason,
                        canonical_json(dict(item.detail)),
                    )
                    for item in unsatisfied
                ],
            )
        return self.plan(plan_id)

    def _verify(
        self,
        product: StateProduct,
        assignments: Sequence[Assignment],
    ) -> None:
        """Refuse to publish a plan that does not consume *this* product."""

        seen: set[str] = set()
        per_room: dict[str, list[Assignment]] = {}
        for item in assignments:
            if item.booking_id in seen:
                raise ProductMismatch(
                    f"booking {item.booking_id!r} appears twice (no splitting)"
                )
            seen.add(item.booking_id)
            if not product.has_room(item.room_id):
                raise ProductMismatch(
                    f"room {item.room_id!r} is absent from product "
                    f"v{product.version}"
                )
            booking = _as_booking(item)
            if not self.room_eligible(product, booking, item.room_id):
                raise ProductMismatch(
                    f"assignment {item.booking_id!r}->{item.room_id!r} is not "
                    f"legal in product v{product.version}"
                )
            if item.upgrade_delta != product.level(item.room_id) - item.min_level:
                raise ProductMismatch(
                    f"assignment {item.booking_id!r} carries an inconsistent upgrade delta"
                )
            per_room.setdefault(item.room_id, []).append(item)
        for rid, items in per_room.items():
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    if not self.pair_compatible_in_room(
                        product,
                        _as_booking(items[i]),
                        _as_booking(items[j]),
                        rid,
                    ):
                        raise ProductMismatch(
                            f"room {rid!r} cannot host bookings "
                            f"{items[i].booking_id!r} and "
                            f"{items[j].booking_id!r} together"
                        )

    def _explain_unsatisfied(
        self,
        product: StateProduct,
        bookings: BookingSlice,
        assignments: Sequence[Assignment],
        booking_id: str,
    ) -> Unsatisfied:
        record = bookings.by_id()[booking_id]
        eligible = self.eligible_rooms(product, record)
        if not eligible:
            detail = self._no_room_detail(product, record)
            return Unsatisfied(booking_id, "no_legal_room", detail)

        assigned = [item for item in assignments if item.room_id in eligible]

        def clashes(item: Assignment) -> bool:
            return not self.pair_compatible_in_room(
                product, record, _as_booking(item), item.room_id
            )

        competitors = sorted(
            {
                item.booking_id
                for item in assigned
                if clashes(item)
            }
        )
        return Unsatisfied(
            booking_id,
            "competition",
            {
                "eligible_rooms": eligible,
                "competing_bookings": competitors,
                "note": (
                    "the plan maximises satisfied bookings, so at least one "
                    "eligible room is held by a competing booking"
                ),
            },
        )

    def _no_room_detail(
        self, product: StateProduct, record: BookingRecord
    ) -> dict[str, Any]:
        reasons: dict[str, list[str]] = {
            "capacity": [],
            "level": [],
            "horizon": [],
            "maintenance": [],
        }
        for rid in product.room_ids():
            room = product.rooms[rid]
            failed = False
            if int(room["capacity"]) < record.party_size:
                reasons["capacity"].append(rid)
                failed = True
            level = int(room["level"])
            if level < record.min_level or level > record.max_level:
                reasons["level"].append(rid)
                failed = True
            hstart, hend = product.horizon
            if record.arrival < hstart or record.departure > hend:
                reasons["horizon"].append(rid)
                failed = True
            if intersects(
                [(int(s), int(e)) for s, e in room["blocked"]],
                record.arrival,
                record.departure,
            ):
                reasons["maintenance"].append(rid)
                failed = True
            if not failed:  # Should be unreachable: eligible_rooms found it.
                reasons["level"].append(rid)
        return {
            "rooms_rejected_by": reasons,
            "checked_product_version": product.version,
            "product_digest": product.digest,
        }

    # -- reading plans -----------------------------------------------------

    def plan(self, plan_id: int) -> Plan:
        row = self.db.one("SELECT * FROM plans WHERE plan_id = ?", (int(plan_id),))
        if row is None:
            raise NotFound(f"plan {plan_id} does not exist")
        assignments = tuple(
            Assignment(
                booking_id=item["booking_id"],
                room_id=item["room_id"],
                arrival=int(item["arrival"]),
                departure=int(item["departure"]),
                party_size=int(item["party_size"]),
                min_level=int(item["min_level"]),
                max_level=int(item["max_level"]),
                room_level=int(item["room_level"]),
                upgrade_delta=int(item["upgrade_delta"]),
            )
            for item in self.db.all(
                "SELECT * FROM plan_assignments WHERE plan_id = ?"
                " ORDER BY booking_id",
                (int(plan_id),),
            )
        )
        unsatisfied = tuple(
            Unsatisfied(
                booking_id=item["booking_id"],
                reason=item["reason"],
                detail=json.loads(item["detail_json"]),
            )
            for item in self.db.all(
                "SELECT * FROM plan_unsatisfied WHERE plan_id = ?"
                " ORDER BY booking_id",
                (int(plan_id),),
            )
        )
        slice_watermark = json.loads(row["slice_json"]).get("watermark")
        return Plan(
            plan_id=int(row["plan_id"]),
            label=row["label"],
            state_version=int(row["state_version"]),
            product_digest=row["product_digest"],
            slice_digest=row["slice_digest"],
            slice_watermark=slice_watermark,
            objective=json.loads(row["objective_json"]),
            assignments=assignments,
            unsatisfied=unsatisfied,
            product_json=row["product_json"],
            slice_json=row["slice_json"],
            created_at=row["created_at"],
        )

    def plans(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM plans ORDER BY plan_id")
        return [
            {
                "plan_id": int(row["plan_id"]),
                "label": row["label"],
                "state_version": int(row["state_version"]),
                "product_digest": row["product_digest"],
                "slice_digest": row["slice_digest"],
                "objective": json.loads(row["objective_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def latest_plan(self) -> Plan:
        plan_id = self.db.scalar("SELECT MAX(plan_id) FROM plans")
        if plan_id is None:
            raise NotFound("no plan has been published")
        return self.plan(int(plan_id))

    def assignment_digest(self, plan: Plan) -> str:
        return digest(
            [
                [item.booking_id, item.room_id]
                for item in plan.assignments
            ]
        )


# ---------------------------------------------------------------------------
# Helpers


def _as_booking(item: Assignment) -> BookingRecord:
    return BookingRecord(
        booking_id=item.booking_id,
        revision=0,
        arrival=item.arrival,
        departure=item.departure,
        party_size=item.party_size,
        min_level=item.min_level,
        max_level=item.max_level,
    )


def _product_view(product_json: str) -> dict[str, Any]:
    content = json.loads(product_json)
    rooms = content["rooms"]
    content["rooms"] = {rid: rooms[rid] for rid in sorted(rooms)}
    return content
