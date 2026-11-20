"""Analytic reference solutions for independent verification.

These are closed-form and independent of the matrix-exponential solver.
``single_room`` is *phase based*: ``c0`` is the concentration at time ``a``
and the result/integral cover ``[a, b]``.
"""

from __future__ import annotations

import math


def single_room(V, q, s, c0, a, b):
    """One room, supply==exhaust==q, constant source s, c(a)=c0.

    Solves V dc/dt = s - q c.  Returns ``(c(b), integral_a^b c dt)``.
    """
    k = q / V
    tau = b - a
    if q == 0:
        # pure accumulation: c(t) = c0 + s (t-a)/V
        c_b = c0 + s * tau / V
        integral = c0 * tau + s / V * tau * tau / 2.0
        return c_b, integral
    c_eq = s / q
    e = math.exp(-k * tau)
    c_b = c_eq + (c0 - c_eq) * e
    integral = c_eq * tau + (c0 - c_eq) * (1.0 - e) / k
    return c_b, integral


def two_room_symmetric(V, q, r, sA, sB, c0A, c0B, t):
    """Two equal rooms, exchange q each way, supply=exhaust=r per room.

    Eigen-decomposition (phases start at t=0):
      mode m1 = (cA+cB)/2 : dc/dt = (sA+sB)/(2V) - (r/V) m1
      mode m2 = (cA-cB)/2 : dc/dt = (sA-sB)/(2V) - (2q+r)/V m2
    Returns ``(cA(t), cB(t))``.
    """
    m1_0 = (c0A + c0B) / 2.0
    m2_0 = (c0A - c0B) / 2.0
    s1 = (sA + sB) / 2.0
    s2 = (sA - sB) / 2.0
    k1 = r / V
    k2 = (2.0 * q + r) / V
    if r > 0:
        m1 = s1 / r + (m1_0 - s1 / r) * math.exp(-k1 * t)
    else:
        m1 = m1_0 + s1 * t / V
    if (2 * q + r) > 0:
        m2 = s2 / (2 * q + r) + (m2_0 - s2 / (2 * q + r)) * math.exp(-k2 * t)
    else:
        m2 = m2_0 + s2 * t / V
    return m1 + m2, m1 - m2


def two_room_symmetric_integral(V, q, r, sA, sB, c0A, c0B, a, b):
    """Integral of cA and cB over [a,b] via the same eigen-decomposition."""
    m1_0 = (c0A + c0B) / 2.0
    m2_0 = (c0A - c0B) / 2.0
    s1 = (sA + sB) / 2.0
    s2 = (sA - sB) / 2.0
    k1 = r / V
    k2 = (2.0 * q + r) / V

    def mode_integral(m0, s, k):
        if k == 0:
            return m0 * (b - a) + s / V * (b * b - a * a) / 2.0
        eq = s / (k * V)
        return eq * (b - a) + (m0 - eq) * (math.exp(-k * a) - math.exp(-k * b)) / k

    I1 = mode_integral(m1_0, s1, k1)
    I2 = mode_integral(m2_0, s2, k2)
    return I1 + I2, I1 - I2
