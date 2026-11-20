"""Exact borehole-to-horizon correlation solver.

For every cored segment of every borehole the solver chooses either one
horizon whose accepted lithology set contains the segment lithology, or
``UNKNOWN``.  After UNKNOWN segments are removed, the assigned horizons of a
borehole must satisfy:

* adjacent *distinct* horizons are connected by a ``shallower_than`` path
  (shallow to deep);
* the same horizon may occupy several consecutive segments but, once left,
  can never be assigned again (no re-entry);
* human fixations are hard constraints and can never become UNKNOWN.

Optimisation is strictly lexicographic across all boreholes:

1. minimum total UNKNOWN weight;
2. then minimum number of horizon switches per borehole (summed);
3. then the lexicographically smallest vector of horizon IDs ordered by
   ``(borehole id, segment start depth)``, with UNKNOWN ranked after every
   real horizon ID.

Because boreholes share no constraints, each borehole is solved independently
with an exact dynamic program over ``(used-horizon bit mask, last horizon)``
states.  An independent brute-force enumerator (:func:`brute_force_hole`) is
kept for cross-checking small inputs in the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product as cartesian

from .models import BoreholeInput, Product
from .poset import PartialOrder

UNKNOWN = "UNKNOWN"
"""Sentinel assignment.  It is never a real horizon ID."""


class CorrelationContradiction(RuntimeError):
    """Raised when no assignment can satisfy the human fixations."""

    def __init__(self, contradictions: list["Contradiction"]):
        self.contradictions = contradictions
        super().__init__(
            "no feasible correlation; "
            + "; ".join(c.message for c in contradictions)
        )


@dataclass(frozen=True)
class Assignment:
    segment_id: str
    borehole_id: str
    horizon_id: str | None  # None means UNKNOWN

    @property
    def is_unknown(self) -> bool:
        return self.horizon_id is None


@dataclass
class Contradiction:
    borehole_id: str
    kind: str
    message: str
    related_fixes: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)


@dataclass
class HoleSolution:
    borehole_id: str
    assignments: tuple[Assignment, ...]
    unknown_weight: int
    switches: int

    @property
    def vector(self) -> tuple[str, ...]:
        return tuple(
            UNKNOWN if a.horizon_id is None else a.horizon_id for a in self.assignments
        )


@dataclass
class CorrelationResult:
    product_id: str
    hole_solutions: dict[str, HoleSolution]
    total_unknown_weight: int
    total_switches: int

    @property
    def lex_vector(self) -> tuple[str, ...]:
        ordered = sorted(self.hole_solutions.values(), key=lambda sol: sol.borehole_id)
        digits: tuple[str, ...] = ()
        for sol in ordered:
            digits += sol.vector
        return digits

    def ordered_holes(self) -> list[HoleSolution]:
        return [self.hole_solutions[bid] for bid in sorted(self.hole_solutions)]


# --------------------------------------------------------------------------- #
# Validation primitives shared by the DP and the brute-force enumerator
# --------------------------------------------------------------------------- #


def check_assigned_sequence(
    horizon_ids: list[str], reachable: list[int], assigned: list[int | None]
) -> list[str]:
    """Validate one assignment vector.

    ``assigned[i]`` is a horizon index or ``None`` for UNKNOWN.  Returns
    violation messages (empty list == valid).
    """
    violations: list[str] = []
    used = 0
    last: int | None = None
    for idx, choice in enumerate(assigned):
        if choice is None:
            continue
        bit = 1 << choice
        if last is None:
            used |= bit
            last = choice
            continue
        if choice == last:
            continue
        if used & bit:
            violations.append(
                f"re-entry into horizon {horizon_ids[choice]!r} at position {idx}"
            )
            continue
        if not (reachable[last] & bit):
            violations.append(
                f"no shallower_than path {horizon_ids[last]!r} -> "
                f"{horizon_ids[choice]!r} at position {idx}"
            )
            continue
        used |= bit
        last = choice
    return violations


def _switch_count(assigned: list[int | None]) -> int:
    switches = 0
    last: int | None = None
    for choice in assigned:
        if choice is None:
            continue
        if last is not None and choice != last:
            switches += 1
        last = choice
    return switches


# --------------------------------------------------------------------------- #
# Contradiction detection on hard fixations
# --------------------------------------------------------------------------- #


def detect_contradictions(
    borehole: BoreholeInput, product: Product, poset: PartialOrder
) -> list[Contradiction]:
    """Explain every fixation that makes a borehole infeasible.

    The check mirrors the DP constraints exactly but runs first, on the
    subsequence of fixed segments, so the error report can name the offending
    fixations and catalogue relations instead of silently dropping them.
    """
    horizon_ids = [h.horizon_id for h in product.horizons]
    horizon_by_id = {h.horizon_id: h for h in product.horizons}
    contradictions: list[Contradiction] = []

    fixed: list[tuple[int, int]] = []  # (segment position, horizon index)
    for pos, segment in enumerate(borehole.segments):
        if segment.fixed_horizon is None:
            continue
        fix_ref = {
            "segment_id": segment.segment_id,
            "start": segment.start,
            "end": segment.end,
            "lithology": segment.lithology,
            "fixed_horizon": segment.fixed_horizon,
        }
        horizon = horizon_by_id.get(segment.fixed_horizon)
        if horizon is None:
            contradictions.append(
                Contradiction(
                    borehole_id=borehole.borehole_id,
                    kind="fixed_unknown_horizon",
                    message=(
                        f"segment {segment.segment_id!r} is fixed to unknown "
                        f"horizon {segment.fixed_horizon!r}"
                    ),
                    related_fixes=[fix_ref],
                )
            )
            continue
        if segment.lithology not in horizon.acceptable_lithology:
            contradictions.append(
                Contradiction(
                    borehole_id=borehole.borehole_id,
                    kind="fixed_lithology",
                    message=(
                        f"segment {segment.segment_id!r} lithology "
                        f"{segment.lithology!r} is not accepted by its fixed "
                        f"horizon {segment.fixed_horizon!r}; accepted codes: "
                        f"{sorted(horizon.acceptable_lithology)}"
                    ),
                    related_fixes=[fix_ref],
                )
            )
            continue
        fixed.append((pos, horizon_ids.index(segment.fixed_horizon)))

    # Order / re-entry contradictions between feasible fixations.
    used: set[int] = set()
    last_fix: tuple[int, int] | None = None
    for pos, h_idx in fixed:
        fix_ref = {
            "segment_id": borehole.segments[pos].segment_id,
            "start": borehole.segments[pos].start,
            "end": borehole.segments[pos].end,
            "lithology": borehole.segments[pos].lithology,
            "fixed_horizon": horizon_ids[h_idx],
        }
        if last_fix is None:
            used.add(h_idx)
            last_fix = (pos, h_idx)
            continue
        prev_pos, prev_idx = last_fix
        prev_ref = {
            "segment_id": borehole.segments[prev_pos].segment_id,
            "start": borehole.segments[prev_pos].start,
            "end": borehole.segments[prev_pos].end,
            "lithology": borehole.segments[prev_pos].lithology,
            "fixed_horizon": horizon_ids[prev_idx],
        }
        if h_idx == prev_idx:
            last_fix = (pos, h_idx)
            continue
        evidence = poset.evidence(horizon_ids[prev_idx], horizon_ids[h_idx])
        if h_idx in used:
            contradictions.append(
                Contradiction(
                    borehole_id=borehole.borehole_id,
                    kind="fixed_reentry",
                    message=(
                        f"fixed horizon {horizon_ids[h_idx]!r} re-occurs on "
                        f"segment {borehole.segments[pos].segment_id!r} after "
                        f"being left; a horizon cannot return once left"
                    ),
                    related_fixes=[prev_ref, fix_ref],
                )
            )
        elif evidence.is_incomparable or evidence.forward_path is None:
            contradictions.append(
                Contradiction(
                    borehole_id=borehole.borehole_id,
                    kind="fixed_order",
                    message=(
                        f"fixed order {horizon_ids[prev_idx]!r} -> "
                        f"{horizon_ids[h_idx]!r} is unsupported: {evidence.explain()}"
                    ),
                    related_fixes=[prev_ref, fix_ref],
                    relations=[
                        {
                            "left": horizon_ids[prev_idx],
                            "right": horizon_ids[h_idx],
                            "forward_path": list(evidence.forward_path)
                            if evidence.forward_path else None,
                            "backward_path": list(evidence.backward_path)
                            if evidence.backward_path else None,
                        }
                    ],
                )
            )
        used.add(h_idx)
        last_fix = (pos, h_idx)
    return contradictions


# --------------------------------------------------------------------------- #
# Exact dynamic program
# --------------------------------------------------------------------------- #


def solve_hole(
    borehole: BoreholeInput, product: Product, poset: PartialOrder
) -> HoleSolution:
    """Solve one borehole exactly with the lexicographic objective."""
    horizon_ids = [h.horizon_id for h in product.horizons]
    horizon_by_id = {h.horizon_id: h for h in product.horizons}
    reachable = poset._reachable  # bit mask of deeper reachable horizons
    h_count = len(horizon_ids)
    base = h_count + 1  # digits 0..H-1 = horizons, H = UNKNOWN
    unknown_digit = h_count

    segments = borehole.segments
    accept: list[frozenset[int]] = []
    fixed: list[int | None] = []
    for segment in segments:
        choices = frozenset(
            i
            for i, horizon in enumerate(product.horizons)
            if segment.lithology in horizon.acceptable_lithology
        )
        accept.append(choices)
        if segment.fixed_horizon is None:
            fixed.append(None)
        else:
            fixed.append(horizon_ids.index(segment.fixed_horizon))

    # One DP layer per segment.  A state is keyed by ``(used mask, last
    # horizon index or -1)``; its value keeps the best cost triple *and* the
    # back-pointer to the previous layer's state.  Keeping back-pointers
    # inside the layer (instead of a global dict keyed by step/state) ensures
    # that when a better path reaches the same state it replaces the pointer
    # atomically and can never corrupt another predecessor's decode chain.
    init_state = (0, -1)
    # layer: dict[state_key] -> (cost_tuple, prev_key, chosen_digit)
    layers: list[dict[tuple[int, int], tuple[tuple[int, int, int], tuple[int, int], int]]] = [
        {init_state: ((0, 0, 0), init_state, -1)}
    ]

    for step, segment in enumerate(segments):
        weight = segment.weight
        if fixed[step] is None:
            choices: list[int] = [unknown_digit] + sorted(accept[step])
        else:
            choices = [fixed[step]]  # fixations can never become UNKNOWN

        current = layers[-1]
        next_layer: dict[
            tuple[int, int], tuple[tuple[int, int, int], tuple[int, int], int]
        ] = {}
        for (mask, last), ((u_cost, s_cost, lex), _, _) in current.items():
            for digit in choices:
                if digit == unknown_digit:
                    new_mask, new_last = mask, last
                    new_u, new_s = u_cost + weight, s_cost
                else:
                    bit = 1 << digit
                    if last == -1:
                        new_mask, new_last, new_s = mask | bit, digit, s_cost
                        new_u = u_cost
                    elif digit == last:
                        new_mask, new_last = mask, last
                        new_u, new_s = u_cost, s_cost
                    elif (mask & bit) or not (reachable[last] & bit):
                        continue  # re-entry or missing shallower_than path
                    else:
                        new_mask = mask | bit
                        new_last = digit
                        new_u, new_s = u_cost, s_cost + 1
                candidate = (new_u, new_s, lex * base + digit)
                key = (new_mask, new_last)
                existing = next_layer.get(key)
                if existing is None or candidate < existing[0]:
                    next_layer[key] = (candidate, (mask, last), digit)
        if not next_layer:
            # Defensive: contradictions should already have been reported.
            raise CorrelationContradiction(
                [
                    Contradiction(
                        borehole_id=borehole.borehole_id,
                        kind="unsat",
                        message="dynamic program found no feasible state",
                    )
                ]
            )
        layers.append(next_layer)

    final_layer = layers[-1]
    best_key = min(final_layer, key=lambda key: final_layer[key][0])
    best_cost = final_layer[best_key][0]

    # Decode the digit vector by walking back through every layer.
    digits: list[int] = []
    key = best_key
    for step in range(len(segments), 0, -1):
        _, prev_key, digit = layers[step][key]
        digits.append(digit)
        key = prev_key
    digits.reverse()

    assignments = tuple(
        Assignment(
            segment_id=segment.segment_id,
            borehole_id=borehole.borehole_id,
            horizon_id=None if digit == unknown_digit else horizon_ids[digit],
        )
        for segment, digit in zip(segments, digits)
    )
    assigned_idx = [None if d == unknown_digit else d for d in digits]
    return HoleSolution(
        borehole_id=borehole.borehole_id,
        assignments=assignments,
        unknown_weight=best_cost[0],
        switches=_switch_count(assigned_idx),
    )


def solve_all(
    boreholes: list[BoreholeInput], product: Product
) -> CorrelationResult:
    """Solve every borehole against one published product.

    Raises :class:`CorrelationContradiction` carrying the full evidence for
    every infeasible borehole; no partial result is produced.
    """
    poset = PartialOrder(
        [h.horizon_id for h in product.horizons],
        [(e.parent_id, e.child_id) for e in product.edges],
    )
    all_contradictions: list[Contradiction] = []
    for borehole in boreholes:
        all_contradictions.extend(
            detect_contradictions(borehole, product, poset)
        )
    if all_contradictions:
        raise CorrelationContradiction(all_contradictions)

    solutions: dict[str, HoleSolution] = {}
    for borehole in boreholes:
        solutions[borehole.borehole_id] = solve_hole(borehole, product, poset)
    return CorrelationResult(
        product_id=product.product_id,
        hole_solutions=solutions,
        total_unknown_weight=sum(sol.unknown_weight for sol in solutions.values()),
        total_switches=sum(sol.switches for sol in solutions.values()),
    )


# --------------------------------------------------------------------------- #
# Independent brute-force enumerator (small-hole cross-check only)
# --------------------------------------------------------------------------- #


def brute_force_hole(
    borehole: BoreholeInput, product: Product, poset: PartialOrder
) -> HoleSolution:
    """Enumerate every admissible assignment and pick the objective minimum.

    Intentionally separate from the DP code so the test suite can compare two
    independent implementations.  Feasible only for small segment counts.
    """
    horizon_ids = [h.horizon_id for h in product.horizons]
    reachable = poset._reachable

    option_lists: list[list[int | None]] = []
    for segment in borehole.segments:
        acceptable = [
            i
            for i, horizon in enumerate(product.horizons)
            if segment.lithology in horizon.acceptable_lithology
        ]
        if segment.fixed_horizon is None:
            option_lists.append(acceptable + [None])
        else:
            option_lists.append([horizon_ids.index(segment.fixed_horizon)])

    best: HoleSolution | None = None
    best_key: tuple[int, int, tuple[str, ...]] | None = None
    for combo in cartesian(*option_lists):
        assigned = list(combo)
        if check_assigned_sequence(horizon_ids, reachable, assigned):
            continue
        unknown_weight = sum(
            segment.weight
            for segment, choice in zip(borehole.segments, assigned)
            if choice is None
        )
        switches = _switch_count(assigned)
        vector = tuple(
            UNKNOWN if choice is None else horizon_ids[choice] for choice in assigned
        )
        key = (unknown_weight, switches, vector)
        if best_key is None or key < best_key:
            best_key = key
            best = HoleSolution(
                borehole_id=borehole.borehole_id,
                assignments=tuple(
                    Assignment(
                        segment_id=segment.segment_id,
                        borehole_id=borehole.borehole_id,
                        horizon_id=choice_name,
                    )
                    for segment, choice_name in zip(
                        borehole.segments,
                        [None if c is None else horizon_ids[c] for c in assigned],
                    )
                ),
                unknown_weight=unknown_weight,
                switches=switches,
            )
    if best is None:
        raise CorrelationContradiction(
            detect_contradictions(borehole, product, poset)
        )
    return best
