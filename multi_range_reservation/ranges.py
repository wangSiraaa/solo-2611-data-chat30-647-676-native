"""Canonical finite integer-range algebra.

A *range value* is a finite set of integer points.  It is stored as a tuple of
half-open integer intervals ``[lower, upper)`` that is canonical:

* no empty segment,
* strictly ascending,
* pairwise non-overlapping and non-adjacent (``previous.upper < next.lower``).

Canonical form makes set equality structural equality: two inputs denote the
same integer set if and only if their :class:`RangeValue` instances are equal.
That property is deliberately *not* based on sorted raw pairs -- merging
overlaps and adjacency is what prevents ``[1,2) U [3,4)`` from being confused
with ``[1,4)`` and what keeps hole ``2`` a hole.

Inputs
------
A value may be given as

* a compact string: ``"[1, 3) (5, 8]"`` (whitespace/comma separated),
* a list/tuple of segments,
* a single segment dict/tuple,
* a mapping with ``"half_open"``, ``"segments"`` or ``"ranges"``,
* another :class:`RangeValue`.

A segment has a lower endpoint, an upper endpoint and two inclusion flags.  For
dicts the accepted key aliases are ``lower``/``lo``/``start`` and
``upper``/``hi``/``end``; inclusion flags default to ``True`` and may be named
``lower_inclusive``/``include_lower``/``lower_closed``/``contains_lower`` and
the corresponding ``upper_*`` names.  A 2-tuple means a closed-closed segment;
a 4-tuple is ``(lower, upper, lower_inclusive, upper_inclusive)``.

Endpoints are integers in ``[-1_000_000, 1_000_000]``.  A segment whose integer
set is empty (for example ``[4, 4)``, ``(2, 3)`` or even an inverted pair) is
legal and contributes no members.  Empty endpoints of an interval are never
"filled in": ``[1,2)`` and ``[3,4)`` remain two segments because point 2 is
absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping, Sequence

__all__ = [
    "DEFAULT_RANGE_SERVICE",
    "LIMIT",
    "RangeError",
    "RangeService",
    "RangeValue",
    "canonicalize",
    "contains",
    "difference",
    "intersection",
    "intersects",
    "is_empty",
    "normalize",
    "union",
]

LIMIT = 1_000_000
# Canonical half-open intervals may end one past the largest endpoint because
# the original upper endpoint was inclusive.
_HALF_OPEN_CEILING = LIMIT + 1

Pair = tuple[int, int]
Segment = tuple[int, int, bool, bool]

_SEGMENT_RE = re.compile(r"[\(\[]\s*([+-]?\d+)\s*,\s*([+-]?\d+)\s*[\)\]]")
_SEPARATOR_RE = re.compile(r"(?:\s|,|;|∪)*\Z")

_LOWER_KEYS = ("lower", "lo", "start")
_UPPER_KEYS = ("upper", "hi", "end")
_LOWER_INCLUSIVE_KEYS = (
    "lower_inclusive",
    "include_lower",
    "lower_closed",
    "contains_lower",
    "contains_lower_endpoint",
    "left_closed",
)
_UPPER_INCLUSIVE_KEYS = (
    "upper_inclusive",
    "include_upper",
    "upper_closed",
    "contains_upper",
    "contains_upper_endpoint",
    "right_closed",
)


class RangeError(ValueError):
    """Raised when a range value cannot be parsed or violates endpoint rules."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_endpoint(
    value: object,
    what: str,
    *,
    upper: bool = False,
    inclusive: bool = True,
) -> int:
    if not _is_int(value):
        raise RangeError(f"{what} endpoint must be an integer, got {value!r}")
    endpoint = int(value)  # type: ignore[arg-type]
    # An exclusive upper endpoint may be LIMIT + 1: it is the canonical
    # representation of a segment ending at LIMIT, e.g. [LIMIT - 1, LIMIT].
    if upper and not inclusive:
        ceiling = _HALF_OPEN_CEILING
    else:
        ceiling = LIMIT
    if endpoint < -LIMIT or endpoint > ceiling:
        raise RangeError(
            f"{what} endpoint {endpoint} is outside the supported domain"
        )
    return endpoint


