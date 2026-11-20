"""Rational-number helpers.

All geometry (channels, energies, coverage lengths, efficiencies, durations)
is carried as :class:`fractions.Fraction` so the per-bin allocation is exact
and every published number can be recomputed by hand.  Exponentials and the
activity inversion are inherently transcendental and stay in ``float``.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Union

Number = Union[int, str, Fraction]


def as_rational(value: Number) -> Fraction:
    """Parse ``int`` / ``Fraction`` / exact decimal string into a Fraction.

    Accepted strings: ``"3"``, ``"-2.5"``, ``"1/7"``, ``"1e3"`` is *not*
    accepted on purpose -- rational inputs must be written exactly.
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        s = value.strip()
        if "/" in s:
            return Fraction(s)
        if "e" in s or "E" in s:
            raise ValueError(f"non-exact (exponent) rational input: {value!r}")
        return Fraction(s)
    raise TypeError(f"cannot interpret {value!r} as rational")


def rational_to_str(value: Fraction) -> str:
    """Stable serialization of a Fraction."""
    return f"{value.numerator}/{value.denominator}"


def rational_to_float(value: Fraction) -> float:
    return float(value)


def require_positive(value: Fraction, name: str) -> Fraction:
    if value <= 0:
        raise ValueError(f"{name} must be a positive rational, got {value}")
    return value


def require_nonneg(value: Fraction, name: str) -> Fraction:
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")
    return value
