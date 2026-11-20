"""Exact single-layer 2D rectangle placement with 90-degree rotation.

Coordinates are non-negative integers. Rectangles may touch (shared edges are
fine) but interiors must not overlap. Feasibility is decided by exhaustive
depth-first search over integer positions — never by area comparison alone.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    """Placed rectangle: occupies [x, x+w) x [y, y+h)."""
    x: int
    y: int
    w: int
    h: int

    def overlaps(self, other: "Rect") -> bool:
        # Strict inequalities: touching edges do NOT count as overlap.
        return (self.x < other.x + other.w and other.x < self.x + self.w
                and self.y < other.y + other.h and other.y < self.y + self.h)


def oriented_dims(length: int, width: int, rotated: int) -> tuple[int, int]:
    """(w, h) of an item after applying the rotation flag."""
    return (width, length) if rotated else (length, width)


def find_canonical_placement(
    items: tuple[tuple[str, int, int], ...] | list[tuple[str, int, int]],
    inner_length: int,
    inner_width: int,
) -> tuple[tuple[str, int, int, int], ...] | None:
    """Find the canonical (lexicographically minimal) feasible placement.

    ``items`` is a sequence of ``(unit_id, length, width)``. Returns a tuple of
    ``(unit_id, x, y, rotated)`` sorted by ``unit_id``, or ``None`` if the set
    cannot be placed in one layer inside the given rectangle.

    Canonical order: items are placed in ascending ``unit_id`` order; the
    search tries candidate ``(x, y, rotated)`` in ascending lexicographic
    order, so the first complete placement found is the lexicographically
    minimal one over the vector ``((x1,y1,r1), (x2,y2,r2), ...)``.
    """
    order = sorted(items, key=lambda t: t[0])
    n = len(order)
    placed: list[Rect] = []
    chosen: list[tuple[int, int, int]] = []

    def dfs(i: int) -> bool:
        if i == n:
            return True
        _uid, length, width = order[i]
        for x in range(inner_length):
            for y in range(inner_width):
                for rot in (0, 1):
                    w, h = oriented_dims(length, width, rot)
                    if x + w > inner_length or y + h > inner_width:
                        continue
                    rect = Rect(x, y, w, h)
                    if any(rect.overlaps(p) for p in placed):
                        continue
                    placed.append(rect)
                    chosen.append((x, y, rot))
                    if dfs(i + 1):
                        return True
                    placed.pop()
                    chosen.pop()
        return False

    if dfs(0):
        return tuple((order[i][0], *chosen[i]) for i in range(n))
    return None


def validate_explicit(
    placements: list[tuple[str, int, int, int, int, int]],
    inner_length: int,
    inner_width: int,
) -> list[str]:
    """Validate explicit placements against a box interior.

    Each entry is ``(unit_id, x, y, rotated, length, width)`` where
    ``length``/``width`` are the item's registered dimensions. Checks:
    non-negative integer coordinates, inside bounds (after rotation), and
    pairwise non-overlap of interiors.
    """
    violations: list[str] = []
    rects: list[tuple[str, Rect]] = []
    for (uid, x, y, rot, length, width) in placements:
        if not all(isinstance(v, int) for v in (x, y, rot)):
            violations.append(f"{uid}: coordinates/rotation must be integers")
            continue
        if rot not in (0, 1):
            violations.append(f"{uid}: rotated must be 0 or 1")
            continue
        if x < 0 or y < 0:
            violations.append(f"{uid}: coordinates must be non-negative")
            continue
        w, h = oriented_dims(length, width, rot)
        if x + w > inner_length or y + h > inner_width:
            violations.append(
                f"{uid}: extends outside box interior "
                f"({x}+{w}x{y}+{h} vs {inner_length}x{inner_width})")
            continue
        rects.append((uid, Rect(x, y, w, h)))
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if rects[i][1].overlaps(rects[j][1]):
                violations.append(
                    f"{rects[i][0]} overlaps {rects[j][0]}")
    return violations
