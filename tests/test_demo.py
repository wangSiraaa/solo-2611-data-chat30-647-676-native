"""End-to-end demo and export verification tests."""

from __future__ import annotations

import json
import os

import pytest

from bmpcatalog import BmpPixels
from bmpcatalog.demo import _analysis_demo, _demo_dataset


def test_demo_assets_parse_and_export_verified(tmp_path):
    products = _demo_dataset(str(tmp_path))
    names = set(products)
    assert {
        "rgb24_bottom_up_pad1.bmp",
        "rgb24_top_down_nopad.bmp",
        "pal8_bottom_up_pad3.bmp",
        "bgrx32_bottom_up.bmp",
        "bitfields32_rgb565_alpha1.bmp",
        "rgba32_bitfields_alpha8.bmp",
    } <= names
    # Every demo asset is a real BMP with a successful catalog record.
    for name, product in products.items():
        assert os.path.getsize(os.path.join(tmp_path, name)) == product.file_size
        assert product.record.file_bytes == product.file_size
        assert product.sha256
    # Different padding/direction/palette cases really differ in file layout.
    assert products["rgb24_bottom_up_pad1.bmp"].record.padding_per_row == 1
    assert products["pal8_bottom_up_pad3.bmp"].record.padding_per_row == 3
    assert products["rgb24_top_down_nopad.bmp"].record.top_down is True
    assert products["pal8_bottom_up_pad3.bmp"].record.palette_entries == 256
    assert products["bgrx32_bottom_up.bmp"].record.masks["alpha"] is None
    assert products["rgba32_bitfields_alpha8.bmp"].record.masks["alpha"] == "0xff000000"


def test_demo_analysis_correction_reopens_real_pixels(tmp_path):
    products = _demo_dataset(str(tmp_path))
    analysis = _analysis_demo(str(tmp_path), products)
    revision = analysis["revision"]
    assert revision["reopened_pixels_mismatched"] == 0
    # The correction truly changed statistics in the overlapping rectangles.
    after = revision["statistics_after_correction"]["regions"]
    before = analysis["regions"]
    changed = [a["sums_rgba"] != b["sums_rgba"] for a, b in zip(after, before)]
    assert any(changed)
    # Union pixel count is the deduplicated area, not the sum of rectangle areas.
    union = analysis["union"]
    assert union["pixels"] < sum(region["pixels"] for region in analysis["regions"])


def test_revised_demo_file_is_independently_reopenable(tmp_path):
    products = _demo_dataset(str(tmp_path))
    _analysis_demo(str(tmp_path), products)
    path = os.path.join(str(tmp_path), "rgba32_bitfields_alpha8_revised.bmp")
    reopened = BmpPixels.from_path(path)
    original = products["rgba32_bitfields_alpha8.bmp"]
    assert reopened.sha256 != original.sha256
    # Outside the corrected rectangle, every pixel matches the original file.
    for y in range(reopened.height):
        for x in range(reopened.width):
            if 3 <= x < 7 and 1 <= y < 4:
                continue
            assert reopened.pixel_at(x, y) == original.pixel_at(x, y)
