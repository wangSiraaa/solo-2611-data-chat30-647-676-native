"""Tide models and the exact critical-point machinery.

A port model is

    g(t) = b + sum_{k=1..4} [ a_k * cos(k*w*t) + c_k * sin(k*w*t) ],   w = pi/12

with ``b, a_k, c_k`` rational and ``t`` in hours.  The level is 24-hour
periodic (``w*24 = 2*pi``).

Critical times (places where ``g`` may attain a local extremum) are exactly the
zeros of ``g'``.  ``g'`` is again a finite trigonometric polynomial, so we build
its unit-circle polynomial, reduce it to its exact square-free part over Q(i)
and solve that with ``mpmath.polyroots``.  Using the *derivative* is the key:
a tangency of the level (a double root of ``g - threshold``) happens exactly at
a zero of ``g'`` where the extremum equals the threshold, so it is never missed
and never needs a sampling grid.
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

import mpmath as mp

from . import gaussian
from .rational import F as _F, frac_str

W = math.pi / 12.0            # fixed angular frequency, rad / hour
PERIOD = 24.0                 # hours, since k*w*24 = 2*pi*k

# high-precision working precision for decisively classifying "is this exactly
# zero?"  Exact algebraic zeros collapse to ~1e-<dps>; genuine nonzero values do
# not.  This is used *only* for zero/nonzero classification and residuals.
DPS = 80


def _mpf(x: Fraction) -> mp.mpf:
    return mp.mpf(x.numerator) / x.denominator


class TideModel:
    """A synthetic port tide model product (one revision)."""

    __slots__ = ("port", "revision", "b", "a", "c", "_mpcoef", "_base_crit",
                 "_level_base_cache_key", "_level_base_cache", "_fcoef", "note")

    def __init__(self, port: str, b, a, c, revision: int = 1, note: str = ""):
        self.port = port
        self.revision = int(revision)
        self.b = _F(b)
        # a / c may be given as scalar / list / dict keyed by k in 1..4
        self.a = self._series(a)
        self.c = self._series(c)
        self.note = note or ""
        self._mpcoef = None
        self._base_crit: Optional[List[float]] = None
        self._level_base_cache_key = None
        self._level_base_cache: List[float] = []
        self._fcoef = None

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _series(src) -> List[Fraction]:
        out = [Fraction(0)] * 4
        if src is None:
            return out
        if isinstance(src, dict):
            for k, v in src.items():
                k = int(k)
                if not (1 <= k <= 4):
                    raise ValueError("harmonic order k must be 1..4")
                out[k - 1] = _F(v)
            return out
        if isinstance(src, (list, tuple)):
            if len(src) > 4:
                raise ValueError("at most 4 harmonics")
            for i, v in enumerate(src):
                out[i] = _F(v)
            return out
        out[0] = _F(src)          # single scalar means k = 1
        return out

    def _mp(self):
        if self._mpcoef is None:
            mp.mp.dps = DPS
            self._mpcoef = (
                _mpf(self.b),
                [_mpf(x) for x in self.a],
                [_mpf(x) for x in self.c],
            )
        return self._mpcoef

    def _float_coef(self):
        if self._fcoef is None:
            self._fcoef = (float(self.b),
                           [float(x) for x in self.a],
                           [float(x) for x in self.c])
        return self._fcoef

    def level_fast(self, t: float) -> float:
        """Fast float-only level (heuristic grid checks only; never used to
        decide a boundary -- the exact algorithm uses high precision)."""
        tb, A, C = self._float_coef()
        th = math.pi * t / 12.0
        val = tb
        for k in range(1, 5):
            val += A[k - 1] * math.cos(k * th) + C[k - 1] * math.sin(k * th)
        return val

    def derivative_fast(self, t: float) -> float:
        tb, A, C = self._float_coef()
        th = math.pi * t / 12.0
        val = 0.0
        for k in range(1, 5):
            val += k * (-A[k - 1] * math.sin(k * th) + C[k - 1] * math.cos(k * th))
        return (math.pi / 12.0) * val

    # --------------------------------------------------------------- evaluation
    def level_mp(self, t) -> mp.mpf:
        """Level at ``t`` hours, high precision."""
        mp.mp.dps = DPS
        tb, A, C = self._mp()
        t = mp.mpf(t)
        th = mp.pi * t / 12
        val = tb
        for k in range(1, 5):
            val += A[k - 1] * mp.cos(k * th) + C[k - 1] * mp.sin(k * th)
        return val

    def level(self, t) -> float:
        return float(self.level_mp(t))

    def derivative_mp(self, t) -> mp.mpf:
        mp.mp.dps = DPS
        tb, A, C = self._mp()
        t = mp.mpf(t)
        th = mp.pi * t / 12
        # d/dt = w * d/dtheta
        val = mp.mpf(0)
        for k in range(1, 5):
            val += k * (-A[k - 1] * mp.sin(k * th) + C[k - 1] * mp.cos(k * th))
        return (mp.pi / 12) * val

    def local_terms_mp(self, t) -> Dict[str, object]:
        """Per-term contribution breakdown at ``t`` (for the catalog query)."""
        mp.mp.dps = DPS
        tb, A, C = self._mp()
        t = mp.mpf(t)
        th = mp.pi * t / 12
        terms = []
        total = tb
        for k in range(1, 5):
            cosc = A[k - 1] * mp.cos(k * th)
            sinc = C[k - 1] * mp.sin(k * th)
            terms.append({
                "k": k,
                "a": frac_str(self.a[k - 1]),
                "c": frac_str(self.c[k - 1]),
                "cos_term": float(cosc),
                "sin_term": float(sinc),
                "term": float(cosc + sinc),
            })
            total += cosc + sinc
        return {"b": frac_str(self.b), "constant": float(tb),
                "terms": terms, "total": float(total)}

    # ------------------------------------------------------- critical times
    def is_constant(self) -> bool:
        return all(x == 0 for x in self.a) and all(x == 0 for x in self.c)

    def _critical_thetas(self) -> List[float]:
        """All theta in [0, 2*pi) where g'(theta)=0, refined to double."""
        # derivative coefficients: sum_k [k*c_k cos(k th) - k*a_k sin(k th)]
        da = [Fraction(k) * self.c[k - 1] for k in range(1, 5)]       # cos coeffs
        dc = [-Fraction(k) * self.a[k - 1] for k in range(1, 5)]      # sin coeffs
        P = gaussian.build_critical_polynomial(Fraction(0), da, dc)
        if not P:
            return []
        S = gaussian.squarefree_part(P)
        if S is None:
            return []            # nonzero constant derivative: no critical times
        # coefficients, highest degree first, as mp complex
        mp.mp.dps = DPS
        coeffs = []
        for coef in reversed(S):
            coeffs.append(mp.mpc(_mpf(coef[0]), _mpf(coef[1])))
        rts = mp.polyroots(coeffs, maxsteps=1000, extraprec=200)

        scale = max(1.0, sum(abs(c) for c in coeffs))
        thetas: List[float] = []
        for z in rts:
            if abs(abs(z) - 1) > 1e-6:
                continue
            th = mp.arg(z)
            if th < 0:
                th += 2 * mp.pi
            # verify it truly zeroes the derivative
            resid = self.derivative_mp(th_mp(th))
            if abs(resid) > mp.mpf(1e-30) * scale:
                continue
            ta = float(th)
            if ta < 1e-9 or ta > 2 * math.pi - 1e-9:
                ta = 0.0
            if ta >= 2 * math.pi:
                ta = 0.0
            thetas.append(ta)
        thetas.sort()
        # dedupe
        uniq: List[float] = []
        for ta in thetas:
            if not uniq or abs(ta - uniq[-1]) > 1e-7:
                uniq.append(ta)
        return uniq

    def base_critical_times(self) -> List[float]:
        """Critical times in one period [0, 24)."""
        if self._base_crit is None:
            self._base_crit = [12.0 * th / math.pi for th in self._critical_thetas()]
        return self._base_crit

    def critical_times(self, lo: float, hi: float) -> List[float]:
        """All critical times t with lo < t < hi (strictly interior)."""
        if self.is_constant():
            return []
        base = self.base_critical_times()
        out: List[float] = []
        for t0 in base:
            n = math.floor((lo - t0) / PERIOD) + 1
            t = t0 + n * PERIOD
            while t <= hi + 1e-9:
                if t > lo + 1e-12 and t < hi - 1e-12:
                    out.append(t)
                n += 1
                t = t0 + n * PERIOD
        out.sort()
        return out

    # ------------------------------------------------------------- extrema
    def extrema(self, lo: float, hi: float) -> Dict[str, object]:
        """Exact-interval extrema: evaluate endpoints and every critical point.

        No fixed sampling grid is used; all local extrema of a finite trig
        polynomial on the interval are among these points.
        """
        if lo > hi:
            raise ValueError("extrema interval requires lo <= hi")
        crit = self.critical_times(lo, hi)
        points = [float(lo)] + crit + [float(hi)]
        values = [self.level(t) for t in points]
        # local extrema strictly interior
        local = []
        for idx in range(1, len(points) - 1):
            t = points[idx]
            v = values[idx]
            left = values[idx - 1]
            right = values[idx + 1]
            if v <= left and v <= right:
                kind = "min"
            elif v >= left and v >= right:
                kind = "max"
            else:
                kind = "inflection"
            local.append({"time": t, "value": v, "kind": kind})
        imin = min(range(len(values)), key=lambda i: values[i])
        imax = max(range(len(values)), key=lambda i: values[i])
        return {
            "port": self.port,
            "revision": self.revision,
            "lo": float(lo),
            "hi": float(hi),
            "min": {"time": points[imin], "value": values[imin]},
            "max": {"time": points[imax], "value": values[imax]},
            "critical_points": crit,
            "local_extrema": local,
        }

    # ------------------------------------------------------------- serialization
    def to_dict(self) -> Dict[str, object]:
        d = {
            "port": self.port,
            "revision": self.revision,
            "b": frac_str(self.b),
            "a": [frac_str(x) for x in self.a],
            "c": [frac_str(x) for x in self.c],
        }
        if self.note:
            d["note"] = self.note
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "TideModel":
        return cls(d["port"], d["b"],
                   [Fraction(x) for x in d["a"]],
                   [Fraction(x) for x in d["c"]],
                   revision=int(d.get("revision", 1)),
                   note=d.get("note", ""))

    def copy(self, port: Optional[str] = None, revision: Optional[int] = None,
             b=None, a=None, c=None) -> "TideModel":
        return TideModel(
            self.port if port is None else port,
            self.b if b is None else _F(b),
            self.a if a is None else self._series(a),
            self.c if c is None else self._series(c),
            self.revision if revision is None else revision,
        )

    # ------------------------------------------------ threshold crossings
    def _level_base_roots(self, req: Fraction) -> List[float]:
        """Times in [0,24) where level(t) == req (exact via Q(i) polynomials)."""
        P = gaussian.build_critical_polynomial(self.b - req, self.a, self.c)
        if not P:
            # level == req identically (only for a constant model); caller
            # handles the constant case, but be defensive.
            return []
        S = gaussian.squarefree_part(P)
        if S is None:
            return []
        mp.mp.dps = DPS
        coeffs = [mp.mpc(_mpf(coef[0]), _mpf(coef[1])) for coef in reversed(S)]
        rts = mp.polyroots(coeffs, maxsteps=1000, extraprec=200)
        scale = max(1.0, sum(abs(c) for c in coeffs))
        times: List[float] = []
        for z in rts:
            if abs(abs(z) - 1) > 1e-6:
                continue
            th = mp.arg(z)
            if th < 0:
                th += 2 * mp.pi
            # genuine zero of level-req if it is a unit-circle root
            ta = float(12 * th / mp.pi)
            if abs(ta - 24.0) < 1e-7:
                ta = 0.0
            if abs(ta) < 1e-7:
                ta = 0.0
            times.append(ta)
        times.sort()
        uniq: List[float] = []
        for ta in times:
            if not uniq or abs(ta - uniq[-1]) > 1e-7:
                uniq.append(ta)
        return uniq

    def level_roots(self, req, lo: float, hi: float) -> List[float]:
        """All t in [lo,hi] with level(t)==req, tiled across periods."""
        req = _F(req)
        if self.is_constant():
            return []
        if self._level_base_cache_key != req:
            self._level_base_cache = self._level_base_roots(req)
            self._level_base_cache_key = req
        base = self._level_base_cache
        out: List[float] = []
        if lo > hi:
            return out
        for t0 in base:
            n0 = math.floor((lo - t0) / PERIOD) - 1
            n1 = math.ceil((hi - t0) / PERIOD) + 1
            for n in range(n0, n1 + 1):
                t = t0 + n * PERIOD
                if lo - 1e-9 <= t <= hi + 1e-9:
                    t = min(max(t, lo), hi)
                    out.append(t)
        out.sort()
        uniq: List[float] = []
        for t in out:
            if not uniq or abs(t - uniq[-1]) > 1e-8:
                uniq.append(t)
        return uniq

    # ------------------------------------------------ min over a window
    def min_level_over(self, t0: float, t1: float) -> Tuple[float, float]:
        """Return (min level, argmin time) over the closed window [t0,t1].

        Evaluates endpoints plus every critical point in the interior; since a
        finite trig polynomial's extrema are exactly at these points, this is a
        complete minimum (no sampling grid).
        """
        pts = [float(t0)]
        if t1 > t0:
            pts += self.critical_times(t0, t1)
        pts.append(float(t1))
        best_t = pts[0]
        best_v = self.level(pts[0])
        for t in pts[1:]:
            v = self.level(t)
            if v < best_v:
                best_v = v
                best_t = t
        return best_v, best_t

    def min_safety_margin(self, req, t0: float, t1: float) -> Tuple[float, float]:
        """min over window of (level(t) - req), and its argmin time."""
        v, t = self.min_level_over(t0, t1)
        return v - float(req), t



def th_mp(th) -> mp.mpf:
    """theta (radians) expressed in hours for reuse of derivative evaluator."""
    return mp.mpf(th) * 12 / mp.pi
