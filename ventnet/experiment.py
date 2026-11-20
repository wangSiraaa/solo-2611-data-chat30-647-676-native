"""Tracer-gas observation chamber: experiments, visits, exposure integrals."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Mapping, Sequence

import numpy as np

from .catalog import Catalog
from .errors import ConflictError, LimitError, ValidationError
from .rational import fraction_to_float, parse_nonnegative, rational_str
from .solver import build_timeline, propagate

MAX_SOURCE_CHANGES = 50
MAX_VISITS = 100
MAX_PERSONS = 200
MAX_DURATION = 10000.0


@dataclass(frozen=True)
class SourceEvent:
    time: float
    room: str
    strength: Fraction

    def __post_init__(self):
        if self.time < 0:
            raise ValidationError("source event time must be >= 0")


@dataclass(frozen=True)
class Visit:
    visit_id: str
    person: str
    room: str
    start: float
    end: float

    def __post_init__(self):
        if not self.visit_id:
            raise ValidationError("visit_id must be non-empty")
        if not self.person:
            raise ValidationError("visit person must be non-empty")
        if self.end < self.start:
            raise ValidationError(f"visit {self.visit_id}: end before start")
        if self.start < 0:
            raise ValidationError(f"visit {self.visit_id}: negative start")


@dataclass
class Experiment:
    catalog: Catalog
    initial_concentration: Mapping[str, Fraction]
    source_events: list[SourceEvent]
    window_start: float
    window_end: float
    visits: list[Visit]
    _identity: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        self.catalog.require_published()
        names = self.catalog.room_names()
        # normalize/validate initial concentrations
        c0 = {}
        for name in names:
            if name not in self.initial_concentration:
                raise ValidationError(f"missing initial concentration for room {name!r}")
            c0[name] = parse_nonnegative(
                self.initial_concentration[name], field=f"c0[{name}]"
            )
        unknown = set(self.initial_concentration) - set(names)
        if unknown:
            raise ValidationError(f"initial concentration for unknown rooms {sorted(unknown)}")
        self.initial_concentration = c0
        if not (0 <= self.window_start <= self.window_end <= MAX_DURATION + 1e-12):
            raise LimitError(
                f"analysis window must satisfy 0 <= start <= end <= {MAX_DURATION}"
            )
        # validate source events
        if len(self.source_events) > MAX_SOURCE_CHANGES:
            raise LimitError(f"at most {MAX_SOURCE_CHANGES} source changes are supported")
        for ev in self.source_events:
            if ev.room not in names:
                raise ValidationError(f"source event references unknown room {ev.room!r}")
            if ev.time < 0:
                raise ValidationError("source event time must be >= 0")
        if len(self.visits) > MAX_VISITS:
            raise LimitError(f"at most {MAX_VISITS} person visits are supported")
        for v in self.visits:
            if v.room not in names:
                raise ValidationError(f"visit {v.visit_id!r} references unknown room {v.room!r}")
            if v.start < 0 or v.end > MAX_DURATION + 1e-12:
                raise LimitError(
                    f"visit {v.visit_id!r} lies outside [0, {MAX_DURATION}]"
                )
            if v.start < self.window_start - 1e-12 or v.end > self.window_end + 1e-12:
                raise ValidationError(
                    f"visit {v.visit_id!r} ({v.start}, {v.end}) lies outside the "
                    f"analysis window [{self.window_start}, {self.window_end}]"
                )
        _check_visit_conflicts(self.visits)

    def source_tuples(self):
        return [(ev.time, ev.room, ev.strength) for ev in self.source_events]

    def canonical_payload(self) -> dict:
        return {
            "catalog": self.catalog.identity,
            "window": [self.window_start, self.window_end],
            "c0": {
                name: rational_str(self.initial_concentration[name])
                for name in sorted(self.initial_concentration)
            },
            "sources": [
                [ev.time, ev.room, rational_str(ev.strength)]
                for ev in self.source_events
            ],
            "visits": [
                [v.visit_id, v.person, v.room, v.start, v.end] for v in self.visits
            ],
        }

    def compute_identity(self) -> str:
        import hashlib
        import json

        blob = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        ).encode()
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    @property
    def identity(self) -> str:
        if self._identity is None:
            self._identity = self.compute_identity()
        return self._identity


def _check_visit_conflicts(visits: Sequence[Visit]) -> None:
    """Reject any person simultaneously present in two places (or double-counted).

    Intervals are half-open ``[start, end)``; touching endpoints do not overlap.
    """
    by_person: dict[str, list[Visit]] = {}
    for v in visits:
        by_person.setdefault(v.person, []).append(v)
    if len(by_person) > MAX_PERSONS:
        raise LimitError(f"at most {MAX_PERSONS} persons are supported")
    for person, recs in by_person.items():
        ordered = sorted(recs, key=lambda r: (r.start, r.end, r.visit_id))
        for prev, cur in zip(ordered, ordered[1:]):
            if cur.start < prev.end - 1e-15:
                if cur.room != prev.room:
                    raise ConflictError(
                        f"person {person!r} is in rooms {prev.room!r} and "
                        f"{cur.room!r} simultaneously around t={cur.start}"
                    )
                raise ConflictError(
                    f"person {person!r} has overlapping visits in room "
                    f"{cur.room!r} around t={cur.start}"
                )


@dataclass
class ExperimentResult:
    identity: str
    catalog_identity: str
    room_names: list[str]
    times: np.ndarray
    concentrations: np.ndarray
    exhausted_mass: np.ndarray
    visit_integrals: list[dict]
    person_totals: dict[str, dict]
    mass_balance: dict
    explanations: list[dict]
    source_events: list[dict]
    visits: list[dict]
    window_start: float
    window_end: float
    max_abs_error_estimate: float

    def concentration_at(self, time: float) -> dict[str, float]:
        if time < self.times[0] - 1e-12 or time > self.times[-1] + 1e-12:
            raise ValidationError(f"time {time} outside integrated window")
        return {
            name: float(np.interp(time, self.times, self.concentrations[:, i]))
            for i, name in enumerate(self.room_names)
        }

    def to_dict(self) -> dict:
        return {
            "identity": self.identity,
            "catalog_identity": self.catalog_identity,
            "window": {"start": self.window_start, "end": self.window_end},
            "room_names": self.room_names,
            "times": self.times.tolist(),
            "concentrations": {
                name: self.concentrations[:, i].tolist()
                for i, name in enumerate(self.room_names)
            },
            "exhausted_mass": {
                name: self.exhausted_mass[:, i].tolist()
                for i, name in enumerate(self.room_names)
            },
            "visit_integrals": self.visit_integrals,
            "person_totals": self.person_totals,
            "mass_balance": self.mass_balance,
            "explanations": self.explanations,
            "source_events": self.source_events,
            "visits": self.visits,
            "max_abs_error_estimate": self.max_abs_error_estimate,
        }


def run_experiment(exp: Experiment, *, atol: float = 1e-7) -> ExperimentResult:
    """Integrate, integrate exposures, and verify window mass conservation."""
    cat = exp.catalog
    matrix = cat.matrices()
    names = matrix["names"]

    c0 = np.array([fraction_to_float(exp.initial_concentration[name]) for name in names])

    visit_points = []
    for v in exp.visits:
        visit_points.append(v.start)
        visit_points.append(v.end)
    timeline = build_timeline(
        matrix,
        exp.source_tuples(),
        exp.window_start,
        exp.window_end,
        extra_breakpoints=visit_points,
    )

    prop = propagate(matrix, timeline, c0, atol=atol)

    # concentration-time integral per segment, mapped to visits by breakpoints
    visit_integrals = []
    person_acc: dict[str, dict] = {}
    for v in exp.visits:
        integral = _integral_over_interval(prop, matrix, v.room, v.start, v.end)
        entry = {
            "visit_id": v.visit_id,
            "person": v.person,
            "room": v.room,
            "start": v.start,
            "end": v.end,
            "duration": v.end - v.start,
            "concentration_integral_mg_s_per_m3": integral,
            "basis_catalog_identity": cat.identity,
        }
        visit_integrals.append(entry)
        acc = person_acc.setdefault(
            v.person, {"person": v.person, "integral_mg_s_per_m3": 0.0, "visits": []}
        )
        acc["integral_mg_s_per_m3"] += integral
        acc["visits"].append(v.visit_id)

    # ---- mass conservation over the analysis window ----
    mass_balance = _mass_balance(exp, matrix, timeline, prop)

    # ---- explanations: which segments/room states each integral used ----
    explanations = []
    for v in exp.visits:
        segs = timeline.explain(v.start, v.end)
        # room state (concentrations) at each covering segment's start
        states = []
        for sg in segs:
            state = {}
            for i, name in enumerate(names):
                state[name] = float(np.interp(sg["t0"], prop.times, prop.concentrations[:, i]))
            states.append({"t": sg["t0"], "concentration_at_segment_start": state})
        explanations.append(
            {
                "visit_id": v.visit_id,
                "room": v.room,
                "requested_window": [v.start, v.end],
                "segments_used": segs,
                "room_states_at_segment_start": states,
                "method": "exact augmented matrix-exponential integral "
                "(integral of exp(B s) ds), not endpoint averaging",
            }
        )
    explanations.append(
        {
            "kind": "timeline",
            "window": [exp.window_start, exp.window_end],
            "segments_used": timeline.explain(exp.window_start, exp.window_end),
            "method": "adaptive exp(B dt) per constant-source segment",
        }
    )

    identity = exp.identity
    return ExperimentResult(
        identity=identity,
        catalog_identity=cat.identity,
        room_names=list(names),
        times=prop.times,
        concentrations=prop.concentrations,
        exhausted_mass=prop.exhausted_mass,
        visit_integrals=visit_integrals,
        person_totals=person_acc,
        mass_balance=mass_balance,
        explanations=explanations,
        source_events=list(timeline.source_events),
        visits=[
            {
                "visit_id": v.visit_id,
                "person": v.person,
                "room": v.room,
                "start": v.start,
                "end": v.end,
            }
            for v in exp.visits
        ],
        window_start=exp.window_start,
        window_end=exp.window_end,
        max_abs_error_estimate=prop.max_abs_error_estimate,
    )


def _integral_over_interval(prop, matrix, room: str, a: float, b: float) -> float:
    """Sum exact segment integrals whose bounds tile [a, b).

    ``a`` and ``b`` were inserted as breakpoints, so every fully-covered
    segment is exact; boundary segments (with zero overlap) are skipped.
    """
    ridx = matrix["index"][room]
    total = 0.0
    for k, (t0, t1) in enumerate(prop.segment_bounds):
        lo = max(a, t0)
        hi = min(b, t1)
        if hi <= lo:
            continue
        frac = (hi - lo) / (t1 - t0)
        if t0 >= a - 1e-12 and t1 <= b + 1e-12:
            total += float(prop.segment_integrals[k][ridx])
        else:
            # Boundary splits are breakpoints, so this should be negligible;
            # scale the full-segment integral proportionally as a safeguard.
            total += float(prop.segment_integrals[k][ridx]) * frac
    return total


def _mass_balance(exp, matrix, timeline, prop) -> dict:
    """Check dStorage = source - exhaust over the window.

    Internal room-to-room flows cancel by construction and are never counted
    as losses.  Storage is measured at the window endpoints (which are always
    breakpoints), not at process start t=0.
    """
    V = matrix["V"]
    i0 = int(np.argmin(np.abs(prop.times - exp.window_start)))
    i1 = int(np.argmin(np.abs(prop.times - exp.window_end)))
    c_init = prop.concentrations[i0]
    c_final = prop.concentrations[i1]
    storage_init = float(np.sum(V * c_init))
    storage_final = float(np.sum(V * c_final))
    d_storage = storage_final - storage_init

    source_mass = 0.0
    for seg in timeline.segments:
        lo = max(exp.window_start, seg.t0)
        hi = min(exp.window_end, seg.t1)
        if hi > lo:
            source_mass += float(np.sum(seg.source)) * (hi - lo)

    exhaust_mass = float(
        np.sum(prop.exhausted_mass[i1] - prop.exhausted_mass[i0])
    )
    residual = d_storage - (source_mass - exhaust_mass)
    scale = max(abs(source_mass), abs(exhaust_mass), abs(d_storage), 1.0)
    return {
        "window": [exp.window_start, exp.window_end],
        "storage_initial": storage_init,
        "storage_final": storage_final,
        "delta_storage": d_storage,
        "source_input": source_mass,
        "outdoor_exhaust": exhaust_mass,
        "internal_flow_note": "room-to-room flows cancel and are not losses",
        "residual": residual,
        "relative_residual": abs(residual) / scale,
        "tolerance": 1e-7,
        "ok": abs(residual) <= 1e-7 * scale,
    }
