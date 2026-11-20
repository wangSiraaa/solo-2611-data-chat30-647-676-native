"""Exact piecewise-linear propagation for a ventilation network.

The concentration dynamics are linear with a piecewise-constant source::

    V_i dc_i/dt = s_i + sum_j q_ji c_j - c_i (sum_j q_ij + q_i,out)

We propagate the augmented state ``x = [c (n), M (n), 1]`` where ``M_i`` is
the cumulative mass exhausted from room ``i`` to the outdoors.  For a segment
with constant ``s`` this is an affine autonomous linear system, so

    x(t + dt) = exp(B dt) x(t)

is *exact* up to floating-point round-off.  The matrix exponential does not
require ``B`` to be invertible, so zero-flow rooms, closed connected components
and singular systems are handled naturally.  The concentration-time integral
over the same segment is obtained from the doubly-augmented matrix

    Q = [[B, 0], [I, 0]],   exp(Q dt) = [[exp(B dt), 0], [int exp(Bs) ds, I]].

This is what makes visit exposure integrals exact rather than endpoint
trapezoids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Mapping, Sequence

import numpy as np
from scipy.linalg import expm

from .errors import LimitError, ValidationError
from .rational import fraction_to_float, parse_nonnegative, rational_str

MAX_DURATION = 10000.0


@dataclass
class Segment:
    t0: float
    t1: float
    source: np.ndarray  # mg/s, float view
    source_exact: tuple[Fraction, ...]

    @property
    def duration(self) -> float:
        return self.t1 - self.t0

    def source_input_exact(self) -> Fraction:
        return sum(self.source_exact, Fraction(0)) * Fraction(self.t1 - self.t0).limit_denominator(10**12)


@dataclass
class Timeline:
    """Sorted union of all breakpoints with piecewise-constant sources."""

    breakpoints: list[float]
    segments: list[Segment]
    room_names: list[str]
    window_start: float
    window_end: float
    source_events: list[dict] = field(default_factory=list)

    def source_vector_at(self, t: float) -> np.ndarray:
        """Source strengths active at time ``t`` (right-continuous, half-open)."""
        for seg in self.segments:
            if seg.t0 <= t < seg.t1:
                return seg.source
        if self.segments and t >= self.segments[-1].t1:
            return self.segments[-1].source
        return np.zeros(len(self.room_names))

    def source_input_between(self, a: float, b: float) -> Fraction:
        """Exact source mass injected by rooms over [a, b)."""
        total = Fraction(0)
        for seg in self.segments:
            lo, hi = max(a, seg.t0), min(b, seg.t1)
            if hi > lo:
                dur = Fraction(hi - lo).limit_denominator(10**12)
                total += sum(seg.source_exact, Fraction(0)) * dur
        return total

    def explain(self, a: float, b: float) -> list[dict]:
        """Describe the segments and constant source states used on [a, b)."""
        out = []
        for seg in self.segments:
            lo, hi = max(a, seg.t0), min(b, seg.t1)
            if hi > lo:
                out.append(
                    {
                        "t0": lo,
                        "t1": hi,
                        "duration": hi - lo,
                        "source_mg_s": {
                            name: rational_str(seg.source_exact[i])
                            for i, name in enumerate(self.room_names)
                            if seg.source_exact[i] != 0
                        },
                    }
                )
        return out


def _group_events(events: Iterable) -> dict[float, dict[int, Fraction]]:
    """Group same-second source changes into a single new state.

    ``events`` yields ``(time, room_name, strength)``.  Conflicting duplicate
    assignments to the same room at the same instant are rejected so the
    result never depends on input order.
    """
    grouped: dict[float, dict[int, Fraction]] = {}
    for ev in events:
        try:
            t, room_idx, strength = ev
        except (TypeError, ValueError) as exc:
            raise ValidationError("source event must be (time, room, strength)") from exc
        if t < 0:
            raise ValidationError(f"source change time must be >= 0, got {t}")
        s = parse_nonnegative(strength, field=f"source[{room_idx}]@{t}")
        bucket = grouped.setdefault(float(t), {})
        if room_idx in bucket and bucket[room_idx] != s:
            raise ValidationError(
                f"conflicting source strengths for room index {room_idx} at t={t}"
            )
        bucket[room_idx] = s
    return grouped


def build_timeline(
    matrix: Mapping,
    source_events: Sequence,
    window_start: float,
    window_end: float,
    extra_breakpoints: Iterable[float] = (),
) -> Timeline:
    """Build the segment timeline.

    ``source_events`` are ``(time, room_name, strength)`` tuples; a change
    applies from its time onward on a half-open interval.  All breakpoints
    (events, window ends, visit endpoints) are merged and sorted once, so
    several changes at the same second form one new state rather than a run
    of zero-length segments.
    """
    names: list[str] = matrix["names"]
    idx = matrix["index"]
    n = len(names)
    if not (0 <= window_start <= window_end <= MAX_DURATION + 1e-12):
        raise LimitError(
            f"analysis window must satisfy 0 <= start <= end <= {MAX_DURATION}"
        )

    normalized = []
    event_records = []
    for t, room, strength in source_events:
        if isinstance(room, str):
            if room not in idx:
                raise ValidationError(f"source event references unknown room {room!r}")
            room_idx = idx[room]
        elif isinstance(room, int) and 0 <= room < n:
            room_idx = room
        else:
            raise ValidationError(f"invalid room reference {room!r} in source event")
        s = parse_nonnegative(strength, field=f"source[{room}]@{t}")
        normalized.append((float(t), room_idx, s))
        event_records.append(
            {"time": float(t), "room": names[room_idx], "strength": rational_str(s)}
        )

    grouped = _group_events(normalized)
    if len(grouped) > 50:
        raise LimitError("at most 50 source-change instants are supported")

    # The process starts at c0 at t=0 and runs through the end of the window.
    # Source changes strictly after the window cannot affect anything inside it.
    points = {0.0, float(window_start), float(window_end)}
    points.update(t for t in grouped if t <= float(window_end) + 1e-15)
    for bp in extra_breakpoints:
        if bp < 0 or bp > MAX_DURATION + 1e-12:
            raise LimitError(f"breakpoint {bp} outside [0, {MAX_DURATION}]")
        if bp <= float(window_end) + 1e-15:
            points.add(float(bp))
    breakpoints = sorted(points)

    # source state evolves by applying each group at its time
    active = [Fraction(0)] * n
    segments: list[Segment] = []
    group_times = sorted(grouped)
    gi = 0
    for k in range(len(breakpoints) - 1):
        t0, t1 = breakpoints[k], breakpoints[k + 1]
        while gi < len(group_times) and group_times[gi] <= t0 + 1e-15:
            for room_idx, s in grouped[group_times[gi]].items():
                active[room_idx] = s
            gi += 1
        if t1 - t0 <= 0:
            continue  # never emit zero-length segments
        src_f = np.array([fraction_to_float(x) for x in active], dtype=float)
        segments.append(
            Segment(t0=t0, t1=t1, source=src_f, source_exact=tuple(active))
        )

    # Apply any groups scheduled exactly at the final breakpoint (no duration).
    return Timeline(
        breakpoints=breakpoints,
        segments=segments,
        room_names=list(names),
        window_start=float(window_start),
        window_end=float(window_end),
        source_events=event_records,
    )


def build_augmented(matrix: Mapping, source: np.ndarray) -> np.ndarray:
    """Return the affine generator ``B`` for ``x = [c, M, 1]``."""
    n = matrix["n"]
    K = matrix["K"]
    V = matrix["V"]
    exhaust = matrix["exhaust"]
    m = 2 * n + 1
    B = np.zeros((m, m), dtype=float)
    B[:n, :n] = K
    B[:n, m - 1] = source / V
    B[n : 2 * n, :n] = np.diag(exhaust)
    return B


def _integral_operator(B: np.ndarray, dt: float) -> np.ndarray:
    """exp of the doubly-augmented generator, returning full propagator."""
    m = B.shape[0]
    Q = np.zeros((2 * m, 2 * m), dtype=float)
    Q[:m, :m] = B
    Q[m:, :m] = np.eye(m)
    return expm(Q * dt)


@dataclass
class PropagationResult:
    times: np.ndarray
    concentrations: np.ndarray  # (npoints, n)
    exhausted_mass: np.ndarray  # (npoints, n) cumulative
    segment_integrals: list[np.ndarray]  # concentration-time integral per segment
    segment_bounds: list[tuple[float, float]]
    max_abs_error_estimate: float


def propagate(
    matrix: Mapping,
    timeline: Timeline,
    c0: np.ndarray,
    *,
    atol: float = 1e-7,
    max_depth: int = 24,
) -> PropagationResult:
    """Integrate the full concentration process over the timeline.

    Returns states at every breakpoint, plus the exact concentration-time
    integral of each segment.  Each constant-source segment is advanced with
    the augmented matrix exponential; if the difference between one full step
    and two half steps exceeds ``atol`` the step is subdivided (controlled
    error).  The matrix exponential itself needs no matrix inverse, so singular
    ``K`` (closed/zero-flow components) is fine.
    """
    n = matrix["n"]
    m = 2 * n + 1
    x = np.zeros(m, dtype=float)
    x[:n] = c0
    x[m - 1] = 1.0

    times = [timeline.breakpoints[0]]
    states = [x.copy()]
    seg_integrals: list[np.ndarray] = []
    seg_bounds: list[tuple[float, float]] = []
    err_est = 0.0

    for seg in timeline.segments:
        dt = seg.duration
        if dt <= 0:
            continue
        B = build_augmented(matrix, seg.source)
        integral_total = np.zeros(n, dtype=float)

        def step(x_local, dt_local, depth):
            """Advance one sub-step, returning (x_next, integral, errest)."""
            F = _integral_operator(B, dt_local)
            x_full = F[:m, :m] @ x_local
            half = dt_local / 2.0
            Fh = _integral_operator(B, half)
            xh = Fh[:m, :m] @ x_local
            int_h1 = Fh[m:, :m] @ x_local
            xh2 = Fh[:m, :m] @ xh
            int_h2 = Fh[m:, :m] @ xh
            err = float(np.max(np.abs(xh2[:n] - x_full[:n])))
            if err <= atol or depth >= max_depth:
                return xh2, int_h1 + int_h2, err
            # subdivide in time: recurse over the two half intervals
            xa, ia, ea = step(x_local, half, depth + 1)
            xb, ib, eb = step(xa, half, depth + 1)
            return xb, ia + ib, max(ea, eb)

        x, integral_total, err = step(x, dt, 0)
        err_est = max(err_est, err)
        x[m - 1] = 1.0  # constant slot stays exactly 1
        times.append(seg.t1)
        states.append(x.copy())
        seg_integrals.append(integral_total[:n].copy())
        seg_bounds.append((seg.t0, seg.t1))

    arr = np.array(states)
    return PropagationResult(
        times=np.array(times),
        concentrations=arr[:, :n],
        exhausted_mass=arr[:, n : 2 * n],
        segment_integrals=seg_integrals,
        segment_bounds=seg_bounds,
        max_abs_error_estimate=err_est,
    )
