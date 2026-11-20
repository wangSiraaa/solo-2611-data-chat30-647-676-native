"""Decay integral and reference-time conversion (the float layer).

The expected window counts follow

    N = eff * (L/T) * A * I(T, lambda),   I = integral_0^T exp(-lambda t) dt

so the activity at measurement start is inverted as

    A = N / (eff * (L/T) * I).

``lambda == 0`` takes the exact limit ``I -> T``; for small ``lambda*T`` the
integral is evaluated through ``expm1`` to avoid cancellation.
"""

from __future__ import annotations

import math
from fractions import Fraction

from .rationals import require_positive


def decay_integral(T: Fraction, lam: float) -> float:
    """Stable evaluation of integral_0^T exp(-lambda t) dt in seconds."""
    Tf = float(T)
    if lam == 0.0:
        return Tf
    x = lam * Tf
    if abs(x) < 1e-4:
        # (1 - exp(-x))/lambda via expm1: no 1-minus-near-1 cancellation.
        return -math.expm1(-x) / lam
    return (1.0 - math.exp(-x)) / lam


def invert_activity(N: Fraction, eff: Fraction, live_frac: Fraction,
                    T: Fraction, lam: float) -> float:
    """Activity A at measurement start from observed expected counts N."""
    require_positive(eff, "eff")
    require_positive(live_frac, "L/T")
    factor = float(eff) * float(live_frac) * decay_integral(T, lam)
    if factor <= 0.0:
        raise ZeroDivisionError("non-positive calibration factor")
    return float(N) / factor


def to_reference_time(A: float, lam: float, start: int, t_ref: int) -> float:
    """A_ref = A * exp(-lambda * (t_ref - start)); lambda=0 limit is A."""
    if lam == 0.0:
        return A
    x = -lam * (t_ref - start)  # positive when t_ref precedes the start
    # exp() overflows past ~709; the back-extrapolated value then saturates
    # at the largest finite float rather than crashing the quantification.
    if x > 709.0:
        return A * math.exp(709.0)
    return A * math.exp(x)


def expected_counts(A: float, eff: Fraction, live_frac: Fraction,
                    T: Fraction, lam: float) -> float:
    """Forward model, used by the checks and the hand-computed examples."""
    return A * float(eff) * float(live_frac) * decay_integral(T, lam)
