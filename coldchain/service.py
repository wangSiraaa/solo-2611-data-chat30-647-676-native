"""Facade wiring the packing, sealing and transport services together.

Concurrency & recovery model
----------------------------
* All shared state lives in one SQLite database (WAL) — never only in memory.
* Every multi-row mutation runs in a single ``BEGIN IMMEDIATE`` transaction
  (unified transaction / recoverable commit): it either commits fully or
  rolls back, so crashes and conflicts never leave partial reservations or
  half-sealed batches.
* Transport plans carry a ``version``; confirm/cancel/depart/replace require
  the caller's expected version (optimistic concurrency) on top of the
  transaction.
* Confirming a plan atomically reserves all its sealed boxes and vehicle
  shifts. Unique primary keys on the reservation tables are the hard
  backstop: two clients confirming overlapping plans concurrently — exactly
  one commits; the loser gets :class:`ReservationConflictError` and holds
  nothing.
"""
from __future__ import annotations

import json

from . import packing as packing_mod
from .errors import (InvalidStateError, NotFoundError, ReservationConflictError,
                     TransportInfeasibleError, VersionConflictError)
from .models import (BoxType, InfeasibilityReport, Item, PlanStatus, Shift,
                     TransportPlan)
from .packing import canonical_json
from .repository import Repository, new_plan_id
from .sealing import SealingService
from .transport import ShippableBox, optimize_transport


