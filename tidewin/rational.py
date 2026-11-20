"""Exact rational arithmetic helpers.

All model coefficients, times and thresholds in this project are rational.
Internally we keep them as :class:`fractions.Fraction` so that the symbolic
algebra (square-free reduction over the Gaussian rationals) is exact.  Only at
the very last step (root finding and residual evaluation) do we descend to
floating point / mpmath.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Union

Number = Union[int, str, Fraction]


def F(x: Number, y: Number = None) -> Fraction:
    """Coerce ``x`` to an exact :class:`Fraction` (int/str/Fraction accepted).

    With a second argument, ``F(num, den)`` builds ``num/den``.
    """
    if y is not None:
        return Fraction(F(x), F(y))
    if isinstance(x, Fraction):
        return x
    if isinstance(x, int):
        return Fraction(x)
    if isinstance(x, str):
        return Fraction(x)          # "1/3", "0.25", "-2" all accepted
    raise TypeError(f"cannot coerce {x!r} to rational")


def frac_str(x: Fraction) -> str:
    """Stable, human-readable canonical serialization of a rational."""
    if x.denominator == 1:
        return str(x.numerator)
    return f"{x.numerator}/{x.denominator}"


def frac_json(x: Fraction) -> str:
    """JSON-friendly representation: ``num/den`` string."""
    return frac_str(x)
