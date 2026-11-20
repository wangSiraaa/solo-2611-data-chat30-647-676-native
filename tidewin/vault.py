"""Vessel passage-window vault.

Semantics
---------
A channel is an ordered list of segments.  Segment ``i`` has rational minimum
depth ``depth_i >= 0``, rational sailing duration ``0 < duration_i <= 12``
hours, and belongs to a port with a tide model (referenced by port + published
revision).  A vessel has rational draft ``draft > 0`` and rational margin
``margin >= 0``.  With departure ``s`` the vessel occupies segment ``i`` during
the *closed* interval

    W_i(s) = [ s + offset_i , s + offset_i + duration_i ],
    offset_i = sum_{j<i} duration_j

and must satisfy, for *every* instant in ``W_i(s)``,

    depth_i + level_i(t) >= draft + margin        (equality is passable).

Let ``req_i = draft + margin - depth_i``.  Segment ``i`` is satisfied at
departure ``s`` iff the whole window ``W_i(s)`` lies inside the port's safe set

    SAFE_i = { u : level_i(u) >= req_i }.

Algorithm (complete, no sampling grid)
--------------------------------------
1. ``SAFE_i`` over the needed span is built from the *exact* roots of
   ``level_i(u) = req_i``.  Roots come from the unit-circle polynomial solved in
   Gaussian-rational arithmetic (see :mod:`tidewin.gaussian`): the square-free
   part guarantees that tangent / double roots survive, so a level that only
   *touches* ``req`` is never lost.  The roots partition the span; each piece is
   classified by the exact sign of ``level - req`` at a midpoint, giving a union
   of closed intervals (plus isolated touching points).
2. A window of positive length ``duration_i`` fits only inside a *single* safe
   interval.  The set of admissible window starts is therefore the erosion
   ``U_i = { u : [u, u+duration_i] subset SAFE_i }`` = union of
   ``[a, b - duration_i]`` over safe intervals ``[a,b]`` with ``b - a >=
   duration_i``.  When ``b - a == duration_i`` exactly this collapses to the
   isolated point ``a`` -- the zero-width tangential passage, preserved.
3. Shift each ``U_i`` back to departure time: ``s = u - offset_i``.  Every
   segment uses its own model and its own offset, so we never intersect raw port
   windows while ignoring the sailing offsets.
4. Intersect the shifted sets across all segments and with ``[S, E]``.  Closed
   sets are intersected exactly; intervals that only touch at one point are
   merged.

All model math is done at high precision; boundary times are reported with far
better than the required 1e-7 h accuracy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Tuple

import mpmath as mp

from .rational import F as _F, frac_str

TOUCH = 1e-9          # endpoints closer than this are the same boundary
                      # (far below the required 1e-7 h accuracy)
WIDTH_TOL = 1e-9      # erosion equality tolerance (tangency detection)
RESID_TOL = 1e-7      # |residual| below this => tide (not range) boundary

Interval = Tuple[float, float]


# --------------------------------------------------------------------------- #
# interval-set primitives (closed intervals; [p,p] is an isolated point)
# --------------------------------------------------------------------------- #
def merge_intervals(ivs: Sequence[Interval], tol: float = TOUCH) -> List[Interval]:
    """Sort and merge overlapping intervals and intervals that touch at a point.

    Two intervals separated by a genuine unsafe gap wider than ``tol`` are kept
    apart; ``tol`` is ~1e-9, two orders of magnitude below the 1e-7 requirement,
    so it never erases a real positive-width unsafe gap.
    """
    if not ivs:
        return []
    ivs = sorted((min(a, b), max(a, b)) for a, b in ivs)
    out: List[Interval] = []
    for a, b in ivs:
        if out and a <= out[-1][1] + tol:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def intersect_sets(A: Sequence[Interval], B: Sequence[Interval],
                   tol: float = TOUCH) -> List[Interval]:
    """Exact intersection of two unions of closed intervals."""
    if not A or not B:
        return []
    res: List[Interval] = []
    for a1, a2 in A:
        for b1, b2 in B:
            lo = max(a1, b1)
            hi = min(a2, b2)
            if lo <= hi:
                res.append((lo, hi))
            elif lo - hi <= tol:
                mid = 0.5 * (lo + hi)
                res.append((mid, mid))
    return merge_intervals(res, tol)


def clip(intervals: Sequence[Interval], lo: float, hi: float) -> List[Interval]:
    return intersect_sets(intervals, [(lo, hi)])


# --------------------------------------------------------------------------- #
# data structures
# --------------------------------------------------------------------------- #
@dataclass
class Segment:
    port: str
    revision: Optional[int]     # None => current published revision
    depth: Fraction
    duration: Fraction

    def to_dict(self) -> dict:
        return {"port": self.port,
                "revision": self.revision if self.revision is not None else "current",
                "depth": frac_str(self.depth), "duration": frac_str(self.duration)}


@dataclass
class Vessel:
    draft: Fraction
    margin: Fraction

    def to_dict(self) -> dict:
        return {"draft": frac_str(self.draft), "margin": frac_str(self.margin)}


@dataclass
class Channel:
    name: str
    segments: List[Segment]

    def offsets(self) -> List[Fraction]:
        off = Fraction(0)
        offsets = []
        for seg in self.segments:
            offsets.append(off)
            off += seg.duration
        return offsets

    def to_dict(self) -> dict:
        return {"name": self.name,
                "segments": [s.to_dict() for s in self.segments]}


@dataclass
class BoundaryInfo:
    """Explanation of one boundary of a feasible interval."""
    time: float
    kind: str                 # "tide" (constraint) or "range" (request limit)
    segment_index: int        # 1-based active segment (0 if none)
    port: Optional[str]
    revision: Optional[int]
    tide_time: float          # instant inside the segment's occupancy window
    residual: float           # min(level - req) over the window (>=0 feasible)
    all_constraints: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "time": self.time,
            "kind": self.kind,
            "segment_index": self.segment_index,
            "port": self.port,
            "revision": self.revision,
            "tide_time": self.tide_time,
            "residual": self.residual,
            "all_constraints": self.all_constraints,
        }


@dataclass
class FeasibleWindow:
    start: float
    end: float
    start_boundary: Optional[BoundaryInfo] = None
    end_boundary: Optional[BoundaryInfo] = None

    @property
    def isolated(self) -> bool:
        return abs(self.end - self.start) <= TOUCH

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "width": max(0.0, self.end - self.start),
            "isolated": self.isolated,
            "start_boundary": self.start_boundary.to_dict()
            if self.start_boundary else None,
            "end_boundary": self.end_boundary.to_dict()
            if self.end_boundary else None,
        }


# --------------------------------------------------------------------------- #
# per-segment safe set and its erosion
# --------------------------------------------------------------------------- #
def _req_mp(req: Fraction) -> mp.mpf:
    mp.mp.dps = 80
    return mp.mpf(req.numerator) / req.denominator


def safe_set(model, req: Fraction, lo: float, hi: float) -> List[Interval]:
    """SAFE = { u in [lo,hi] : model.level(u) >= req }, as closed intervals.

    Complete via exact roots of level = req (square-free, tangency-safe) plus
    midpoint sign classification.  No fixed sampling grid is used.
    """
    if lo >= hi:
        # degenerate range: a single point
        if model.is_constant():
            ok = model.b >= req
        else:
            ok = model.level_mp(lo) - _req_mp(req) >= 0
        return [(lo, hi if hi > lo else lo)] if ok else []

    if model.is_constant():
        return [(lo, hi)] if model.b >= req else []

    roots = model.level_roots(req, lo, hi)          # times with level == req
    interior = [r for r in roots if lo + TOUCH < r < hi - TOUCH]

    boundaries = [lo] + interior + [hi]
    boundaries.sort()
    # dedupe
    B: List[float] = []
    for t in boundaries:
        if not B or abs(t - B[-1]) > TOUCH:
            B.append(t)

    rmp = _req_mp(req)
    safe: List[Interval] = []
    for j in range(len(B) - 1):
        a, b = B[j], B[j + 1]
        if b - a <= TOUCH:
            continue
        mid = 0.5 * (a + b)
        # strictly nonzero on the open piece (roots partition it)
        if model.level_mp(mid) - rmp > 0:
            safe.append((a, b))

    # isolated touching roots not covered by a safe piece
    for r in interior:
        covered = any(a - TOUCH <= r <= b + TOUCH for a, b in safe)
        if not covered:
            safe.append((r, r))

    # domain endpoints that are themselves safe but isolated
    for e in (lo, hi):
        if not any(a - TOUCH <= e <= b + TOUCH for a, b in safe):
            if model.level_mp(e) - rmp >= 0:
                safe.append((e, e))

    return merge_intervals(safe)


def erode_by_duration(safe: Sequence[Interval], duration: float) -> List[Interval]:
    """Set of window starts u with [u, u+duration] fully inside ``safe``."""
    out: List[Interval] = []
    for a, b in safe:
        width = b - a
        if width >= duration - WIDTH_TOL:
            start = a
            end = b - duration
            if end - start <= WIDTH_TOL:   # tangential / within tolerance
                end = start
            out.append((start, end))
    return merge_intervals(out)


# --------------------------------------------------------------------------- #
# the solver
# --------------------------------------------------------------------------- #
def _resolve_model(catalog, seg: Segment):
    return catalog.get_model(seg.port, seg.revision)


def segment_feasible_starts(model, req: Fraction, duration: float,
                            u_lo: float, u_hi: float) -> List[Interval]:
    """Admissible window-start times u in [u_lo,u_hi] for one segment."""
    # safe set must cover window ends up to u_hi + duration
    safe = safe_set(model, req, u_lo, u_hi + duration)
    starts = erode_by_duration(safe, duration)
    return clip(starts, u_lo, u_hi)


def solve_request(catalog, channel: Channel, vessel: Vessel,
                  S: float, E: float) -> dict:
    """Compute every maximal feasible departure interval and isolated time."""
    if E < S:
        raise ValueError("departure range requires S <= E")
    if not channel.segments:
        raise ValueError("channel needs at least one segment")

    draft, margin = vessel.draft, vessel.margin
    offsets = channel.offsets()
    seg_constraints = []           # (seg, offset, req, model, duration)
    for i, seg in enumerate(channel.segments):
        req = draft + margin - seg.depth
        model = _resolve_model(catalog, seg)
        seg_constraints.append((seg, float(offsets[i]), req, model,
                                float(seg.duration)))
    feasible: List[Interval] = [(float(S), float(E))]
    for (seg, off, req, model, dur) in seg_constraints:
        u_lo = float(S) + off
        u_hi = float(E) + off
        starts_u = segment_feasible_starts(model, req, dur, u_lo, u_hi)
        # shift window start back to departure time s = u - offset
        shifted = merge_intervals([(a - off, b - off) for a, b in starts_u])
        feasible = intersect_sets(feasible, clip(shifted, float(S), float(E)))

    windows: List[FeasibleWindow] = []
    for a, b in merge_intervals(feasible):
        sw = _boundary(catalog, channel, vessel, seg_constraints, a)
        ew = _boundary(catalog, channel, vessel, seg_constraints, b)
        windows.append(FeasibleWindow(a, b, sw, ew))

    status = "FEASIBLE" if windows else "INFEASIBLE"
    return {
        "status": status,
        "S": float(S),
        "E": float(E),
        "channel": channel.name,
        "vessel": vessel.to_dict(),
        "windows": [w.to_dict() for w in windows],
        "n_windows": len(windows),
        "n_isolated": sum(1 for w in windows if w.isolated),
    }


def _boundary(catalog, channel: Channel, vessel: Vessel, seg_constraints,
              s: float) -> BoundaryInfo:
    """Explain the active constraint at departure time ``s``.

    For each segment we take the minimum of ``level - req`` over the whole
    occupancy window and its argmin instant (the binding tide time).  The active
    segment is the one with the smallest residual.
    """
    all_c = []
    best = None
    for idx, (seg, off, req, model, dur) in enumerate(seg_constraints, start=1):
        t0 = s + off
        t1 = t0 + dur
        minres, argt = model.min_safety_margin(req, t0, t1)
        rec = {
            "segment_index": idx,
            "port": seg.port,
            "revision": model.revision,
            "depth": frac_str(seg.depth),
            "duration": frac_str(seg.duration),
            "window": [t0, t1],
            "tide_time": argt,
            "residual": minres,
            "satisfied": minres >= -RESID_TOL,
        }
        all_c.append(rec)
        if best is None or minres < best[1]:
            best = (rec, minres)

    rec, minres = best
    kind = "tide" if abs(minres) <= RESID_TOL else "range"
    return BoundaryInfo(
        time=float(s), kind=kind,
        segment_index=rec["segment_index"] if kind == "tide" else 0,
        port=rec["port"] if kind == "tide" else None,
        revision=rec["revision"] if kind == "tide" else None,
        tide_time=rec["tide_time"],
        residual=float(minres),
        all_constraints=all_c,
    )


# --------------------------------------------------------------------------- #
# independent checking helpers
# --------------------------------------------------------------------------- #
def check_window_point(catalog, channel: Channel, vessel: Vessel,
                       s: float) -> dict:
    """Brute check of a single departure time (uses exact window minima)."""
    constraints = []
    ok = True
    offsets = channel.offsets()
    for idx, seg in enumerate(channel.segments, start=1):
        req = vessel.draft + vessel.margin - seg.depth
        model = _resolve_model(catalog, seg)
        off = float(offsets[idx - 1])
        t0 = s + off
        t1 = t0 + float(seg.duration)
        minres, argt = model.min_safety_margin(req, t0, t1)
        satisfied = minres >= -1e-9
        ok = ok and satisfied
        constraints.append({"segment_index": idx, "port": seg.port,
                            "revision": model.revision,
                            "window": [t0, t1], "tide_time": argt,
                            "residual": minres, "satisfied": satisfied})
    return {"s": s, "feasible": ok, "constraints": constraints}


def check_window_point_fast(catalog, channel: Channel, vessel: Vessel,
                            s: float) -> bool:
    """Fast float-only feasibility check for heuristic grid falsifiers.

    Uses the model's *exact* critical times (so the window minimum is still
    complete) but evaluates levels in double precision.  NOT used to determine
    boundaries -- only to independently refute/confirm points in verification.
    """
    offsets = channel.offsets()
    for idx, seg in enumerate(channel.segments):
        req = float(vessel.draft + vessel.margin - seg.depth)
        model = _resolve_model(catalog, seg)
        off = float(offsets[idx])
        t0 = s + off
        t1 = t0 + float(seg.duration)
        pts = [t0]
        if not model.is_constant():
            pts += model.critical_times(t0, t1)
        pts.append(t1)
        if min(model.level_fast(t) for t in pts) < req - 1e-12:
            return False
    return True
