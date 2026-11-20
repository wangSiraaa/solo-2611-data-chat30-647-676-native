"""Thin hotel facade used by the demo and tests.

Importing this module is optional: the room-state service and the fulfilment
vault are usable independently.  The facade reduces boilerplate and implements
one policy that the two independent modules cannot decide for themselves:
a committed room-state change publishes a *new* plan rather than overwriting
the old one.  Old plans remain readable forever.
"""

from __future__ import annotations

from typing import Any

from .bookings import BookingSlice, BookingStore
from .db import Database
from .fulfilment import Fulfilment, Plan
from .state import RoomStateService, StateProduct


class Hotel:
    def __init__(
        self,
        db: Database,
        horizon: tuple[int, int] = (0, 30),
    ) -> None:
        self.db = db
        self.state = RoomStateService(db, horizon=horizon)
        self.bookings = BookingStore(db)
        self.fulfilment = Fulfilment(db)

    # -- room state --------------------------------------------------------

    def register_room(self, room_id: str, level: int, capacity: int,
                      cleaning_days: int) -> int:
        return self.state.register_room(room_id, level, capacity, cleaning_days)

    def add_maintenance(self, maintenance_id: str, room_id: str,
                        start: int, end: int) -> dict[str, Any]:
        return self.state.add_maintenance(maintenance_id, room_id, start, end)

    def replace_maintenance(self, maintenance_id: str, revision: int,
                            room_id: str, start: int, end: int) -> dict[str, Any]:
        return self.state.replace_maintenance(
            maintenance_id, revision, room_id, start, end
        )

    def revoke_maintenance(self, maintenance_id: str, revision: int,
                           room_id: str | None = None) -> dict[str, Any]:
        return self.state.revoke_maintenance(maintenance_id, revision, room_id)

    def product(self, version: int | None = None) -> StateProduct:
        return self.state.product(version)

    # -- maintenance with automatic re-planning -----------------------------

    def add_maintenance_and_replan(
        self, maintenance_id: str, room_id: str, start: int, end: int,
        label: str | None = None,
    ) -> tuple[dict[str, Any], Plan | None]:
        receipt = self.add_maintenance(maintenance_id, room_id, start, end)
        return receipt, self.publish_if_changed(label=label)

    def replace_maintenance_and_replan(
        self, maintenance_id: str, revision: int, room_id: str,
        start: int, end: int, label: str | None = None,
    ) -> tuple[dict[str, Any], Plan | None]:
        receipt = self.replace_maintenance(
            maintenance_id, revision, room_id, start, end
        )
        return receipt, self.publish_if_changed(label=label)

    def revoke_maintenance_and_replan(
        self, maintenance_id: str, revision: int,
        room_id: str | None = None, label: str | None = None,
    ) -> tuple[dict[str, Any], Plan | None]:
        receipt = self.revoke_maintenance(maintenance_id, revision, room_id)
        return receipt, self.publish_if_changed(label=label)

    # -- bookings ----------------------------------------------------------

    def submit(self, booking_id: str, revision: int, arrival: int, departure: int,
               party_size: int, min_level: int, max_level: int) -> dict[str, Any]:
        return self.bookings.submit(
            booking_id, revision, arrival, departure, party_size,
            min_level, max_level,
        )

    def cancel(self, booking_id: str, revision: int) -> dict[str, Any]:
        return self.bookings.cancel(booking_id, revision)

    def slice(self, watermark: int | None = None) -> BookingSlice:
        return self.bookings.snapshot(watermark)

    # -- fulfilment --------------------------------------------------------

    def publish(
        self,
        product: StateProduct | None = None,
        slice_: BookingSlice | None = None,
        label: str | None = None,
    ) -> Plan:
        """Publish a plan from an explicit product and slice.

        Defaults resolve to the latest product and current slice for
        convenience in demos; correctness-critical callers should pass both
        explicitly.
        """

        if product is None:
            product = self.product()
        if slice_ is None:
            slice_ = self.slice()
        return self.fulfilment.publish(product, slice_, label=label)

    def publish_if_changed(self, label: str | None = None) -> Plan | None:
        """Publish only when product or booking slice differs from the latest plan.

        No plan exists yet -> returns ``None``; callers decide when to start.
        A no-op maintenance replay leaves both digests unchanged, so it
        correctly does not spawn a plan.
        """

        latest_id = self.db.scalar("SELECT MAX(plan_id) FROM plans")
        if latest_id is None:
            return None
        latest = self.fulfilment.plan(int(latest_id))
        product = self.product()
        slice_ = self.slice()
        if (
            latest.product_digest == product.digest
            and latest.slice_digest == slice_.digest
        ):
            return None
        return self.fulfilment.publish(product, slice_, label=label)

    def plan(self, plan_id: int) -> Plan:
        return self.fulfilment.plan(plan_id)

    def plans(self) -> list[dict[str, Any]]:
        return self.fulfilment.plans()

    def latest_plan(self) -> Plan:
        return self.fulfilment.latest_plan()
