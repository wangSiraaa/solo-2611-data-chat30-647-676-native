"""Scale demo: write a 2048x2048 BMP and run 1000 region statistics.

Run:  python -m bmpcatalog.benchmark [output_dir]

The generated image uses a repeating tile so the file can be built quickly, but
it is a normal BMP on disk and is re-opened through the same catalog path used
in production.  Memory is measured only around the statistics phase, because
the source product itself is an immutable byte buffer.
"""

from __future__ import annotations

import os
import random
import sys
import time
import tracemalloc

from .analysis import RegionAnalyzer
from .bmp import BmpPixels
from .geometry import Rectangle
from .writer import build_bmp


def _pixel(x: int, y: int) -> tuple[int, int, int, int]:
    tile = 64
    tx, ty = x % tile, y % tile
    r = (tx * 7 + ty * 3 + 5) % 256
    g = (tx * 5 + ty * 11 + 9) % 256
    b = (tx * 13 + ty * 2 + 17) % 256
    a = (tx * 17 + ty * 29 + 3) % 256
    if (tx + 2 * ty) % 6 == 0:
        a = 0
    return (r, g, b, a)


def _rectangles(width: int, height: int, count: int = 1000, seed: int = 20241118):
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        x0 = rng.randrange(width)
        y0 = rng.randrange(height)
        out.append(
            Rectangle(x0, y0, rng.randrange(x0 + 1, width + 1), rng.randrange(y0 + 1, height + 1))
        )
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out_dir = argv[0] if argv else os.path.join(os.getcwd(), "demo_assets", "scale")
    os.makedirs(out_dir, exist_ok=True)
    width = height = 2048
    path = os.path.join(out_dir, "scale_2048x2048_rgba.bmp")
    if not os.path.exists(path):
        # Build one 64x64 tile, then repeat it horizontally and vertically to
        # form a normal 2048x2048 bottom-up BMP.
        masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
        tile = build_bmp(
            64, 64, 32, [[_pixel(x, y) for x in range(64)] for y in range(64)],
            masks=masks, top_down=True,
        )
        tile_pixels = tile[70:]
        row_stride = 64 * 4
        reps = width // 64
        body = bytearray()
        vertical = height // 64
        for tile_row in range(64):
            row = tile_pixels[tile_row * row_stride : (tile_row + 1) * row_stride]
            body.extend(row * reps)
        body = bytes(body) * vertical
        import struct

        header = bytearray(b"BM")
        header += struct.pack("<I", 70 + len(body))
        header += b"\x00\x00\x00\x00"
        header += struct.pack("<I", 70)
        header += struct.pack("<I", 40)
        header += struct.pack("<i", width)
        header += struct.pack("<i", height)
        header += struct.pack("<H", 1)
        header += struct.pack("<H", 32)
        header += struct.pack("<I", 3)
        header += struct.pack("<I", len(body))
        header += struct.pack("<i", 2835)
        header += struct.pack("<i", 2835)
        header += struct.pack("<I", 0)
        header += struct.pack("<I", 0)
        header += struct.pack("<4I", *masks)
        with open(path, "wb") as handle:
            handle.write(bytes(header) + body)

    product = BmpPixels.from_path(path)
    rects = _rectangles(width, height)
    # Time without tracing, then measure allocations in a second identical run.
    started = time.perf_counter()
    analyzer = RegionAnalyzer(product, tile_size=64)
    report = analyzer.report(rects)
    elapsed = time.perf_counter() - started

    tracemalloc.start()
    memory_analyzer = RegionAnalyzer(product, tile_size=64)
    memory_analyzer.report(rects)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"file: {path} ({product.file_size} bytes)")
    print(f"rectangles: {len(report.regions)}")
    print(f"union pixels: {report.union.count} (naive sum: {sum(r.area for r in rects)})")
    print(f"statistics time: {elapsed:.2f}s")
    print(f"peak traced memory during statistics: {peak / 1024:.0f} KiB")
    print(f"declared working-set bound: {analyzer.working_set_bound / 1024:.0f} KiB")
    print("read counters:", report.read_stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
