"""Domain model dataclasses.

All dimensions are positive integers, all coordinates non-negative integers.
Everything here is immutable (`frozen=True`) so plans and sealed boxes can be
shared between threads and services without defensive copies.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Item:
    """One physical goods unit. Dimensions are current (latest) values."""
    unit_id: str
    length: int
    width: int
    weight: int
    zone: str
    version: int = 1

    def __post_init__(self):
        if self.length <= 0 or self.width <= 0 or self.weight <= 0:
            raise ValueError("item dimensions and weight must be positive integers")
        if not self.unit_id:
            raise ValueError("unit_id must be non-empty")


@dataclass(frozen=True)
class BoxType:
    box_type_id: str
    inner_length: int
    inner_width: int
    empty_weight: int
    max_gross_weight: int
    allowed_zones: frozenset[str]

    def __post_init__(self):
        if self.inner_length <= 0 or self.inner_width <= 0:
            raise ValueError("box inner dimensions must be positive integers")
        if self.empty_weight < 0:
            raise ValueError("empty weight must be >= 0")
        if self.max_gross_weight <= 0:
            raise ValueError("max gross weight must be positive")

    @property
    def area(self) -> int:
        return self.inner_length * self.inner_width


@dataclass(frozen=True)
class Shift:
    """One vehicle shift (班次)."""
    shift_id: str
    allowed_zones: frozenset[str]
    max_gross_weight: int
    max_boxes: int
    fixed_fee: int
    stop_fee: int


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Placement:
    unit_id: str
    x: int
    y: int
    rotated: int  # 0 = as-is, 1 = rotated 90 degrees


@dataclass(frozen=True)
class BoxPlan:
    """Planned content of one physical box (not yet sealed)."""
    box_type_id: str
    zone: str
    placements: tuple[Placement, ...]  # sorted by unit_id
    gross_weight: int
    empty_weight: int
    area: int


@dataclass(frozen=True)
class PackingPlan:
    """A complete, canonically ordered packing solution."""
    boxes: tuple[BoxPlan, ...]          # canonical order (see packing.NORMALIZATION)
    item_versions: tuple[tuple[str, int], ...]  # (unit_id, version) sorted by unit_id
    plan_id: str
    normalization: str

    @property
    def box_count(self) -> int:
        return len(self.boxes)

    @property
    def total_area(self) -> int:
        return sum(b.area for b in self.boxes)

    def item_ids(self) -> tuple[str, ...]:
        return tuple(uid for uid, _ in self.item_versions)


# ---------------------------------------------------------------------------
# Sealed boxes (immutable products)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SealedItem:
    """Snapshot of one item as it was sealed inside a box."""
    unit_id: str
    x: int
    y: int
    rotated: int
    length: int
    width: int
    weight: int
    zone: str


@dataclass(frozen=True)
class SealedBox:
    sealed_box_id: str
    plan_id: str
    box_type_id: str
    zone: str
    gross_weight: int
    empty_weight: int
    items: tuple[SealedItem, ...]  # sorted by unit_id
    created_at: str


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class PlanStatus(str, Enum):
    PLANNED = "PLANNED"
    CONFIRMED = "CONFIRMED"
    DEPARTED = "DEPARTED"
    CANCELLED = "CANCELLED"
    REPLACED = "REPLACED"


@dataclass(frozen=True)
class TransportAssignment:
    sealed_box_id: str
    store_id: str
    shift_id: str
    gross_weight: int


@dataclass(frozen=True)
class TransportPlan:
    plan_id: str
    status: PlanStatus
    version: int
    assignments: tuple[TransportAssignment, ...]  # sorted by sealed_box_id
    total_cost: int
    vehicle_count: int

    def shift_vector(self) -> tuple[str, ...]:
        """Shift ids ordered by sealed box id — the final tie-break key."""
        return tuple(a.shift_id for a in self.assignments)


@dataclass(frozen=True)
class BoxFeasibility:
    sealed_box_id: str
    zone: str
    gross_weight: int
    store_id: str
    compatible_shifts: tuple[str, ...]
    blocking: tuple[str, ...]  # reasons no shift can take this box, if any


@dataclass(frozen=True)
class InfeasibilityReport:
    """Structured explanation when no vehicle assignment exists.

    Every requested box is listed exactly once — boxes are never omitted.
    """
    per_box: tuple[BoxFeasibility, ...]
    unmet_constraints: tuple[str, ...]
    total_gross_weight: int
    total_capacity: int
    total_boxes: int
    total_max_boxes: int