class ColdChainService:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.sealing = SealingService(repo)

    # ------------------------------------------------------------------
    # Master data
    # ------------------------------------------------------------------

    def register_item(self, unit_id: str, length: int, width: int,
                      weight: int, zone: str) -> Item:
        with self.repo.write_tx() as con:
            item = Item(unit_id=unit_id, length=length, width=width,
                        weight=weight, zone=zone, version=1)
            self.repo.upsert_item(con, item)
            return item

    def update_item_dims(self, unit_id: str, *, length: int | None = None,
                         width: int | None = None,
                         weight: int | None = None) -> Item:
        """Change item dimensions -> a new item version.

        Old sealed boxes keep their snapshots untouched; existing unsealed
        plans become stale and can no longer be sealed.
        """
        with self.repo.write_tx() as con:
            cur = self.repo.get_item(con, unit_id)
            if cur is None:
                raise NotFoundError(f"item {unit_id} not registered")
            item = Item(unit_id=unit_id,
                        length=length if length is not None else cur.length,
                        width=width if width is not None else cur.width,
                        weight=weight if weight is not None else cur.weight,
                        zone=cur.zone, version=cur.version + 1)
            self.repo.upsert_item(con, item)
            return item

    def register_box_type(self, box_type_id: str, inner_length: int,
                          inner_width: int, empty_weight: int,
                          max_gross_weight: int,
                          allowed_zones: set[str] | frozenset[str]) -> None:
        with self.repo.write_tx() as con:
            self.repo.upsert_box_type(con, box_type_id, inner_length,
                                      inner_width, empty_weight,
                                      max_gross_weight, allowed_zones)

    def register_shift(self, shift_id: str, allowed_zones, max_gross_weight,
                       max_boxes: int, fixed_fee: int, stop_fee: int) -> None:
        with self.repo.write_tx() as con:
            self.repo.upsert_shift(con, Shift(
                shift_id=shift_id, allowed_zones=frozenset(allowed_zones),
                max_gross_weight=max_gross_weight, max_boxes=max_boxes,
                fixed_fee=fixed_fee, stop_fee=stop_fee))

    # ------------------------------------------------------------------
    # Packing (independently callable by packers)
    # ------------------------------------------------------------------

    def plan_packing(self):
        """Optimize a packing plan over all currently registered items."""
        with self.repo.read() as con:
            items = self.repo.all_items(con)
            types = [BoxType(
                box_type_id=r["box_type_id"],
                inner_length=r["inner_length"],
                inner_width=r["inner_width"],
                empty_weight=r["empty_weight"],
                max_gross_weight=r["max_gross_weight"],
                allowed_zones=frozenset(json.loads(r["allowed_zones"])))
                for r in self.repo.all_box_types(con)]
        plan = packing_mod.optimize(items, types)
        with self.repo.write_tx() as con:
            self.repo.save_packing_plan(
                con, plan.plan_id, plan.normalization,
                canonical_json(plan), dict(plan.item_versions))
        return plan

    # ------------------------------------------------------------------
    # Transport planning
    # ------------------------------------------------------------------

    def plan_transport(self, shipments: dict[str, str],
                       shift_ids: list[str] | None = None,
                       ) -> TransportPlan | InfeasibilityReport:
        """Build an optimal transport plan for sealed boxes -> stores.

        ``shipments`` maps sealed_box_id -> destination store id. Reads the
        actual sealed gross weights (empty box weight included). Returns the
        persisted PLANNED plan, or an InfeasibilityReport covering every box.
        """
        result = self._compute_transport(shipments, shift_ids)
        if isinstance(result, InfeasibilityReport):
            return result
        plan = TransportPlan(plan_id=new_plan_id(), status=PlanStatus.PLANNED,
                             version=1, assignments=result.assignments,
                             total_cost=result.total_cost,
                             vehicle_count=result.vehicle_count)
        with self.repo.write_tx() as con:
            self.repo.insert_transport_plan(con, plan)
        return plan

    def _compute_transport(self, shipments: dict[str, str],
                           shift_ids: list[str] | None):
        with self.repo.read() as con:
            missing = [sid for sid in shipments
                       if self.repo.get_sealed_box(con, sid) is None]
            if missing:
                raise NotFoundError(f"unknown sealed boxes: {missing}")
            boxes = []
            for sid, store in shipments.items():
                box = self.repo.get_sealed_box(con, sid)
                boxes.append(ShippableBox(
                    sealed_box_id=sid, zone=box.zone,
                    gross_weight=box.gross_weight, store_id=store))
            if shift_ids is None:
                shifts = self.repo.all_shifts(con)
            else:
                shifts = []
                for shid in shift_ids:
                    s = self.repo.get_shift(con, shid)
                    if s is None:
                        raise NotFoundError(f"unknown shift {shid}")
                    shifts.append(s)
        return optimize_transport(boxes, shifts)

    def get_transport_plan(self, plan_id: str) -> TransportPlan:
        with self.repo.read() as con:
            plan = self.repo.get_transport_plan(con, plan_id)
        if plan is None:
            raise NotFoundError(f"transport plan {plan_id} not found")
        return plan

    # ------------------------------------------------------------------
    # Confirmation / cancellation / departure / replacement
    # ------------------------------------------------------------------

    def confirm(self, plan_id: str, expected_version: int) -> TransportPlan:
        """Atomically reserve all boxes and shifts of the plan."""
        with self.repo.write_tx() as con:
            plan = self._load_for_update(con, plan_id, expected_version)
            if plan.status != PlanStatus.PLANNED:
                raise InvalidStateError(
                    f"plan {plan_id} is {plan.status.value}, not PLANNED")
            self._reserve(con, plan)
            self.repo.update_plan_status(con, plan_id, PlanStatus.CONFIRMED,
                                         expected_version)
            return self.repo.get_transport_plan(con, plan_id)

    def cancel(self, plan_id: str, expected_version: int) -> TransportPlan:
        """Release the plan's reservations. Departed plans cannot cancel."""
        with self.repo.write_tx() as con:
            plan = self._load_for_update(con, plan_id, expected_version)
            if plan.status == PlanStatus.DEPARTED:
                raise InvalidStateError(
                    f"plan {plan_id} already departed; cannot cancel")
            if plan.status in (PlanStatus.CANCELLED, PlanStatus.REPLACED):
                raise InvalidStateError(
                    f"plan {plan_id} already {plan.status.value}")
            self.repo.release_reservations(con, plan_id)
            self.repo.update_plan_status(con, plan_id, PlanStatus.CANCELLED,
                                         expected_version)
            return self.repo.get_transport_plan(con, plan_id)

    def depart(self, plan_id: str, expected_version: int) -> TransportPlan:
        """Mark a confirmed plan departed. Reservations stay as history."""
        with self.repo.write_tx() as con:
            plan = self._load_for_update(con, plan_id, expected_version)
            if plan.status != PlanStatus.CONFIRMED:
                raise InvalidStateError(
                    f"plan {plan_id} is {plan.status.value}, not CONFIRMED")
            self.repo.update_plan_status(con, plan_id, PlanStatus.DEPARTED,
                                         expected_version)
            return self.repo.get_transport_plan(con, plan_id)

    def replace(self, old_plan_id: str, shipments: dict[str, str],
                expected_version: int,
                shift_ids: list[str] | None = None) -> TransportPlan:
        """Atomically swap a confirmed plan for a new packing product.

        The old plan must be CONFIRMED (not departed). Its reservations are
        released, it is marked REPLACED, and the new plan — computed from the
        new sealed boxes, so changed box counts and gross weights propagate —
        is inserted already CONFIRMED with its own reservations, all in one
        transaction. Old sealed boxes and departed facts remain untouched.
        """
        new_result = self._compute_transport(shipments, shift_ids)
        if isinstance(new_result, InfeasibilityReport):
            raise TransportInfeasibleError(
                "replacement plan is infeasible; old plan kept", new_result)
        with self.repo.write_tx() as con:
            old = self._load_for_update(con, old_plan_id, expected_version)
            if old.status != PlanStatus.CONFIRMED:
                raise InvalidStateError(
                    f"plan {old_plan_id} is {old.status.value};"
                    f" only CONFIRMED plans can be replaced")
            self.repo.release_reservations(con, old_plan_id)
            self.repo.update_plan_status(con, old_plan_id,
                                         PlanStatus.REPLACED,
                                         expected_version)
            confirmed = TransportPlan(
                plan_id=new_plan_id(), status=PlanStatus.CONFIRMED,
                version=1, assignments=new_result.assignments,
                total_cost=new_result.total_cost,
                vehicle_count=new_result.vehicle_count)
            self.repo.insert_transport_plan(con, confirmed)
            self._reserve(con, confirmed)
            return self.repo.get_transport_plan(con, confirmed.plan_id)

    # ------------------------------------------------------------------
    # Traceability
    # ------------------------------------------------------------------

    def trace_unit(self, unit_id: str) -> list[dict]:
        with self.repo.read() as con:
            return self.repo.trace_unit(con, unit_id)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _load_for_update(self, con, plan_id: str,
                         expected_version: int) -> TransportPlan:
        plan = self.repo.get_transport_plan(con, plan_id)
        if plan is None:
            raise NotFoundError(f"transport plan {plan_id} not found")
        if plan.version != expected_version:
            raise VersionConflictError(
                f"plan {plan_id}: expected version {expected_version},"
                f" current {plan.version}")
        return plan

    def _reserve(self, con, plan: TransportPlan) -> None:
        box_ids = [a.sealed_box_id for a in plan.assignments]
        shift_ids = sorted({a.shift_id for a in plan.assignments})
        box_hits, shift_hits = self.repo.conflicting_reservations(
            con, box_ids, shift_ids)
        if box_hits or shift_hits:
            raise ReservationConflictError(
                f"plan {plan.plan_id} conflicts with active reservations",
                boxes=box_hits, shifts=shift_hits)
        self.repo.insert_reservations(con, plan.plan_id, box_ids, shift_ids)