def _check_flag(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise RangeError(f"{what} inclusion flag must be bool, got {value!r}")
    return value


def _canonical_pairs(spans: Iterable[Pair]) -> tuple[Pair, ...]:
    """Sort and merge half-open spans, dropping empty ones."""

    ordered = sorted((int(lo), int(hi)) for lo, hi in spans if lo < hi)
    merged: list[list[int]] = []
    for lo, hi in ordered:
        if merged and lo <= merged[-1][1]:
            if hi > merged[-1][1]:
                merged[-1][1] = hi
        else:
            merged.append([lo, hi])
    return tuple((lo, hi) for lo, hi in merged)


def _canonicalize_segments(segments: Iterable[Segment]) -> tuple[Pair, ...]:
    spans: list[Pair] = []
    for lower, upper, lower_inclusive, upper_inclusive in segments:
        lo_closed = _check_flag(lower_inclusive, "lower")
        hi_closed = _check_flag(upper_inclusive, "upper")
        lo = _check_endpoint(lower, "lower")
        hi = _check_endpoint(upper, "upper", upper=True, inclusive=hi_closed)
        start = lo if lo_closed else lo + 1
        end = hi + 1 if hi_closed else hi
        if start < end:
            spans.append((start, end))
    return _canonical_pairs(spans)


def _segment_from_mapping(value: Mapping[str, object]) -> Segment:
    lower_key = next((k for k in _LOWER_KEYS if k in value), None)
    upper_key = next((k for k in _UPPER_KEYS if k in value), None)
    if lower_key is None or upper_key is None:
        raise RangeError(
            f"segment mapping needs a lower key {_LOWER_KEYS} and an upper key {_UPPER_KEYS}"
        )
    lower = value[lower_key]
    upper = value[upper_key]

    def read_flag(keys: Sequence[str], label: str) -> bool:
        present = [value[k] for k in keys if k in value]
        if not present:
            return True
        first = _check_flag(present[0], label)
        if any(flag != first for flag in present[1:]):
            raise RangeError(f"conflicting {label} inclusion flags")
        return first

    return (
        lower,  # type: ignore[return-value]
        upper,  # type: ignore[return-value]
        read_flag(_LOWER_INCLUSIVE_KEYS, "lower"),
        read_flag(_UPPER_INCLUSIVE_KEYS, "upper"),
    )


def _segment_from_sequence(value: Sequence[object]) -> Segment:
    if len(value) == 2:
        return (value[0], value[1], True, True)  # type: ignore[return-value]
    if len(value) == 4:
        return (
            value[0],
            value[1],
            value[2],  # type: ignore[arg-type]
            value[3],  # type: ignore[arg-type]
        )
    raise RangeError(
        "a segment sequence must have 2 entries (closed by default) or "
        "4 entries (lower, upper, lower_inclusive, upper_inclusive)"
    )


def _looks_like_segment_item(value: object) -> bool:
    return isinstance(value, Mapping) or (
        isinstance(value, (list, tuple)) and len(value) in (2, 4)
    )


def _is_single_segment_sequence(value: Sequence[object]) -> bool:
    if len(value) == 2:
        return _is_int(value[0]) and _is_int(value[1])
    if len(value) == 4:
        return (
            _is_int(value[0])
            and _is_int(value[1])
            and isinstance(value[2], bool)
            and isinstance(value[3], bool)
        )
    return False


def _parse_string(text: str) -> list[Segment]:
    stripped = text.strip()
    if stripped in ("", "∅", "{}", "[]", "()"):
        # Explicit notation for the empty value; an empty list is also
        # accepted by the sequence parser, this covers CLI/text input.
        return []
    segments: list[Segment] = []
    position = 0
    for match in _SEGMENT_RE.finditer(text):
        gap = text[position : match.start()]
        if not _SEPARATOR_RE.fullmatch(gap):
            raise RangeError(f"cannot parse range text near {gap!r}")
        lower = int(match.group(1))
        upper = int(match.group(2))
        lower_inclusive = text[match.start()] == "["
        upper_inclusive = text[match.end() - 1] == "]"
        segments.append((lower, upper, lower_inclusive, upper_inclusive))
        position = match.end()
    tail = text[position:]
    if not _SEPARATOR_RE.fullmatch(tail):
        raise RangeError(f"cannot parse range text near {tail!r}")
    if not segments and text.strip():
        raise RangeError(f"cannot parse range text {text!r}")
    return segments


def _parse_value(value: object) -> list[Segment]:
    if isinstance(value, RangeValue):
        # Caller handles this case directly; kept for completeness.
        return [
            (lo, hi - 1, True, True) for lo, hi in value.pairs
        ]
    if isinstance(value, str):
        return _parse_string(value)
    if isinstance(value, Mapping):
        if not value:
            return []
        if "ranges" in value:
            return _parse_value(value["ranges"])
        if "segments" in value:
            return _parse_value(value["segments"])
        if "half_open" in value:
            return _segments_from_half_open(value["half_open"])
        return [_segment_from_mapping(value)]
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            return []
        if _is_single_segment_sequence(value):
            return [_segment_from_sequence(value)]
        if all(isinstance(item, str) for item in value):
            parsed: list[Segment] = []
            for item in value:
                parsed.extend(_parse_string(item))
            return parsed
        if not all(_looks_like_segment_item(item) for item in value):
            raise RangeError(
                "a tuple/list is either one segment or a list of segments"
            )
        segments: list[Segment] = []
        for item in value:
            if isinstance(item, Mapping):
                segments.append(_segment_from_mapping(item))
            elif _is_single_segment_sequence(item):  # type: ignore[arg-type]
                segments.append(_segment_from_sequence(item))  # type: ignore[arg-type]
            else:
                raise RangeError(f"invalid segment {item!r}")
        return segments
    raise RangeError(f"unsupported range value {value!r}")


def _segments_from_half_open(value: object) -> list[Segment]:
    if not isinstance(value, (list, tuple)):
        raise RangeError("'half_open' must be a list of [lower, upper) pairs")
    segments: list[Segment] = []
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise RangeError(f"half-open pair must have two endpoints, got {pair!r}")
        lo, hi = pair
        if not _is_int(lo) or not _is_int(hi):
            raise RangeError(f"half-open endpoints must be integers, got {pair!r}")
        lo = int(lo)
        hi = int(hi)
        if lo < -LIMIT or lo > LIMIT or hi < -LIMIT or hi > _HALF_OPEN_CEILING:
            raise RangeError(
                f"half-open pair {pair!r} is outside the supported domain"
            )
        if lo >= hi:
            continue
        # Convert without changing the represented points: [lo, hi) is the same
        # closed-closed segment [lo, hi - 1] only when hi > lo.
        segments.append((lo, hi - 1, True, True))
    return segments


@dataclass(frozen=True, init=False)
class RangeValue:
    """Immutable canonical finite set of integer points."""

    pairs: tuple[Pair, ...]

    def __init__(self, value: object = ()) -> None:
        if isinstance(value, RangeValue):
            object.__setattr__(self, "pairs", value.pairs)
            return
        object.__setattr__(self, "pairs", _canonicalize_segments(_parse_value(value)))

    # -- construction ---------------------------------------------------
    @classmethod
    def _from_canonical(cls, pairs: Iterable[Pair]) -> "RangeValue":
        """Internal fast path; the caller guarantees canonical pairs."""

        obj = object.__new__(cls)
        object.__setattr__(obj, "pairs", tuple(pairs))
        return obj

    @classmethod
    def from_half_open(cls, pairs: Iterable[Sequence[int]]) -> "RangeValue":
        """Build from half-open ``[lower, upper)`` pairs (canonicalized)."""

        return cls({"half_open": list(pairs)})

    @classmethod
    def empty(cls) -> "RangeValue":
        return cls._from_canonical(())

    # -- inspection -----------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not self.pairs

    @property
    def segment_count(self) -> int:
        return len(self.pairs)

    def as_pairs(self) -> list[list[int]]:
        return [[lo, hi] for lo, hi in self.pairs]

    def as_segments(self) -> list[dict[str, object]]:
        return [
            {
                "lower": lo,
                "upper": hi - 1,
                "lower_inclusive": True,
                "upper_inclusive": True,
            }
            for lo, hi in self.pairs
        ]

    def to_dict(self) -> dict[str, object]:
        return {
            "half_open": self.as_pairs(),
            "text": self.to_text(),
        }

    def to_text(self) -> str:
        if not self.pairs:
            return "∅"
        return " ∪ ".join(f"[{lo}, {hi})" for lo, hi in self.pairs)

    def __str__(self) -> str:
        return self.to_text()

    def __repr__(self) -> str:
        return f"RangeValue({self.to_text()})"

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def contains(self, point: int) -> bool:
        if not _is_int(point):
            raise RangeError(f"point must be an integer, got {point!r}")
        for lo, hi in self.pairs:
            if point < lo:
                return False
            if point < hi:
                return True
        return False

    def __contains__(self, point: object) -> bool:
        return self.contains(point)  # type: ignore[arg-type]

    def iter_points(self) -> Iterator[int]:
        for lo, hi in self.pairs:
            yield from range(lo, hi)

    # -- algebra --------------------------------------------------------
    def union(self, *others: object) -> "RangeValue":
        return union(self, *others)

    def intersection(self, other: object) -> "RangeValue":
        return intersection(self, other)

    def difference(self, other: object) -> "RangeValue":
        return difference(self, other)

    def intersects(self, other: object) -> bool:
        return intersects(self, other)


def normalize(value: object = ()) -> RangeValue:
    """Return the canonical equivalence class of ``value``."""

    return value if isinstance(value, RangeValue) else RangeValue(value)


def canonicalize(value: object = ()) -> RangeValue:
    """Alias for :func:`normalize`."""

    return normalize(value)


def is_empty(value: object) -> bool:
    return normalize(value).is_empty


def contains(value: object, point: int) -> bool:
    return normalize(value).contains(point)


def union(*values: object) -> RangeValue:
    spans: list[Pair] = []
    for value in values:
        spans.extend(normalize(value).pairs)
    return RangeValue._from_canonical(_canonical_pairs(spans))


def intersection(left: object, right: object) -> RangeValue:
    a = normalize(left).pairs
    b = normalize(right).pairs
    result: list[Pair] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            result.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        elif b[j][1] < a[i][1]:
            j += 1
        else:
            i += 1
            j += 1
    return RangeValue._from_canonical(result)


def difference(left: object, right: object) -> RangeValue:
    a = normalize(left).pairs
    b = normalize(right).pairs
    result: list[Pair] = []
    for a_lo, a_hi in a:
        cursor = a_lo
        for b_lo, b_hi in b:
            if b_hi <= cursor:
                continue
            if b_lo >= a_hi:
                break
            if b_lo > cursor:
                result.append((cursor, min(b_lo, a_hi)))
            cursor = max(cursor, b_hi)
            if cursor >= a_hi:
                break
        if cursor < a_hi:
            result.append((cursor, a_hi))
    return RangeValue._from_canonical(_canonical_pairs(result))


def intersects(left: object, right: object) -> bool:
    a = normalize(left).pairs
    b = normalize(right).pairs
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i][1] <= b[j][0]:
            i += 1
        elif b[j][1] <= a[i][0]:
            j += 1
        else:
            return True
    return False


