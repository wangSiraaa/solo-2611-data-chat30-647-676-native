"""Exact interval / bin geometry on the rational channel axis.

A whole channel bin ``j`` is the half-open interval ``[j, j+1)``.  A
back-projected energy window becomes an interval ``[lo, hi)`` on this axis and
its counts are split *per bin by overlap length*, never by bin centre.
Everything here is exact Fraction arithmetic.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import List, Tuple

from .rationals import as_rational


def overlap_length(lo_a: Fraction, hi_a: Fraction,
                   lo_b: Fraction, hi_b: Fraction) -> Fraction:
    """Length of intersection of two half-open intervals."""
    lo = max(lo_a, lo_b)
    hi = min(hi_a, hi_b)
    return hi - lo if hi > lo else Fraction(0)


def bins_touched(lo: Fraction, hi: Fraction) -> List[int]:
    """Indices of whole bins ``[j, j+1)`` meeting half-open interval [lo, hi).

    An endpoint landing exactly on a bin boundary does not enter the bin on
    the open side: coverage of zero length is omitted later anyway.
    """
    if hi <= lo:
        return []
    first = math.floor(lo)
    hi_floor = math.floor(hi)
    # Half-open [lo, hi): an integer hi excludes its own bin, otherwise the
    # bin containing hi is touched (with fractional, non-full coverage).
    last = hi_floor if hi == hi_floor else hi_floor + 1
    return list(range(first, last))


def bin_coverages(lo: Fraction, hi: Fraction,
                  n_bins: int) -> Tuple[List[Tuple[int, Fraction]], Fraction]:
    """Split interval ``[lo, hi)`` into per-bin overlap lengths.

    Returns ``(entries, total)`` where entries are ``(bin_index, length)``
    pairs with strictly positive length and ``total`` is their exact sum.
    Bins outside ``range(n_bins)`` are reported as well, so callers can tell
    "unknown region" apart from zero coverage -- see
    :func:`coverage_in_bounds`.
    """
    lo = as_rational(lo)
    hi = as_rational(hi)
    if hi <= lo:
        return [], Fraction(0)
    entries: List[Tuple[int, Fraction]] = []
    total = Fraction(0)
    for j in bins_touched(lo, hi):
        length = overlap_length(lo, hi, Fraction(j), Fraction(j + 1))
        if length > 0:
            entries.append((j, length))
            total += length
    return entries, total


def coverage_in_bounds(entries, n_bins: int) -> bool:
    """True iff every covered bin index is a measured channel bin."""
    return all(0 <= j < n_bins for j, _ in entries)


def counts_from_coverages(counts, entries) -> Fraction:
    """Expected window counts = sum_j counts[j] * coverage_j (bin width 1)."""
    total = Fraction(0)
    for j, length in entries:
        total += Fraction(int(counts[j])) * length
    return total
