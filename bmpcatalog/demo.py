"""Build reproducible demo assets and write a statistics report.

Run:  python -m bmpcatalog.demo [output_dir]

The demo writes real BMP files covering different padding, storage directions,
palettes, non-8-bit masks, transparency, overlapping rectangles and a local
correction.  It then exports revised BMPs and re-opens them to verify that the
pixels actually on disk match the revision, not just the aggregate totals.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Sequence

from .analysis import RegionAnalyzer
from .bmp import BmpPixels
from .geometry import Rectangle
from .writer import build_bmp


def _smooth(width: int, height: int, alpha: bool = False):
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            r = (x * 7 + y * 3 + 5) % 256
            g = (x * 5 + y * 11 + 9) % 256
            b = (x * 13 + y * 2 + 17) % 256
            a = 255
            if alpha:
                a = (x * 17 + y * 29 + 3) % 256
                if (x + 2 * y) % 6 == 0:
                    a = 0
            row.append((r, g, b, a))
        rows.append(tuple(row))
    return tuple(rows)


def _palette(entries: int = 256):
    return tuple(((i * 7) % 256, (i * 31) % 256, (255 - i * 3) % 256) for i in range(entries))


def _write(path: str, data: bytes) -> BmpPixels:
    with open(path, "wb") as handle:
        handle.write(data)
    return BmpPixels.from_path(path)


def _demo_dataset(out_dir: str) -> dict[str, BmpPixels]:
    os.makedirs(out_dir, exist_ok=True)
    products: dict[str, BmpPixels] = {}

    # 24-bit bottom-up, width 13 -> stride 40 with 1 padding byte per row.
    rows = _smooth(13, 7)
    products["rgb24_bottom_up_pad1.bmp"] = _write(
        os.path.join(out_dir, "rgb24_bottom_up_pad1.bmp"),
        build_bmp(13, 7, 24, rows, top_down=False, padding_fn=lambda r, n: bytes([0xA5] * n)),
    )

    # 24-bit top-down, width 10 has no padding.
    products["rgb24_top_down_nopad.bmp"] = _write(
        os.path.join(out_dir, "rgb24_top_down_nopad.bmp"),
        build_bmp(10, 5, 24, _smooth(10, 5), top_down=True),
    )

    # 8-bit palette, width 9 -> stride 12 with 3 padding bytes per row.
    pal = _palette()
    pal_rows = tuple(
        tuple((*pal[(x * 11 + y * 13) % 256], 255) for x in range(9))
        for y in range(6)
    )
    products["pal8_bottom_up_pad3.bmp"] = _write(
        os.path.join(out_dir, "pal8_bottom_up_pad3.bmp"),
        build_bmp(9, 6, 8, pal_rows, palette=pal, padding_fn=lambda r, n: bytes([0x5A] * n)),
    )

    # 32-bit BGRX: the X byte must not become alpha.
    products["bgrx32_bottom_up.bmp"] = _write(
        os.path.join(out_dir, "bgrx32_bottom_up.bmp"),
        build_bmp(8, 4, 32, _smooth(8, 4), top_down=False),
    )

    # 32-bit bitfields with 5-6-5-style channels and a 1-bit alpha mask.
    masks = (0x1F000000, 0x00FC0000, 0x0003F000, 0x00000800)
    bitfield_rows = tuple(
        tuple(
            (
                (x * 37 + y * 13) % 256,
                (x * 19 + y * 41) % 256,
                (x * 7 + y * 23) % 256,
                255 if (x + y) % 3 else 0,
            )
            for x in range(11)
        )
        for y in range(7)
    )
    products["bitfields32_rgb565_alpha1.bmp"] = _write(
        os.path.join(out_dir, "bitfields32_rgb565_alpha1.bmp"),
        build_bmp(11, 7, 32, bitfield_rows, masks=masks, top_down=True),
    )

    # 32-bit RGBA bit-fields (8 bits per channel) with real transparency.
    rgba_masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    products["rgba32_bitfields_alpha8.bmp"] = _write(
        os.path.join(out_dir, "rgba32_bitfields_alpha8.bmp"),
        build_bmp(12, 8, 32, _smooth(12, 8, alpha=True), masks=rgba_masks),
    )
    return products


def _analysis_demo(out_dir: str, products: dict[str, BmpPixels]) -> dict[str, object]:
    product = products["rgba32_bitfields_alpha8.bmp"]
    analyzer = RegionAnalyzer(product, tile_size=4)
    rects = [
        Rectangle(0, 0, 6, 5),
        Rectangle(4, 2, 10, 8),       # overlaps the first
        Rectangle(9, 0, 12, 3),
        Rectangle(2, 6, 12, 8),       # touches the bottom and right edges
    ]
    report = analyzer.report(rects)
    result = report.as_dict()
    result["working_set_bound_bytes"] = analyzer.working_set_bound

    # Local correction: change one rectangle, export, and re-open the actual file.
    correction = Rectangle(3, 1, 7, 4)
    revised = product.revise([(correction, (10, 200, 30, 64))])
    revised_path = os.path.join(out_dir, "rgba32_bitfields_alpha8_revised.bmp")
    revised.save(revised_path)
    reopened = BmpPixels.from_path(revised_path)
    mismatch = []
    for y in range(reopened.height):
        for x in range(reopened.width):
            expected = (10, 200, 30, 64) if (
                correction.x0 <= x < correction.x1 and correction.y0 <= y < correction.y1
            ) else product.pixel_at(x, y)
            if reopened.pixel_at(x, y) != expected:
                mismatch.append((x, y, reopened.pixel_at(x, y), expected))
    revised_report = RegionAnalyzer(reopened, tile_size=4).report(rects + [correction])
    result["revision"] = {
        "corrected_rectangle": correction.to_tuple(),
        "parent_sha256": revised.revision.parent_sha256,
        "revised_sha256": reopened.sha256,
        "reopened_pixels_mismatched": len(mismatch),
        "statistics_after_correction": revised_report.as_dict(),
    }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out_dir = argv[0] if argv else os.path.join(os.getcwd(), "demo_assets", "out")
    products = _demo_dataset(out_dir)

    catalogs = {
        name: product.catalog_record() for name, product in products.items()
    }
    analysis = _analysis_demo(out_dir, products)

    summary = {"catalogs": catalogs, "analysis": analysis}
    print(json.dumps(summary, indent=2, sort_keys=True))
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