class RangeService:
    """Thin object wrapper exposing the algebra used by the repository.

    The repository only depends on this small protocol (``normalize``,
    ``union``, ``intersection``, ``difference``, ``contains``, ``intersects``),
    which lets tests substitute an equivalent implementation.  Every operation
    is expressed in terms of ``self.normalize`` so that a subclass overriding
    normalization changes all algebra coherently instead of only parsing.
    """

    def normalize(self, value: object = ()) -> RangeValue:
        return normalize(value)

    canonicalize = normalize

    def _pairs(self, value: object) -> tuple[Pair, ...]:
        return self.normalize(value).pairs

    def union(self, *values: object) -> RangeValue:
        spans: list[Pair] = []
        for value in values:
            spans.extend(self._pairs(value))
        return RangeValue._from_canonical(_canonical_pairs(spans))

    def intersection(self, left: object, right: object) -> RangeValue:
        return intersection_from_pairs(self._pairs(left), self._pairs(right))

    def difference(self, left: object, right: object) -> RangeValue:
        return difference_from_pairs(self._pairs(left), self._pairs(right))

    def contains(self, value: object, point: int) -> bool:
        return self.normalize(value).contains(point)

    def intersects(self, left: object, right: object) -> bool:
        return intersects_from_pairs(self._pairs(left), self._pairs(right))

    def is_empty(self, value: object) -> bool:
        return self.normalize(value).is_empty


def intersection_from_pairs(
    a: Sequence[Pair], b: Sequence[Pair]
) -> RangeValue:
    result: list[Pair] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            result.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        elif b[j][1] < a[i][1]:
            j += 1
        else:
            i += 1
            j += 1
    return RangeValue._from_canonical(result)


def difference_from_pairs(
    a: Sequence[Pair], b: Sequence[Pair]
) -> RangeValue:
    result: list[Pair] = []
    for a_lo, a_hi in a:
        cursor = a_lo
        for b_lo, b_hi in b:
            if b_hi <= cursor:
                continue
            if b_lo >= a_hi:
                break
            if b_lo > cursor:
                result.append((cursor, min(b_lo, a_hi)))
            cursor = max(cursor, b_hi)
            if cursor >= a_hi:
                break
        if cursor < a_hi:
            result.append((cursor, a_hi))
    return RangeValue._from_canonical(_canonical_pairs(result))


def intersects_from_pairs(a: Sequence[Pair], b: Sequence[Pair]) -> bool:
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i][1] <= b[j][0]:
            i += 1
        elif b[j][1] <= a[i][0]:
            j += 1
        else:
            return True
    return False


DEFAULT_RANGE_SERVICE = RangeService()
