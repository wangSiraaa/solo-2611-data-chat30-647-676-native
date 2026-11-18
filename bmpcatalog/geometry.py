"""Logical, top-left-origin half-open rectangles.

Every public rectangle in this package uses logical image coordinates:
``x`` grows to the right, ``y`` grows downwards, and the rectangle is
half-open (``x0 <= x < x1``, ``y0 <= y < y1``).  BMP storage order is handled
only by the catalog layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import RectangleError, RectangleOutOfBounds


def _require_int(value: object, name: str) -> int:
    # bool is an int subclass; rejecting it avoids surprising True/False input.
    if isinstance(value, bool) or not isinstance(value, int):
        raise RectangleError(f"{name} must be an int, got {type(value).__name__}")
    return value


@dataclass(frozen=True, slots=True)
class Rectangle:
    """A half-open logical rectangle with positive area."""

    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self) -> None:
        for name in ("x0", "y0", "x1", "y1"):
            _require_int(getattr(self, name), name)
        if self.x0 >= self.x1:
            raise RectangleError(f"empty rectangle width: x0={self.x0}, x1={self.x1}")
        if self.y0 >= self.y1:
            raise RectangleError(f"empty rectangle height: y0={self.y0}, y1={self.y1}")

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def area(self) -> int:
        return self.width * self.height

    def intersect(self, other: "Rectangle") -> "Rectangle | None":
        """Return the overlap, or ``None`` when the rectangles are disjoint."""

        x0 = max(self.x0, other.x0)
        y0 = max(self.y0, other.y0)
        x1 = min(self.x1, other.x1)
        y1 = min(self.y1, other.y1)
        if x0 >= x1 or y0 >= y1:
            return None
        return Rectangle(x0, y0, x1, y1)

    def overlaps(self, other: "Rectangle") -> bool:
        return (
            self.x0 < other.x1
            and other.x0 < self.x1
            and self.y0 < other.y1
            and other.y0 < self.y1
        )

    def contains(self, other: "Rectangle") -> bool:
        return (
            self.x0 <= other.x0
            and self.y0 <= other.y0
            and other.x1 <= self.x1
            and other.y1 <= self.y1
        )

    def validate_inside(self, width: int, height: int) -> "Rectangle":
        """Reject any part of the rectangle that is outside ``[0,w) x [0,h)``.

        The input rectangle is returned unchanged, which makes this convenient
        to use as a query validator.
        """

        if self.x0 < 0 or self.y0 < 0 or self.x1 > width or self.y1 > height:
            raise RectangleOutOfBounds(
                f"rectangle ({self.x0},{self.y0},{self.x1},{self.y1}) "
                f"is outside image bounds {width}x{height}"
            )
        return self

    def to_tuple(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)

    def __iter__(self):
        yield self.x0
        yield self.y0
        yield self.x1
        yield self.y1


def as_rectangle(value: object) -> Rectangle:
    """Coerce a rectangle-like value to :class:`Rectangle`.

    Accepted forms are ``Rectangle`` and a 4-item iterable
    ``(x0, y0, x1, y1)``.  Strings and mappings are rejected rather than
    treated as sequences of characters/keys.
    """

    if isinstance(value, Rectangle):
        return value
    if isinstance(value, (str, bytes, bytearray, dict)):
        raise RectangleError(f"cannot interpret {type(value).__name__} as a rectangle")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise RectangleError(
            f"expected Rectangle or 4 integers, got {type(value).__name__}"
        ) from exc
    if len(items) != 4:
        raise RectangleError(f"expected 4 rectangle values, got {len(items)}")
    return Rectangle(items[0], items[1], items[2], items[3])


def coerce_rectangles(values: Iterable[object]) -> tuple[Rectangle, ...]:
    return tuple(as_rectangle(value) for value in values)
