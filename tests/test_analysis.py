"""Region aggregation, union exactness, revision binding and workload tests."""

from __future__ import annotations

import tracemalloc

import pytest

from bmpcatalog import (
    BmpPixels,
    Rectangle,
    RectangleOutOfBounds,
    RegionAnalyzer,
    direct_rect_stats,
    direct_union_stats,
)
from bmpcatalog.bmp import PixelTotals

from conftest import (
    build_from_truth,
    palette8,
    rectangles_for,
    smooth_truth,
)


def _random_truth_8bit(width, height):
    from conftest import Truth

    pal = palette8(256)
    rows = tuple(
        tuple((pal[(x * 13 + y * 7) % 256][0], pal[(x * 13 + y * 7) % 256][1],
               pal[(x * 13 + y * 7) % 256][2], 255) for x in range(width))
        for y in range(height)
    )
    return Truth(width, height, rows), pal


@pytest.mark.parametrize("bpp", [8, 24, 32])
def test_each_rectangle_matches_independent_truth(bpp):
    height, width = 13, 11
    masks = None
    if bpp == 8:
        truth, pal = _random_truth_8bit(width, height)
        data = build_from_truth(truth, 8, palette=pal, padding_byte=0x33)
    elif bpp == 24:
        truth = smooth_truth(width, height)
        data = build_from_truth(truth, bpp, padding_byte=0x77)
    else:
        truth = smooth_truth(width, height, alpha=True)
        masks = (0x0000FF, 0x00FF00, 0xFF0000, 0xFF000000)
        data = build_from_truth(truth, bpp, masks=masks, padding_byte=0x77)
    product = BmpPixels.from_bytes(data)
    analyzer = RegionAnalyzer(product, tile_size=4)
    rects = [
        Rectangle(0, 0, width, height),
        Rectangle(0, 0, 1, 1),
        Rectangle(width - 1, height - 1, width, height),
        Rectangle(2, 1, 9, 6),
        Rectangle(0, 5, 4, 13),
        Rectangle(7, 0, 11, 4),
        Rectangle(3, 3, 8, 9),
    ]
    for rect in rects:
        got = analyzer.rect_stats(rect)
        expected = truth.rect_totals(rect)
        assert got.totals == expected, rect
        assert got.count == rect.area
        assert got.sums == expected.channel_sums
        assert got.means == expected.means
        assert got.transparent == expected.transparent
        # Direct reader path agrees with the tiled path.
        assert direct_rect_stats(product, rect).totals == expected


def test_block_rect_stats_cross_tiles_and_touch_image_edges():
    truth = smooth_truth(10, 9)
    product = BmpPixels.from_bytes(build_from_truth(truth, 24))
    analyzer = RegionAnalyzer(product, tile_size=4)
    rect = Rectangle(1, 2, 10, 9)  # touches right and bottom edges
    assert analyzer.rect_stats(rect).totals == truth.rect_totals(rect)
    block = product.read_rect(rect)
    for y in range(rect.y0, rect.y1):
        assert block.row(y - rect.y0) == bytes(
            c for x in range(rect.x0, rect.x1) for c in truth.pixel(x, y)
        )
    for y in range(rect.y0, rect.y1):
        for x in range(rect.x0, rect.x1):
            assert block.pixel_at(x, y) == truth.pixel(x, y)


def test_out_of_bounds_analysis_is_rejected_not_clipped():
    product = BmpPixels.from_bytes(build_from_truth(smooth_truth(6, 6), 24))
    analyzer = RegionAnalyzer(product, tile_size=4)
    with pytest.raises(RectangleOutOfBounds):
        analyzer.rect_stats(Rectangle(0, 0, 7, 1))
    with pytest.raises(RectangleOutOfBounds):
        analyzer.union_stats([Rectangle(0, 0, 2, 2), Rectangle(5, 5, 7, 6)])


