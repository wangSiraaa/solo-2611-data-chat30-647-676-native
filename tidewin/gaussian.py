"""Exact polynomial arithmetic over the Gaussian rationals Q(i).

The tidal model uses ``w = pi/12``.  With ``theta = w*t`` and ``z = e^{i*theta}``
we have ``cos(k*theta) = (z^k + z^-k)/2`` and ``sin(k*theta) = (z^k - z^-k)/(2i)``.
Multiplying the level equation by ``z^4`` (the highest used harmonic is k = 4)
therefore turns "find all critical times" into "find all roots of a polynomial
``P(z)`` with Gaussian-rational coefficients *that lie on the unit circle*".

Because the coefficients are exact rationals, we can perform an exact
square-free reduction: ``G = gcd(P, P')`` is computed by the Euclidean
algorithm over Q(i), then ``S = P / G`` is the square-free part.  Every
distinct time that makes ``g(t) = 0`` (including tangent/double roots such as
``1 - cos(theta)`` at ``theta = 0``) is a *simple* root of ``S``.  Numerically
solving ``S`` with ``mpmath.polyroots`` then recovers every root exactly once,
with no coalescing or root-squaring error and no dependence on a sampling grid.
"""
from __future__ import annotations

from fractions import Fraction
from typing import List, Tuple, Optional

from .rational import F

GZ = (F(0), F(0))           # 0 + 0i
GO = (F(1), F(0))           # 1 + 0i

Gauss = Tuple[Fraction, Fraction]


# --------------------------------------------------------------------------- #
# Gaussian rational scalar ops
# --------------------------------------------------------------------------- #
def gadd(a: Gauss, b: Gauss) -> Gauss:
    return (a[0] + b[0], a[1] + b[1])


def gsub(a: Gauss, b: Gauss) -> Gauss:
    return (a[0] - b[0], a[1] - b[1])


def gmul(a: Gauss, b: Gauss) -> Gauss:
    return (a[0] * b[0] - a[1] * b[1], a[0] * b[1] + a[1] * b[0])


def ginv(a: Gauss) -> Gauss:
    d = a[0] * a[0] + a[1] * a[1]
    if d == 0:
        raise ZeroDivisionError("division by Gaussian-rational zero")
    return (a[0] / d, -a[1] / d)


def gdiv(a: Gauss, b: Gauss) -> Gauss:
    return gmul(a, ginv(b))


def is_zero(a: Gauss) -> bool:
    return a[0] == 0 and a[1] == 0


# --------------------------------------------------------------------------- #
# Polynomial ops (lists, index = degree, trailing zeros stripped)
# --------------------------------------------------------------------------- #
Poly = List[Gauss]


def ptrim(p: Poly) -> Poly:
    p = list(p)
    while p and is_zero(p[-1]):
        p.pop()
    return p


def pdeg(p: Poly) -> int:
    return len(ptrim(p)) - 1


def pdivmod(a: Poly, b: Poly) -> Tuple[Poly, Poly]:
    a = ptrim(a)
    b = ptrim(b)
    if not b:
        raise ZeroDivisionError("polynomial division by zero")
    q: Poly = []
    while len(a) >= len(b) and a:
        lead = a[-1]
        ldb = b[-1]
        c = gdiv(lead, ldb)
        d = len(a) - len(b)
        while len(q) <= d:
            q.append(GZ)
        q[d] = gadd(q[d], c)
        for i in range(len(b)):
            a[d + i] = gsub(a[d + i], gmul(c, b[i]))
        a = ptrim(a)
    return ptrim(q), a


def pgcd(a: Poly, b: Poly) -> Poly:
    a = ptrim(a)
    b = ptrim(b)
    while b:
        r = pdivmod(a, b)[1]
        a, b = b, r
    return ptrim(a)


def pmonic(a: Poly) -> Poly:
    a = ptrim(a)
    if not a:
        return a
    lc = a[-1]
    return [gdiv(c, lc) for c in a]


def pdiff(a: Poly) -> Poly:
    a = ptrim(a)
    return ptrim([gmul(a[i], (F(i), F(0))) for i in range(1, len(a))])


# --------------------------------------------------------------------------- #
# Build the unit-circle critical polynomial
# --------------------------------------------------------------------------- #
def build_critical_polynomial(b: Fraction,
                              a: List[Fraction],
                              c: List[Fraction]) -> Poly:
    """Return ``P(z)`` such that ``b + sum_k a_k cos(k th) + c_k sin(k th) = 0``
    for ``z = e^{i th}`` is equivalent to ``P(z) = 0``.

    ``a`` and ``c`` hold ``a_1..a_4`` / ``c_1..c_4``.  Returns the trimmed exact
    integer-degree polynomial (degree <= 8).  An empty list means the equation
    is identically satisfied for all ``z`` (all coefficients zero).
    """
    if len(a) > 4 or len(c) > 4:
        raise ValueError("this model supports at most 4 harmonics (k=1..4)")
    a = list(a) + [F(0)] * (4 - len(a))
    c = list(c) + [F(0)] * (4 - len(c))
    deg = 8
    p: Poly = [GZ] * (deg + 1)
    p[4] = gadd(p[4], (b, F(0)))
    for k in range(1, 5):
        ak, ck = a[k - 1], c[k - 1]
        if ak == 0 and ck == 0:
            continue
        # z^{4+k}:  (a_k cos + c_k sin) -> (a_k - i c_k)/2  (multiplied by z^4)
        p[4 + k] = gadd(p[4 + k], (ak / 2, -ck / 2))
        # z^{4-k}:  (a_k + i c_k)/2
        p[4 - k] = gadd(p[4 - k], (ak / 2, ck / 2))
    return ptrim(p)


def squarefree_part(p: Poly) -> Optional[Poly]:
    """Monic square-free part of ``p`` (exact over Q(i)).

    Returns ``None`` if ``p`` is constant (no roots).  This is what guarantees
    tangent / double / higher-multiplicity critical times survive root finding.
    """
    p = ptrim(p)
    if len(p) <= 1:
        return None
    d = pdiff(p)
    if not d:
        # p is a nonzero constant already handled above; defensive.
        return None
    g = pgcd(p, d)
    q, r = pdivmod(p, g)
    assert not ptrim(r), "exact gcd failed to divide P"
    return pmonic(q)


def polynomial_orbit_bits(p: Poly) -> int:
    """Bitmask of which ``z^k`` positions actually occur (0..8).  Used only for
    diagnostics and to detect a zero polynomial cheaply."""
    bits = 0
    p = ptrim(p)
    for i, coef in enumerate(p):
        if not is_zero(coef):
            bits |= (1 << i)
    return bits
