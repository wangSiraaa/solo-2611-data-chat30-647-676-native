"""Borehole geometry, horizon statistics and sample membership.

Depth rules (all exact rational arithmetic):

* cored segments and explicit uncored gaps are non-overlapping, ordered
  shallow to deep, with positive thickness and non-negative endpoints;
* a gap is attributed to a horizon **only** when the cored segments
  immediately above and below it are assigned to the same known horizon;
  otherwise the gap stays ``UNDETERMINED`` and is never averaged across
  horizons;
* samples use half-open interval membership ``[start, end)``: a sample whose
  depth equals the deepest segment end is a miss, not a hit;
* samples landing in UNKNOWN segments never contribute to known-horizon
  statistics.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import rational as R
from .models import BoreholeInput, SampleInput, SegmentInput
from .solver import HoleSolution

UNDETERMINED = "UNDETERMINED"

SAMPLE_HIT = "HIT"                 # inside a segment assigned to a horizon
SAMPLE_UNKNOWN = "UNKNOWN_HIT"     # inside an UNKNOWN segment
SAMPLE_GAP = "GAP"                 # inside an explicit uncored gap
SAMPLE_MISS = "MISS"               # outside every segment and gap
SAMPLE_AMBIGUOUS = "AMBIGUOUS"     # depth matches more than one borehole


class GeometryError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OrderedEntry:
    kind: str            # "segment" | "gap"
    start: str
    end: str
    ref: object


def validate_borehole(borehole: BoreholeInput) -> None:
    """Validate ordering, non-overlap, positivity and identifier uniqueness."""
    seen_ids: set[str] = set()
    entries: list[tuple[str, str, str, str]] = []
    for segment in borehole.segments:
        if segment.segment_id in seen_ids:
            raise GeometryError(
                f"borehole {borehole.borehole_id!r}: duplicate segment id "
                f"{segment.segment_id!r}"
            )
        seen_ids.add(segment.segment_id)
        _check_interval(
            borehole.borehole_id, segment.segment_id, segment.start, segment.end
        )
        if segment.weight <= 0:
            raise GeometryError(
                f"segment {segment.segment_id!r}: weight must be a positive "
                f"integer, got {segment.weight}"
            )
        entries.append((segment.start, segment.end, "segment", segment.segment_id))
    gap_ids: set[str] = set()
    for gap in borehole.gaps:
        if gap.gap_id in gap_ids:
            raise GeometryError(
                f"borehole {borehole.borehole_id!r}: duplicate gap id "
                f"{gap.gap_id!r}"
            )
        gap_ids.add(gap.gap_id)
        _check_interval(borehole.borehole_id, gap.gap_id, gap.start, gap.end)
        entries.append((gap.start, gap.end, "gap", gap.gap_id))

    entries.sort(key=lambda e: R.to_fraction(e[0]))
    previous_end: R.Fraction | None = None
    previous_ref: str | None = None
    for start, end, kind, ref in entries:
        start_f, end_f = R.to_fraction(start), R.to_fraction(end)
        if previous_end is not None and start_f < previous_end:
            raise GeometryError(
                f"borehole {borehole.borehole_id!r}: entry {ref!r} overlaps "
                f"previous entry {previous_ref!r}"
            )
        previous_end, previous_ref = end_f, ref

    # Segments themselves must arrive shallow to deep in the input.
    seg_starts = [R.to_fraction(s.start) for s in borehole.segments]
    if seg_starts != sorted(seg_starts):
        raise GeometryError(
            f"borehole {borehole.borehole_id!r}: segments must be ordered "
            f"shallow to deep"
        )


def _check_interval(borehole_id: str, ref: str, start: str, end: str) -> None:
    start_f, end_f = R.to_fraction(start), R.to_fraction(end)
    if not R.is_positive(R.sub(end, start)):
        raise GeometryError(
            f"borehole {borehole_id!r}: interval {ref!r} must have positive "
            f"thickness (start={start}, end={end})"
        )


def ordered_entries(borehole: BoreholeInput) -> list[OrderedEntry]:
    entries = [
        OrderedEntry("segment", s.start, s.end, s) for s in borehole.segments
    ] + [OrderedEntry("gap", g.start, g.end, g) for g in borehole.gaps]
    entries.sort(key=lambda e: (R.to_fraction(e.start), 0 if e.kind == "gap" else 1))
    return entries


# --------------------------------------------------------------------------- #
# Per-horizon statistics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HorizonStat:
    borehole_id: str
    horizon_id: str
    top_depth: str
    bottom_depth: str
    cored_thickness: str
    gap_thickness: str
    segment_ids: tuple[str, ...]
    gap_ids: tuple[str, ...]


@dataclass(frozen=True)
class GapAttribution:
    borehole_id: str
    gap_id: str
    start: str
    end: str
    thickness: str
    horizon_id: str | None       # None == UNDETERMINED
    reason: str


def build_statistics(
    borehole: BoreholeInput, solution: HoleSolution
) -> tuple[list[HorizonStat], list[GapAttribution]]:
    assignment_by_segment = {a.segment_id: a.horizon_id for a in solution.assignments}

    # --- Gap attribution -------------------------------------------------- #
    entries = ordered_entries(borehole)
    gap_records: list[GapAttribution] = []
    for pos, entry in enumerate(entries):
        if entry.kind != "gap":
            continue
        above = _nearest_segment(entries, pos, direction=-1)
        below = _nearest_segment(entries, pos, direction=+1)
        above_h = assignment_by_segment.get(above.segment_id) if above else None
        below_h = assignment_by_segment.get(below.segment_id) if below else None
        thickness = R.sub(entry.end, entry.start)
        if (
            above_h is not None
            and below_h is not None
            and above_h == below_h
        ):
            gap_records.append(
                GapAttribution(
                    borehole_id=borehole.borehole_id,
                    gap_id=entry.ref.gap_id,
                    start=entry.start,
                    end=entry.end,
                    thickness=thickness,
                    horizon_id=above_h,
                    reason="cored segments immediately above and below are "
                    "assigned to the same horizon",
                )
            )
        else:
            why = _undetermined_reason(above, below, above_h, below_h)
            gap_records.append(
                GapAttribution(
                    borehole_id=borehole.borehole_id,
                    gap_id=entry.ref.gap_id,
                    start=entry.start,
                    end=entry.end,
                    thickness=thickness,
                    horizon_id=None,
                    reason=why,
                )
            )

    # --- Horizon geometry ------------------------------------------------- #
    grouped: dict[str, dict] = {}
    for segment in borehole.segments:
        horizon_id = assignment_by_segment[segment.segment_id]
        if horizon_id is None:
            continue  # UNKNOWN segments never enter known-horizon statistics
        bucket = grouped.setdefault(
            horizon_id,
            {
                "top": R.to_fraction(segment.start),
                "bottom": R.to_fraction(segment.end),
                "cored": R.Fraction(0),
                "segments": [],
                "gaps": [],
            },
        )
        bucket["top"] = min(bucket["top"], R.to_fraction(segment.start))
        bucket["bottom"] = max(bucket["bottom"], R.to_fraction(segment.end))
        bucket["cored"] += R.to_fraction(segment.end) - R.to_fraction(segment.start)
        bucket["segments"].append(segment.segment_id)

    for gap in gap_records:
        if gap.horizon_id is None:
            continue
        bucket = grouped[gap.horizon_id]
        bucket["gaps"].append(gap.gap_id)

    stats = [
        HorizonStat(
            borehole_id=borehole.borehole_id,
            horizon_id=horizon_id,
            top_depth=R.to_str(bucket["top"]),
            bottom_depth=R.to_str(bucket["bottom"]),
            cored_thickness=R.to_str(bucket["cored"]),
            gap_thickness=R.to_str(
                sum(
                    (R.to_fraction(g.thickness) for g in gap_records
                     if g.horizon_id == horizon_id),
                    R.Fraction(0),
                )
            ),
            segment_ids=tuple(bucket["segments"]),
            gap_ids=tuple(bucket["gaps"]),
        )
        for horizon_id, bucket in sorted(grouped.items())
    ]
    return stats, gap_records


def _nearest_segment(
    entries: list[OrderedEntry], pos: int, direction: int
) -> SegmentInput | None:
    i = pos + direction
    while 0 <= i < len(entries):
        if entries[i].kind == "segment":
            return entries[i].ref  # type: ignore[return-value]
        i += direction
    return None


def _undetermined_reason(
    above: SegmentInput | None,
    below: SegmentInput | None,
    above_h: str | None,
    below_h: str | None,
) -> str:
    if above is None or below is None:
        return "gap is not bounded by cored segments on both sides"
    if above_h is None or below_h is None:
        return "at least one bounding segment is UNKNOWN"
    return (
        f"bounding segments belong to different horizons "
        f"({above_h!r} above, {below_h!r} below); cross-horizon gaps are not "
        f"apportioned"
    )


# --------------------------------------------------------------------------- #
# Sample membership
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SampleMembership:
    sample_id: str
    depth: str
    borehole_id: str | None
    status: str
    segment_id: str | None
    horizon_id: str | None
    gap_id: str | None
    note: str


def locate_sample_in_borehole(
    sample: SampleInput,
    borehole: BoreholeInput,
    solution: HoleSolution,
) -> SampleMembership | None:
    """Return membership if the sample depth falls inside this borehole."""
    depth = R.to_fraction(sample.depth)
    assignment_by_segment = {a.segment_id: a.horizon_id for a in solution.assignments}
    for segment in borehole.segments:  # half-open [start, end)
        if R.to_fraction(segment.start) <= depth < R.to_fraction(segment.end):
            horizon_id = assignment_by_segment[segment.segment_id]
            if horizon_id is None:
                return SampleMembership(
                    sample.sample_id, sample.depth, borehole.borehole_id,
                    SAMPLE_UNKNOWN, segment.segment_id, None, None,
                    "sample lies in a segment assigned to UNKNOWN and cannot "
                    "count toward known-horizon statistics",
                )
            return SampleMembership(
                sample.sample_id, sample.depth, borehole.borehole_id,
                SAMPLE_HIT, segment.segment_id, horizon_id, None,
                f"sample lies in [start, end) of segment {segment.segment_id}",
            )
    for gap in borehole.gaps:  # half-open [start, end)
        if R.to_fraction(gap.start) <= depth < R.to_fraction(gap.end):
            return SampleMembership(
                sample.sample_id, sample.depth, borehole.borehole_id,
                SAMPLE_GAP, None, None, gap.gap_id,
                "sample lies inside an explicit uncored gap",
            )
    return None


def assign_samples(
    samples: list[SampleInput],
    boreholes: dict[str, BoreholeInput],
    solutions: dict[str, HoleSolution],
) -> list[SampleMembership]:
    results: list[SampleMembership] = []
    for sample in sorted(samples, key=lambda s: s.sample_id):
        if sample.borehole_id is not None:
            borehole = boreholes.get(sample.borehole_id)
            if borehole is None:
                results.append(
                    SampleMembership(
                        sample.sample_id, sample.depth, sample.borehole_id,
                        SAMPLE_MISS, None, None, None,
                        f"borehole {sample.borehole_id!r} does not exist",
                    )
                )
                continue
            found = locate_sample_in_borehole(
                sample, borehole, solutions[sample.borehole_id]
            )
            results.append(
                found
                or SampleMembership(
                    sample.sample_id, sample.depth, sample.borehole_id,
                    SAMPLE_MISS, None, None, None,
                    "depth is outside every [start, end) segment and gap; "
                    "a depth equal to the deepest segment end remains a miss",
                )
            )
            continue

        candidates = [
            membership
            for borehole in boreholes.values()
            if (membership := locate_sample_in_borehole(
                sample, borehole, solutions[borehole.borehole_id]
            ))
            is not None
        ]
        if len(candidates) > 1:
            results.append(
                SampleMembership(
                    sample.sample_id, sample.depth, None,
                    SAMPLE_AMBIGUOUS, None, None, None,
                    "depth is contained in more than one borehole: "
                    + ", ".join(sorted(m.borehole_id or "?" for m in candidates)),
                )
            )
        elif candidates:
            results.append(candidates[0])
        else:
            results.append(
                SampleMembership(
                    sample.sample_id, sample.depth, None,
                    SAMPLE_MISS, None, None, None,
                    "depth is outside every segment and gap of every borehole",
                )
            )
    return results
