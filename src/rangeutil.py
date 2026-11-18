"""HTTP byte-range, ETag and coverage-interval helpers.

Shared by the object source service and the assembler so both sides agree on
normalisation rules while still being separate modules.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Sequence


ETAG_RE = re.compile(r'^"([0-9a-fA-F]{64})"$')
CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$", re.IGNORECASE)
UNSATISFIED_CONTENT_RANGE_RE = re.compile(r"^bytes\s+\*/(\d+)$", re.IGNORECASE)
RANGE_TOKEN_RE = re.compile(r"^(\d*)-(\d*)$")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def quote_etag(digest_hex: str) -> str:
    """Strong ETag, exactly the lower-case SHA256 of the version body."""
    return '"%s"' % digest_hex.lower()


def strong_etag_digest(value: str | None) -> str | None:
    """Return the SHA256 digest from a strong ETag, or None.

    Weak validators (W/"...") deliberately do not match: every version here is
    immutable and the strong comparison is what binds an assembly to bytes.
    """
    if not value:
        return None
    m = ETAG_RE.match(value.strip())
    return m.group(1).lower() if m else None


@dataclass(frozen=True, order=True)
class ByteRange:
    """Inclusive byte interval."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def to_header(self) -> str:
        return f"{self.start}-{self.end}"

    def as_list(self) -> list[int]:
        return [self.start, self.end]


class RangeHeaderError(ValueError):
    """Range header is syntactically invalid (400)."""


class RangeNotSatisfiable(ValueError):
    """Every requested range is unsatisfiable (416).

    ``total`` is the object length used for ``Content-Range: bytes */total``.
    """

    def __init__(self, total: int):
        super().__init__("no satisfiable byte range")
        self.total = total


def parse_range_header(value: str, total: int) -> list[ByteRange]:
    """Parse an HTTP ``Range: bytes=...`` header.

    Rules (RFC 7233, with explicit errors):
      * ``a-b`` clamps ``b`` to ``total-1``; ``a >= total`` is unsatisfiable.
      * ``a-`` runs through the last byte.
      * ``-n`` is the last n bytes; ``n <= 0`` is unsatisfiable.
      * ``a-b`` with ``b < a`` is unsatisfiable.
      * Unsatisfiable members of a multi-range request are ignored while at
        least one satisfiable member remains; otherwise ``RangeNotSatisfiable``.
      * Anything else raises ``RangeHeaderError``.
    """

    if value is None:
        raise RangeHeaderError("missing Range header")
    text = value.strip()
    unit, sep, rest = text.partition("=")
    if not sep or unit.strip().lower() != "bytes":
        raise RangeHeaderError("only the bytes range unit is supported")
    specs = rest.split(",")
    if not specs or any(not s.strip() for s in specs):
        raise RangeHeaderError("empty range member")

    ranges: list[ByteRange] = []
    unsatisfiable = 0
    for spec in specs:
        token = spec.strip()
        m = RANGE_TOKEN_RE.match(token)
        if not m:
            raise RangeHeaderError(f"bad range member: {token!r}")
        first, last = m.groups()
        if first == "" and last == "":
            raise RangeHeaderError("range member has neither start nor suffix")

        if first == "":
            suffix_len = int(last)
            if suffix_len <= 0 or total == 0:
                unsatisfiable += 1
                continue
            start = max(0, total - suffix_len)
            end = total - 1
        else:
            start = int(first)
            if start >= total:
                unsatisfiable += 1
                continue
            if last == "":
                end = total - 1
            else:
                end = int(last)
                if end < start:
                    unsatisfiable += 1
                    continue
                end = min(end, total - 1)
        ranges.append(ByteRange(start, end))

    if not ranges:
        if unsatisfiable:
            raise RangeNotSatisfiable(total)
        raise RangeHeaderError("no range members")
    return ranges


def parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    """Return ``(start, end, total)`` from a satisfiable Content-Range."""
    if not value:
        return None
    m = CONTENT_RANGE_RE.match(value.strip())
    if not m:
        return None
    start, end, total = (int(g) for g in m.groups())
    if start > end or end >= total:
        return None
    return start, end, total