def test_empty_rectangle_is_rejected():
    from bmpcatalog import RectangleError

    product = BmpPixels.from_bytes(build_from_truth(smooth_truth(3, 3), 24))
    analyzer = RegionAnalyzer(product)
    with pytest.raises(RectangleError):
        Rectangle(1, 1, 1, 3)
    with pytest.raises(RectangleError):
        analyzer.rect_stats((1, 1, 1, 3))
    # An empty union is valid and has zero pixels; empty rectangles are not.
    empty_union = analyzer.union_stats([])
    assert empty_union.count == 0
    assert empty_union.totals == PixelTotals.empty()


def test_union_counts_overlapping_pixels_once():
    truth = smooth_truth(12, 10)
    product = BmpPixels.from_bytes(build_from_truth(truth, 24))
    analyzer = RegionAnalyzer(product, tile_size=3)
    rects = [
        Rectangle(0, 0, 6, 5),
        Rectangle(3, 2, 9, 8),  # overlaps the first
        Rectangle(3, 2, 9, 8),  # duplicate rectangle
        Rectangle(0, 7, 12, 10),
        Rectangle(8, 0, 12, 4),
    ]
    got = analyzer.union_stats(rects)
    expected = truth.union_totals(rects)
    assert got.totals == expected
    assert got.count == expected.count
    # Naively summing per-rectangle counts must overcount, proving the union is
    # not computed by adding rectangle statistics.
    naive = sum(r.area for r in rects)
    assert got.count < naive
    per_rect_sum = PixelTotals.empty()
    for rect in rects:
        per_rect_sum = per_rect_sum + analyzer.rect_stats(rect).totals
    assert per_rect_sum.count > got.count
    assert per_rect_sum.r > got.totals.r


def test_union_disjoint_and_adjacent_rectangles():
    truth = smooth_truth(8, 8, alpha=True)
    masks = (0x0000FF, 0x00FF00, 0xFF0000, 0xFF000000)
    product = BmpPixels.from_bytes(
        build_from_truth(truth, 32, masks=masks, padding_byte=0x11)
    )
    analyzer = RegionAnalyzer(product, tile_size=5)
    disjoint = [
        Rectangle(0, 0, 2, 2),
        Rectangle(5, 1, 8, 3),
        Rectangle(2, 5, 4, 7),
    ]
    got = analyzer.union_stats(disjoint)
    assert got.totals == truth.union_totals(disjoint)
    assert got.totals == sum(
        (analyzer.rect_stats(r).totals for r in disjoint), PixelTotals.empty()
    )
    adjacent = [Rectangle(0, 0, 4, 4), Rectangle(4, 0, 8, 4), Rectangle(0, 4, 8, 8)]
    assert analyzer.union_stats(adjacent).totals == truth.union_totals(adjacent)


def test_union_matches_direct_reference_for_random_overlaps():
    truth = smooth_truth(17, 15)
    product = BmpPixels.from_bytes(build_from_truth(truth, 24, top_down=True))
    rects = rectangles_for(17, 15, count=25, seed=7)
    analyzer = RegionAnalyzer(product, tile_size=4)
    assert analyzer.union_stats(rects).totals == truth.union_totals(rects)
    # The naive per-rectangle sum is only valid when nothing overlaps.
    assert analyzer.union_stats(rects).totals == direct_union_stats(product, rects).totals


def test_report_binds_to_product_and_preserves_old_results_after_revision():
    truth = smooth_truth(9, 7, alpha=True)
    masks = (0x0000FF, 0x00FF00, 0xFF0000, 0xFF000000)
    original = BmpPixels.from_bytes(build_from_truth(truth, 32, masks=masks))
    rects = [Rectangle(0, 0, 5, 4), Rectangle(3, 2, 9, 7)]
    old_analyzer = RegionAnalyzer(original, tile_size=4)
    old_report = old_analyzer.report(rects)
    old_expected = [truth.rect_totals(r) for r in rects]
    assert [s.totals for s in old_report.regions] == old_expected
    assert old_report.bound_to(original)

    correction = Rectangle(2, 1, 6, 5)
    new_values = bytes((9, 8, 7, 128)) * correction.area
    revised = original.revise([(correction, new_values)])
    new_report = RegionAnalyzer(revised, tile_size=4).report(rects)

    # The old report still describes the old product and old pixels.
    assert old_report.bound_to(original)
    assert not old_report.bound_to(revised)
    assert old_report.source_sha256 == original.sha256
    assert [s.totals for s in old_report.regions] == old_expected

    # The new report consumes the corrected pixels.
    for stats in new_report.regions:
        expected = manual_totals_with_correction(truth, stats.rectangle, correction)
        assert stats.totals == expected
    assert new_report.bound_to(revised)
    assert new_report.source_revision["parent_sha256"] == original.sha256


