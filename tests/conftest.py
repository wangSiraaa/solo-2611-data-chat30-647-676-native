"""Shared fixtures: independent truth matrices and BMP builders.

The reference model here is deliberately naive: a list of logical top-left
RGBA rows.  Every statistic in the tests is recomputed from that matrix rather
than from the library's own decoder, so a shared bug in the catalog and the
analyzer cannot hide.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

from bmpcatalog import PixelTotals, Rectangle, build_bmp


RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class Truth:
    width: int
    height: int
    rows: tuple[tuple[RGBA, ...], ...]

    def pixel(self, x: int, y: int) -> RGBA:
        return self.rows[y][x]

    def rgba_bytes(self) -> bytes:
        return bytes(channel for row in self.rows for px in row for channel in px)

    def rect_totals(self, rect: Rectangle) -> PixelTotals:
        return manual_totals(self, [rect])

    def union_totals(self, rects: Sequence[Rectangle]) -> PixelTotals:
        return manual_totals(self, rects)


def manual_totals(truth: Truth, rects: Sequence[Rectangle]) -> PixelTotals:
    seen: set[tuple[int, int]] = set()
    for rect in rects:
        for y in range(rect.y0, rect.y1):
            for x in range(rect.x0, rect.x1):
                seen.add((x, y))
    r = g = b = a = 0
    transparent = opaque = partial = 0
    for x, y in seen:
        pr, pg, pb, pa = truth.pixel(x, y)
        r += pr
        g += pg
        b += pb
        a += pa
        if pa == 0:
            transparent += 1
        elif pa == 255:
            opaque += 1
        else:
            partial += 1
    return PixelTotals(len(seen), r, g, b, a, transparent, opaque, partial)


def smooth_truth(
    width: int,
    height: int,
    *,
    alpha: bool = False,
    quadrants: bool = False,
) -> Truth:
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            r = (x * 7 + y * 3) % 256
            g = (x * 5 + y * 11) % 256
            b = (x * 13 + y * 2) % 256
            if quadrants:
                if x < width // 2 and y < height // 2:
                    r, g, b = 10, 20, 30
                elif x >= width // 2 and y < height // 2:
                    r, g, b = 200, 40, 60
                elif x < width // 2:
                    r, g, b = 70, 180, 90
                else:
                    r, g, b = 21, 22, 23
            if alpha:
                a = (x * 17 + y * 29) % 256
                if (x + 2 * y) % 7 == 0:
                    a = 0
                elif (x + y) % 5 == 0:
                    a = 255
            else:
                a = 255
            row.append((r, g, b, a))
        rows.append(tuple(row))
    return Truth(width, height, tuple(rows))


def palette8(entries: int = 256) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        ((i * 7) % 256, (i * 31) % 256, (255 - i * 3) % 256) for i in range(entries)
    )


def truth_to_palette_indices(truth: Truth, palette) -> Truth:
    """Map an RGB truth through a palette to RGBA values the 8-bit file holds."""

    lookup = {}
    for r, g, b in palette:
        lookup.setdefault((r, g, b), (r, g, b))
    rows = []
    for row in truth.rows:
        out = []
        for r, g, b, a in row:
            key = (r, g, b)
            if key not in lookup:
                raise AssertionError(f"colour {key} is not in the palette")
            rr, gg, bb = lookup[key]
            out.append((rr, gg, bb, 255))
        rows.append(tuple(out))
    return Truth(truth.width, truth.height, tuple(rows))


def build_from_truth(
    truth: Truth,
    bpp: int,
    *,
    top_down: bool = False,
    palette=None,
    masks=None,
    padding_byte: int | None = None,
    letterbox_padding: bool = False,
    gap_bytes: int = 0,
) -> bytes:
    if padding_byte is None:
        padding_byte = 0xCD

    def padding_fn(row_index: int, length: int) -> bytes:
        if letterbox_padding:
            return bytes((0xFF if row_index % 2 == 0 else 0x00, 0x13, 0x57, 0x9B)[:length])
        return bytes((padding_byte,)) * length

    return build_bmp(
        truth.width,
        truth.height,
        bpp,
        truth.rows,
        top_down=top_down,
        palette=palette,
        masks=masks,
        padding_fn=padding_fn,
        gap_bytes=gap_bytes,
    )


def rectangles_for(width: int, height: int, count: int = 1000, seed: int = 20241118):
    rng = random.Random(seed)
    rects = []
    for _ in range(count):
        x0 = rng.randrange(width)
        y0 = rng.randrange(height)
        x1 = rng.randrange(x0 + 1, width + 1)
        y1 = rng.randrange(y0 + 1, height + 1)
        rects.append(Rectangle(x0, y0, x1, y1))
    return rects


# 8-8-8-8 channel masks (red, green, blue, alpha) in a 32-bit word.
RGBA8888_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)


@dataclass(frozen=True)
class TiledTruth:
    """A truth image that repeats a small tile in both directions.

    ``rect_totals`` is computed analytically in O(height) by using prefix sums
    of one tile period, so large-image checks do not iterate every pixel.
    """

    width: int
    height: int
    tile: Truth

    def pixel(self, x: int, y: int) -> RGBA:
        return self.tile.pixel(x % self.tile.width, y % self.tile.height)

    @property
    def rows(self):
        # Reuses the tile's pixel tuple objects, so this is references only.
        return tuple(
            tuple(self.pixel(x, y) for x in range(self.width))
            for y in range(self.height)
        )

    def _prefixes(self):
        tw = self.tile.width
        rows = []
        for row in self.tile.rows:
            pr = [0] * (tw + 1)
            pg = [0] * (tw + 1)
            pb = [0] * (tw + 1)
            pa = [0] * (tw + 1)
            pt = [0] * (tw + 1)
            po = [0] * (tw + 1)
            pp = [0] * (tw + 1)
            for i, (r, g, b, a) in enumerate(row):
                pr[i + 1] = pr[i] + r
                pg[i + 1] = pg[i] + g
                pb[i + 1] = pb[i] + b
                pa[i + 1] = pa[i] + a
                pt[i + 1] = pt[i] + (1 if a == 0 else 0)
                po[i + 1] = po[i] + (1 if a == 255 else 0)
                pp[i + 1] = pt[i + 1] + po[i + 1]
            rows.append((pr, pg, pb, pa, pt, po))
        return rows

    def _span(self, prefix, start: int, length: int) -> int:
        tw = self.tile.width
        full, rem = divmod(length, tw)
        period = prefix[tw] - prefix[0]
        off = start % tw
        total = full * period
        if rem:
            end = off + rem
            if end <= tw:
                total += prefix[end] - prefix[off]
            else:
                total += prefix[tw] - prefix[off] + prefix[end - tw] - prefix[0]
        return total

    def rect_totals(self, rect: Rectangle) -> PixelTotals:
        prefixes = self._prefixes()
        count = rect.area
        r = g = b = a = transparent = opaque = 0
        th = self.tile.height
        for y in range(rect.y0, rect.y1):
            pr, pg, pb, pa, pt, po = prefixes[y % th]
            r += self._span(pr, rect.x0, rect.width)
            g += self._span(pg, rect.x0, rect.width)
            b += self._span(pb, rect.x0, rect.width)
            a += self._span(pa, rect.x0, rect.width)
            transparent += self._span(pt, rect.x0, rect.width)
            opaque += self._span(po, rect.x0, rect.width)
        return PixelTotals(
            count, r, g, b, a, transparent, opaque, count - transparent - opaque
        )

    def union_totals(self, rects: Sequence[Rectangle]) -> PixelTotals:
        return interval_union_totals_by_truth(self, rects)

    def rgba_bytes(self) -> bytes:
        return b"".join(
            bytes(c for x in range(self.width) for c in self.pixel(x, y))
            for y in range(self.height)
        )

    def to_bmp(self, bpp: int, *, masks=None, top_down: bool = False, padding_byte: int = 0) -> bytes:
        """Fast BMP builder for large tiled truths.

        Requires the image dimensions to be exact multiples of the tile.  Each
        stored row is built once per tile row and the whole byte block is
        repeated, so building a 2048x2048 fixture does not iterate 4M pixels.
        """

        import struct

        from bmpcatalog.writer import _bits, _row_stride, _scale, _shift

        th, tw = self.tile.height, self.tile.width
        if self.height % th or self.width % tw:
            raise ValueError("TiledTruth.to_bmp needs exact tile multiples")
        stride = _row_stride(self.width, bpp)
        pixel_row_bytes = self.width * (bpp // 8)
        pad = stride - pixel_row_bytes
        period = self.width // tw

        def encode(r, g, b, a) -> bytes:
            if bpp == 24:
                return bytes((b, g, r))
            if bpp == 32 and masks is None:
                return bytes((b, g, r, 0xEE))
            rm, gm, bm, am = masks
            word = (
                _scale(r, _bits(rm)) << _shift(rm)
                | _scale(g, _bits(gm)) << _shift(gm)
                | _scale(b, _bits(bm)) << _shift(bm)
            )
            if am:
                word |= _scale(a, _bits(am)) << _shift(am)
            return word.to_bytes(4, "little")

        tile_rows = []
        for row in self.tile.rows:
            chunk = b"".join(encode(*px) for px in row)
            tile_rows.append(chunk * period + bytes((padding_byte,)) * pad)
        order = range(th) if top_down else range(th - 1, -1, -1)
        block = b"".join(tile_rows[y] for y in order)
        body = block * (self.height // th)

        mask_bytes = b""
        compression = 0
        if masks is not None:
            compression = 3
            rm, gm, bm, am = masks
            mask_bytes = struct.pack("<III", rm, gm, bm)
            if am:
                mask_bytes += struct.pack("<I", am)
        offset = 54 + len(mask_bytes)
        total = offset + len(body)
        header = bytearray()
        header += b"BM"
        header += struct.pack("<I", total)
        header += b"\x00\x00\x00\x00"
        header += struct.pack("<I", offset)
        header += struct.pack("<I", 40)
        header += struct.pack("<i", self.width)
        header += struct.pack("<i", -self.height if top_down else self.height)
        header += struct.pack("<H", 1)
        header += struct.pack("<H", bpp)
        header += struct.pack("<I", compression)
        header += struct.pack("<I", len(body))
        header += struct.pack("<i", 2835)
        header += struct.pack("<i", 2835)
        header += struct.pack("<I", 0)
        header += struct.pack("<I", 0)
        return bytes(header) + mask_bytes + body


def interval_union_totals(
    product, rects: Sequence[Rectangle], step: int = 1
) -> PixelTotals:
    """Independent union truth using per-row interval merging.

    This avoids allocating a full-image mask and avoids the library's tile
    code entirely.  It reads merged horizontal runs through the catalog's
    public rows, so it scales to large images.
    """

    totals = PixelTotals.empty()
    by_row: dict[int, list[tuple[int, int]]] = {}
    for rect in rects:
        for y in range(rect.y0, rect.y1, step):
            by_row.setdefault(y, []).append((rect.x0, rect.x1))
    for y in sorted(by_row):
        merged = _merge(by_row[y])
        for x0, x1 in merged:
            totals = totals + product.totals(Rectangle(x0, y, x1, y + 1))
    return totals


def interval_union_totals_by_truth(truth, rects: Sequence[Rectangle]) -> PixelTotals:
    totals = PixelTotals.empty()
    by_row: dict[int, list[tuple[int, int]]] = {}
    for rect in rects:
        for y in range(rect.y0, rect.y1):
            by_row.setdefault(y, []).append((rect.x0, rect.x1))
    for y in sorted(by_row):
        for x0, x1 in _merge(by_row[y]):
            totals = totals + truth.rect_totals(Rectangle(x0, y, x1, y + 1))
    return totals


def _merge(intervals):
    merged = []
    for x0, x1 in sorted(intervals):
        if merged and x0 <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], x1))
        else:
            merged.append((x0, x1))
    return merged
