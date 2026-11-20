"""Exact rational value helpers.

All physics in this project is computed with :class:`fractions.Fraction`; no
binary floating point ever touches a phase, a wavelength, a displacement or a
search bound.  Floats are accepted on input only as a convenience and are
converted through their *decimal string* so that ``0.3`` means ``3/10`` rather
than the binary approximation ``5404319552844595/18014398509481984``.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Union

Number = Union[int, Fraction, str, float]

__all__ = [
    "Q",
    "Number",
    "ceil_frac",
    "floor_frac",
    "round_frac",
    "is_integral",
]


def Q(value: Number) -> Fraction:
    """Convert ``value`` to an exact :class:`Fraction`.

    ``float`` inputs are captured via ``repr`` (shortest decimal round-trip),
    so a literal ``0.1`` becomes ``1/10`` and never the underlying binary
    expansion.  This keeps synthetic boundaries such as a closed displacement
    bound ``[Dlo, Dhi]`` exactly where the author wrote them.
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):  # bool is an int subclass; keep it predictable
        raise TypeError("booleans are not valid rational quantities")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    if isinstance(value, float):
        # repr() yields the shortest decimal that round-trips to this float.
        return Fraction(repr(value))
    raise TypeError(f"cannot convert {type(value).__name__} to Fraction")


def floor_frac(value: Fraction) -> int:
    """Floor a rational to an ``int`` (Python ``//`` floors, unlike ``int``)."""
    return value.numerator // value.denominator


def ceil_frac(value: Fraction) -> int:
    """Ceiling of a rational without any floating point."""
    return -((-value.numerator) // value.denominator)


def round_frac(value: Fraction) -> int:
    """Round half to even, exactly (banker's rounding on rationals)."""
    return round(value)


def is_integral(value: Fraction) -> bool:
    return value.denominator == 1