def manual_totals_with_correction(truth, rect, correction):
    values = []
    for y in range(rect.y0, rect.y1):
        for x in range(rect.x0, rect.x1):
            if (
                correction.x0 <= x < correction.x1
                and correction.y0 <= y < correction.y1
            ):
                values.append((9, 8, 7, 128))
            else:
                values.append(truth.pixel(x, y))
    return PixelTotals(
        len(values),
        sum(v[0] for v in values),
        sum(v[1] for v in values),
        sum(v[2] for v in values),
        sum(v[3] for v in values),
        sum(1 for v in values if v[3] == 0),
        sum(1 for v in values if v[3] == 255),
        sum(1 for v in values if 0 < v[3] < 255),
    )


def test_large_workload_2048x2048_1000_rects_under_4mib():
    # A repeating 64x64 tile keeps the fixture and analytic truth cheap, while
    # the file is still a full 2048x2048 image with 1000 random rectangles.
    from conftest import TiledTruth, interval_union_totals

    width = height = 2048
    truth = TiledTruth(width, height, smooth_truth(64, 64, alpha=True))
    masks = (0x0000FF, 0x00FF00, 0xFF0000, 0xFF000000)
    data = truth.to_bmp(32, masks=masks, top_down=False, padding_byte=0x00)
    product = BmpPixels.from_bytes(data)
    rects = rectangles_for(width, height, count=1000, seed=99)

    tracemalloc.start()
    analyzer = RegionAnalyzer(product, tile_size=64)
    report = analyzer.report(rects, include_union=True)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(report.regions) == 1000
    # Spot-check exact per-rectangle totals against the analytic model.
    for stats in report.regions[::97]:
        assert stats.totals == truth.rect_totals(stats.rectangle)
    # Union is exact (independent per-row interval merge), not the naive sum.
    expected_union = interval_union_totals(product, rects)
    assert report.union.totals == expected_union
    naive = sum(r.area for r in rects)
    assert report.union.count < naive
    assert analyzer.working_set_bound <= 4 * 1024 * 1024
    # Explicit budget: default cache cap (4096 entries x 256B) + one tile row.
    assert analyzer.working_set_bound == 4096 * 256 + 64 * 4
    # Peak Python allocations during the report stay inside 4 MiB.  The source
    # image itself was allocated before measurement.
    assert peak <= 4 * 1024 * 1024, peak


def test_tile_cache_reduces_repeated_reads():
    truth = smooth_truth(40, 40)
    product = BmpPixels.from_bytes(build_from_truth(truth, 24))
    analyzer = RegionAnalyzer(product, tile_size=8)
    analyzer.reset_counters()
    for _ in range(20):
        analyzer.rect_stats(Rectangle(0, 0, 40, 40))
    counters = analyzer.counters()
    # A full-image rectangle touches 25 tiles; repeated passes reuse them.
    assert counters["whole_tile_reads"] == 25
    assert counters["cache_hits"] == 20 * 25 - 25


def test_rect_reading_does_not_copy_full_image_per_rect():
    truth = smooth_truth(200, 200)
    product = BmpPixels.from_bytes(build_from_truth(truth, 24))
    analyzer = RegionAnalyzer(product, tile_size=32)
    tracemalloc.start()
    for rect in [Rectangle(0, 0, 5, 5), Rectangle(190, 190, 200, 200), Rectangle(50, 50, 60, 60)] * 50:
        block = product.read_rect(rect)
        assert len(block.rgba) == rect.area * 4
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # Each read is bounded by one small rectangle, not by the 200x200 image.
    assert peak < 200 * 200 * 4
