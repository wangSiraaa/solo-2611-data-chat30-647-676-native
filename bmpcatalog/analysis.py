"""Region aggregation over immutable BMP products.

Two execution strategies share the same public surface:

``direct_*`` helpers
    Read the requested rows through the catalog's public rectangle API every
    time.  They are the simple reference used by tests.

``RegionAnalyzer``
    Splits a product into square tiles and caches exact :class:`PixelTotals`
    per tile.  A rectangle is answered by combining whole-tile totals with
    reads of only the fractional edge tiles.  A union is computed by a
    horizontal sweep line: between consecutive rectangle edges the covered
    x-intervals are merged, and each merged interval is answered through the
    same tile cache.  Overlap is removed during the merge, so every pixel is
    counted exactly once and no per-image mask is allocated.

The working set during a query is bounded by the interval lists (O(number of
rectangles)), one decoded tile row, and the tile cache, which has a hard entry
cap.  A full image is never copied per rectangle.

Reports are immutable snapshots bound to the product digest and to logical
top-left-origin rectangles.  Revising a product produces a new product and new
reports; old reports keep describing the old product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .bmp import BmpPixels, PixelTotals
from .geometry import Rectangle, as_rectangle, coerce_rectangles

DEFAULT_TILE_SIZE = 64
DEFAULT_MAX_CACHED_TILES = 4096


def direct_rect_stats(product: BmpPixels, rectangle) -> "RegionStats":
    rect = as_rectangle(rectangle).validate_inside(product.width, product.height)
    return RegionStats(rect, product.totals(rect))


def direct_union_stats(product: BmpPixels, rectangles: Iterable) -> "UnionStats":
    """Reference union implementation: per-row interval merging, no masks.

    It does not use the tile cache or the analyzer's sweep line, so tests can
    compare two independent exact strategies.
    """

    rects = coerce_rectangles(rectangles)
    for rect in rects:
        rect.validate_inside(product.width, product.height)
    if not rects:
        return UnionStats(rectangles=(), totals=PixelTotals.empty())

    by_row: dict[int, list[tuple[int, int]]] = {}
    for rect in rects:
        for y in range(rect.y0, rect.y1):
            by_row.setdefault(y, []).append((rect.x0, rect.x1))

    totals = PixelTotals.empty()
    for y in sorted(by_row):
        for x0, x1 in _merge_intervals(by_row[y]):
            totals = totals + product.totals(Rectangle(x0, y, x1, y + 1))
    return UnionStats(rectangles=rects, totals=totals)


@dataclass(frozen=True, slots=True)
class RegionStats:
    rectangle: Rectangle
    totals: PixelTotals

    @property
    def count(self) -> int:
        return self.totals.count

    @property
    def sums(self) -> tuple[int, int, int, int]:
        return self.totals.channel_sums

    @property
    def means(self) -> tuple[float, float, float, float]:
        return self.totals.means

    @property
    def transparent(self) -> int:
        return self.totals.transparent

    @property
    def opaque(self) -> int:
        return self.totals.opaque

    @property
    def partial_alpha(self) -> int:
        return self.totals.partial

    def as_dict(self) -> dict[str, object]:
        return {
            "rectangle": self.rectangle.to_tuple(),
            "pixels": self.count,
            "sums_rgba": self.sums,
            "means_rgba": self.means,
            "transparent_pixels": self.transparent,
            "opaque_pixels": self.opaque,
            "partial_alpha_pixels": self.partial_alpha,
        }


@dataclass(frozen=True, slots=True)
class UnionStats:
    rectangles: tuple[Rectangle, ...]
    totals: PixelTotals

    @property
    def count(self) -> int:
        return self.totals.count

    @property
    def sums(self) -> tuple[int, int, int, int]:
        return self.totals.channel_sums

    @property
    def means(self) -> tuple[float, float, float, float]:
        return self.totals.means

    @property
    def transparent(self) -> int:
        return self.totals.transparent

    def as_dict(self) -> dict[str, object]:
        return {
            "rectangles": [rect.to_tuple() for rect in self.rectangles],
            "pixels": self.count,
            "sums_rgba": self.sums,
            "means_rgba": self.means,
            "transparent_pixels": self.transparent,
        }


@dataclass(frozen=True, slots=True)
class RegionReport:
    """Immutable report bound to one product version and logical coordinates."""

    source_sha256: str
    source_file_name: str
    source_width: int
    source_height: int
    source_revision: dict[str, object] | None
    regions: tuple[RegionStats, ...]
    union: UnionStats | None
    read_stats: dict[str, int] = field(default_factory=dict)

    def bound_to(self, product: BmpPixels) -> bool:
        """True when this report describes ``product`` exactly."""

        return (
            product.sha256 == self.source_sha256
            and product.width == self.source_width
            and product.height == self.source_height
        )

    def region(self, rectangle) -> RegionStats:
        rect = as_rectangle(rectangle)
        for stats in self.regions:
            if stats.rectangle == rect:
                return stats
        raise KeyError(f"rectangle {rect.to_tuple()} is not part of this report")

    def as_dict(self) -> dict[str, object]:
        return {
            "source_sha256": self.source_sha256,
            "source_file_name": self.source_file_name,
            "width": self.source_width,
            "height": self.source_height,
            "revision": self.source_revision,
            "regions": [stats.as_dict() for stats in self.regions],
            "union": self.union.as_dict() if self.union else None,
            "read_stats": dict(self.read_stats),
        }


class _TileCache:
    def __init__(self, max_entries: int) -> None:
        self._entries: dict[tuple[int, int], PixelTotals] = {}
        self._max_entries = max(1, max_entries)

    def get(self, key: tuple[int, int]) -> PixelTotals | None:
        return self._entries.get(key)

    def put(self, key: tuple[int, int], value: PixelTotals) -> None:
        if len(self._entries) >= self._max_entries and key not in self._entries:
            # FIFO eviction: Python dicts preserve insertion order.  This keeps
            # the cache's memory bounded regardless of query shape.
            oldest = next(iter(self._entries))
            del self._entries[oldest]
        self._entries[key] = value

    def __len__(self) -> int:
        return len(self._entries)


class RegionAnalyzer:
    """Tile-cached exact statistics for one product version."""

    def __init__(
        self,
        product: BmpPixels,
        tile_size: int = DEFAULT_TILE_SIZE,
        max_cached_tiles: int = DEFAULT_MAX_CACHED_TILES,
    ) -> None:
        if tile_size <= 0:
            raise ValueError("tile_size must be positive")
        self.product = product
        self.tile_size = tile_size
        self._tiles_x = (product.width + tile_size - 1) // tile_size
        self._tiles_y = (product.height + tile_size - 1) // tile_size
        self._cache = _TileCache(max_cached_tiles)
        self._tile_reads = 0
        self._partial_reads = 0
        self._cache_hits = 0
        self._pixels_counted = 0

    # ------------------------------------------------------------- metadata
    @property
    def working_set_bound(self) -> int:
        """Conservative bytes touched per query, excluding the source product.

        A decoded tile row and the capped tile cache.  The union sweep keeps
        only interval lists, which are bounded by the number of rectangles and
        are not image-sized.
        """

        tile = self.tile_size
        cache_entry = 256  # PixelTotals + dict key + dict slot, rounded up.
        return tile * 4 + self._cache._max_entries * cache_entry

    def reset_counters(self) -> None:
        self._tile_reads = 0
        self._partial_reads = 0
        self._cache_hits = 0
        self._pixels_counted = 0

    def counters(self) -> dict[str, int]:
        return {
            "whole_tile_reads": self._tile_reads,
            "partial_tile_reads": self._partial_reads,
            "cache_hits": self._cache_hits,
            "pixels_counted": self._pixels_counted,
            "cached_tiles": len(self._cache),
        }

    # --------------------------------------------------------------- tiles
    def _tile_rect(self, tx: int, ty: int) -> Rectangle:
        tile = self.tile_size
        x0 = tx * tile
        y0 = ty * tile
        x1 = min(x0 + tile, self.product.width)
        y1 = min(y0 + tile, self.product.height)
        return Rectangle(x0, y0, x1, y1)

    def _tile_totals(self, tx: int, ty: int) -> PixelTotals:
        key = (tx, ty)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache_hits += 1
            return cached
        rect = self._tile_rect(tx, ty)
        totals = self.product.totals(rect)
        self._tile_reads += 1
        self._pixels_counted += totals.count
        self._cache.put(key, totals)
        return totals

    # ---------------------------------------------------------- rect stats
    def rect_stats(self, rectangle) -> RegionStats:
        rect = as_rectangle(rectangle).validate_inside(
            self.product.width, self.product.height
        )
        return RegionStats(rect, self._rect_totals(rect))

    def _rect_totals(self, rect: Rectangle) -> PixelTotals:
        tile = self.tile_size
        totals = PixelTotals.empty()
        tx0 = rect.x0 // tile
        tx1 = (rect.x1 - 1) // tile
        ty0 = rect.y0 // tile
        ty1 = (rect.y1 - 1) // tile
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                tile_rect = self._tile_rect(tx, ty)
                overlap = rect.intersect(tile_rect)
                if overlap is None:
                    continue
                if overlap == tile_rect:
                    totals = totals + self._tile_totals(tx, ty)
                else:
                    totals = totals + self.product.totals(overlap)
                    self._partial_reads += 1
                    self._pixels_counted += overlap.area
        return totals

    # --------------------------------------------------------- union stats
    def union_stats(self, rectangles: Iterable) -> UnionStats:
        rects = coerce_rectangles(rectangles)
        self._validate_all(rects)
        totals = self._union_totals(rects)
        return UnionStats(rectangles=rects, totals=totals)

    def _validate_all(self, rects: tuple[Rectangle, ...]) -> None:
        for rect in rects:
            rect.validate_inside(self.product.width, self.product.height)

    def _union_totals(self, rects: tuple[Rectangle, ...]) -> PixelTotals:
        """Exact union totals via a horizontal sweep line with interval merging.

        Rectangles produce add/remove events at their top and bottom edges.
        Between consecutive event rows the set of covered x-intervals is
        constant, so it is merged once (dropping overlap) and each merged
        interval is answered as one logical rectangle across the whole row
        span.  Every pixel is therefore counted exactly once, even when many
        rectangles overlap; the active interval list and merged list are the
        only per-query structures, so memory stays O(number of rectangles).
        """

        if not rects:
            return PixelTotals.empty()

        events: dict[int, list[tuple[int, tuple[int, int]]]] = {}
        for rect in rects:
            key = (rect.x0, rect.x1)
            events.setdefault(rect.y0, []).append((1, key))
            events.setdefault(rect.y1, []).append((-1, key))

        active: dict[tuple[int, int], int] = {}
        totals = PixelTotals.empty()
        event_rows = sorted(events)
        for index, y in enumerate(event_rows):
            for delta, key in events[y]:
                count = active.get(key, 0) + delta
                if count:
                    active[key] = count
                else:
                    active.pop(key, None)
            if index + 1 >= len(event_rows):
                break
            next_y = event_rows[index + 1]
            if next_y <= y or not active:
                continue
            merged = _merge_intervals(active.keys())
            for x0, x1 in merged:
                totals = totals + self._rect_totals(
                    Rectangle(x0, y, x1, next_y)
                )
        return totals

    # ------------------------------------------------------------- reports
    def report(
        self, rectangles: Iterable, include_union: bool = True
    ) -> RegionReport:
        rects = coerce_rectangles(rectangles)
        self._validate_all(rects)
        self.reset_counters()
        regions = tuple(self.rect_stats(rect) for rect in rects)
        union = self.union_stats(rects) if include_union else None
        revision = None
        if self.product.revision is not None:
            revision = self.product.revision.as_dict()
        return RegionReport(
            source_sha256=self.product.sha256,
            source_file_name=self.product.file_name,
            source_width=self.product.width,
            source_height=self.product.height,
            source_revision=revision,
            regions=regions,
            union=union,
            read_stats=self.counters(),
        )

def _merge_intervals(intervals) -> list[tuple[int, int]]:
    """Merge half-open intervals; sorted output has no overlap or touching."""

    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for x0, x1 in ordered:
        if merged and x0 <= merged[-1][1]:
            if x1 > merged[-1][1]:
                merged[-1] = (merged[-1][0], x1)
        else:
            merged.append((x0, x1))
    return merged


def union_of(*rectangles) -> Rectangle | None:
    """Bounding rectangle of a group; not a replacement for exact union stats."""

    rects = coerce_rectangles(rectangles)
    if not rects:
        return None
    return Rectangle(
        min(r.x0 for r in rects),
        min(r.y0 for r in rects),
        max(r.x1 for r in rects),
        max(r.y1 for r in rects),
    )