def parse_unsatisfied_content_range(value: str | None) -> int | None:
    if not value:
        return None
    m = UNSATISFIED_CONTENT_RANGE_RE.match(value.strip())
    return int(m.group(1)) if m else None


def merge_ranges(ranges: Iterable[ByteRange]) -> list[ByteRange]:
    """Sort and coalesce overlapping or adjacent ranges.

    The server may use this; clients must therefore never assume that one
    requested range produces one response part.
    """
    ordered = sorted(ranges)
    if not ordered:
        return []
    out = [ByteRange(ordered[0].start, ordered[0].end)]
    for r in ordered[1:]:
        cur = out[-1]
        if r.start <= cur.end + 1:
            out[-1] = ByteRange(cur.start, max(cur.end, r.end))
        else:
            out.append(ByteRange(r.start, r.end))
    return out


def merge_overlaps(ranges: Iterable[ByteRange]) -> list[ByteRange]:
    """Coalesce only *overlapping* ranges; adjacent ranges stay separate.

    Used when building a request list where adjacent capped pieces must remain
    distinct, otherwise a server that merges adjacent ranges would recreate
    the very big range we are trying to avoid.
    """
    ordered = sorted(ranges)
    if not ordered:
        return []
    out = [ByteRange(ordered[0].start, ordered[0].end)]
    for r in ordered[1:]:
        cur = out[-1]
        if r.start <= cur.end:
            out[-1] = ByteRange(cur.start, max(cur.end, r.end))
        else:
            out.append(ByteRange(r.start, r.end))
    return out


def normalize_intervals(intervals: Iterable[tuple[int, int] | ByteRange]) -> list[ByteRange]:
    return merge_ranges(ByteRange(x[0], x[1]) if not isinstance(x, ByteRange) else x
                        for x in intervals)


def covered_length(intervals: Iterable[tuple[int, int] | ByteRange]) -> int:
    return sum(r.length for r in normalize_intervals(intervals))


def holes(total: int, intervals: Iterable[tuple[int, int] | ByteRange]) -> list[ByteRange]:
    """Complement of confirmed intervals in ``[0, total)``."""
    if total <= 0:
        return []
    out: list[ByteRange] = []
    cursor = 0
    for r in normalize_intervals(intervals):
        if r.start > cursor:
            out.append(ByteRange(cursor, min(r.start - 1, total - 1)))
        cursor = max(cursor, r.end + 1)
        if cursor >= total:
            break
    if cursor < total:
        out.append(ByteRange(cursor, total - 1))
    return out


def subtract_intervals(total: int,
                       wanted: Iterable[ByteRange],
                       confirmed: Iterable[tuple[int, int] | ByteRange]) -> list[ByteRange]:
    """Return the parts of ``wanted`` not covered by ``confirmed``."""
    conf = normalize_intervals(confirmed)
    out: list[ByteRange] = []
    for w in normalize_intervals(wanted):
        cursor = w.start
        for c in conf:
            if c.end < cursor:
                continue
            if c.start > w.end:
                break
            if c.start > cursor:
                out.append(ByteRange(cursor, min(c.start - 1, w.end)))
            cursor = max(cursor, c.end + 1)
            if cursor > w.end:
                break
        if cursor <= w.end:
            out.append(ByteRange(cursor, w.end))
    return out


def intersects(a: ByteRange, b: ByteRange) -> bool:
    return a.start <= b.end and b.start <= a.end


def intersection(a: ByteRange, b: ByteRange) -> ByteRange | None:
    lo = max(a.start, b.start)
    hi = min(a.end, b.end)
    return ByteRange(lo, hi) if lo <= hi else None


def ranges_within(wanted: Sequence[ByteRange], parts: Sequence[ByteRange]) -> bool:
    """True if every part is fully inside the union of wanted ranges."""
    if not parts:
        return True
    union = normalize_intervals(wanted)
    for p in parts:
        if not any(w.start <= p.start and p.end <= w.end for w in union):
            return False
    return True


def format_content_range(r: ByteRange, total: int) -> str:
    return f"bytes {r.start}-{r.end}/{total}"
