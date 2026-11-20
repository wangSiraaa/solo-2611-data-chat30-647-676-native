"""Transport loading optimizer (运输装载).

Pure and deterministic. Input: sealed boxes (each with a destination store)
and vehicle shifts. A sealed box is an indivisible unit: it is never split,
never duplicated onto two vehicles, never repacked. One vehicle may serve
several stores; its cost is ``fixed_fee + (#distinct stores) * stop_fee``.

Optimization hierarchy (exact enumeration with branch & bound):
  1. minimize total cost;
  2. minimize the number of used vehicles;
  3. minimize the shift-id vector ordered by sealed box id (lexicographic).

Gross weights are taken from the sealed boxes as-is — the empty box weight
is already included, so vehicle loads are never under-counted.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import (BoxFeasibility, InfeasibilityReport, Shift,
                     TransportAssignment)


@dataclass(frozen=True)
class ShippableBox:
    sealed_box_id: str
    zone: str
    gross_weight: int
    store_id: str


@dataclass(frozen=True)
class TransportSolution:
    assignments: tuple[TransportAssignment, ...]  # sorted by sealed_box_id
    total_cost: int
    vehicle_count: int

    def shift_vector(self) -> tuple[str, ...]:
        return tuple(a.shift_id for a in self.assignments)


def optimize_transport(boxes: list[ShippableBox],
                       shifts: list[Shift],
                       ) -> TransportSolution | InfeasibilityReport:
    """Assign every box to exactly one shift, optimally.

    Returns a :class:`TransportSolution`, or an :class:`InfeasibilityReport`
    listing every box and the unmet constraints when infeasible.
    """
    if not boxes:
        raise ValueError("no boxes to ship")
    boxes = sorted(boxes, key=lambda b: b.sealed_box_id)
    shifts = sorted(shifts, key=lambda s: s.shift_id)
    n, m = len(boxes), len(shifts)

    # per (box, shift) admissibility ignoring other boxes
    admissible = [[_box_fits_shift(b, s) for s in shifts] for b in boxes]

    best: dict = {"key": None, "assign": None}

    # per-shift running state
    load = [0] * m
    count = [0] * m
    stores: list[set] = [set() for _ in range(m)]
    assign = [-1] * n

    def current_cost() -> int:
        c = 0
        for j in range(m):
            if count[j]:
                c += shifts[j].fixed_fee + len(stores[j]) * shifts[j].stop_fee
        return c

    def recurse(i: int) -> None:
        if best["key"] is not None and current_cost() > best["key"][0]:
            return  # cost lower bound prune (fees are non-negative)
        if i == n:
            cost = current_cost()
            vehicles = sum(1 for j in range(m) if count[j])
            vector = tuple(shifts[assign[k]].shift_id for k in range(n))
            key = (cost, vehicles, vector)
            if best["key"] is None or key < best["key"]:
                best["key"] = key
                best["assign"] = list(assign)
            return
        b = boxes[i]
        for j in range(m):
            if not admissible[i][j]:
                continue
            s = shifts[j]
            if count[j] + 1 > s.max_boxes:
                continue
            if load[j] + b.gross_weight > s.max_gross_weight:
                continue
            load[j] += b.gross_weight
            count[j] += 1
            stores[j].add(b.store_id)
            assign[i] = j
            recurse(i + 1)
            assign[i] = -1
            count[j] -= 1
            load[j] -= b.gross_weight
            # rebuild store set (another box on this shift may share the store)
            stores[j] = {boxes[k].store_id for k in range(i)
                         if assign[k] == j}

    recurse(0)

    if best["assign"] is None:
        return _build_infeasibility_report(boxes, shifts, admissible)

    assignments = tuple(
        TransportAssignment(sealed_box_id=boxes[i].sealed_box_id,
                            store_id=boxes[i].store_id,
                            shift_id=shifts[best["assign"][i]].shift_id,
                            gross_weight=boxes[i].gross_weight)
        for i in range(n))
    return TransportSolution(assignments=assignments,
                             total_cost=best["key"][0],
                             vehicle_count=best["key"][1])


def _box_fits_shift(b: ShippableBox, s: Shift) -> bool:
    return (b.zone in s.allowed_zones
            and b.gross_weight <= s.max_gross_weight
            and s.max_boxes >= 1)


def _build_infeasibility_report(boxes, shifts, admissible
                                ) -> InfeasibilityReport:
    per_box: list[BoxFeasibility] = []
    unmet: list[str] = []
    for i, b in enumerate(boxes):
        compat = [s.shift_id for j, s in enumerate(shifts) if admissible[i][j]]
        blocking: list[str] = []
        if not compat:
            zone_ok = [s for s in shifts if b.zone in s.allowed_zones]
            if not zone_ok:
                blocking.append(
                    f"zone {b.zone} not accepted by any shift")
            heavy = [s for s in zone_ok
                     if b.gross_weight > s.max_gross_weight]
            if heavy and len(heavy) == len(zone_ok) and zone_ok:
                blocking.append(
                    f"gross weight {b.gross_weight} exceeds capacity of"
                    f" every zone-compatible shift")
            if zone_ok and not heavy:
                blocking.append("no individually compatible shift")
            unmet.append(
                f"box {b.sealed_box_id} (store {b.store_id}): "
                + "; ".join(blocking))
        per_box.append(BoxFeasibility(
            sealed_box_id=b.sealed_box_id, zone=b.zone,
            gross_weight=b.gross_weight, store_id=b.store_id,
            compatible_shifts=tuple(compat), blocking=tuple(blocking)))

    total_gross = sum(b.gross_weight for b in boxes)
    total_cap = sum(s.max_gross_weight for s in shifts)
    total_max_boxes = sum(s.max_boxes for s in shifts)
    if total_gross > total_cap:
        unmet.append(
            f"aggregate: total gross {total_gross} > total capacity"
            f" {total_cap}")
    if len(boxes) > total_max_boxes:
        unmet.append(
            f"aggregate: {len(boxes)} boxes > total box slots"
            f" {total_max_boxes}")
    # per-zone aggregate capacity check
    zones = {b.zone for b in boxes}
    for z in sorted(zones):
        need = sum(b.gross_weight for b in boxes if b.zone == z)
        have = sum(s.max_gross_weight for s in shifts
                   if z in s.allowed_zones)
        if need > have:
            unmet.append(
                f"zone {z}: demand {need} > compatible capacity {have}")
    if not unmet:
        unmet.append(
            "boxes are individually shippable but no joint assignment"
            " satisfies all vehicle weight/count limits")
    return InfeasibilityReport(
        per_box=tuple(per_box),
        unmet_constraints=tuple(unmet),
        total_gross_weight=total_gross,
        total_capacity=total_cap,
        total_boxes=len(boxes),
        total_max_boxes=total_max_boxes)
