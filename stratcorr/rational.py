"""Canonical non-negative rational numbers stored as ``"num/den"`` strings.

Depths and thicknesses are rational metres.  Canonical form:

* denominator is a positive integer,
* numerator is a non-negative integer,
* the fraction is fully reduced (gcd == 1), integers are written ``"n/1"``.

Arithmetic uses :class:`fractions.Fraction`; only the canonical string is
persisted so comparisons and ordering are exact (no float drift at gap and
sample boundaries).
"""

from __future__ import annotations

from fractions import Fraction


class RationalError(ValueError):
    """Raised when a rational depth/thickness is malformed or negative."""


def to_fraction(value: str | int | Fraction) -> Fraction:
    """Parse a canonical rational string, int or Fraction into a Fraction."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        if value < 0:
            raise RationalError(f"rational must be non-negative: {value!r}")
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise RationalError("empty rational value")
        if "/" in text:
            num_s, den_s = text.split("/", 1)
            try:
                num = int(num_s)
                den = int(den_s)
            except ValueError as exc:
                raise RationalError(f"malformed rational {value!r}") from exc
        else:
            try:
                num = int(text)
            except ValueError as exc:
                raise RationalError(f"malformed rational {value!r}") from exc
            den = 1
        if den <= 0:
            raise RationalError(f"denominator must be positive: {value!r}")
        if num < 0:
            raise RationalError(f"rational must be non-negative: {value!r}")
        return Fraction(num, den)
    raise RationalError(f"unsupported rational type: {type(value)!r}")


def to_str(value: str | int | Fraction) -> str:
    """Return the canonical ``"num/den"`` representation."""
    frac = to_fraction(value)
    return f"{frac.numerator}/{frac.denominator}"


def add(a: str, b: str) -> str:
    return to_str(to_fraction(a) + to_fraction(b))


def sub(a: str, b: str) -> str:
    result = to_fraction(a) - to_fraction(b)
    if result < 0:
        raise RationalError(f"negative subtraction result: {a} - {b}")
    return to_str(result)


def is_positive(value: str | int | Fraction) -> bool:
    return to_fraction(value) > 0


def compare(a: str, b: str) -> int:
    """Three-way comparison on canonical strings: -1, 0 or 1."""
    fa, fb = to_fraction(a), to_fraction(b)
    return -1 if fa < fb else (1 if fa > fb else 0)
