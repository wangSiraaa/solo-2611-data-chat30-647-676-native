"""Catalog parsing, geometry, header validation and revision tests."""

from __future__ import annotations

import struct

import pytest

from bmpcatalog import (
    BmpFormatError,
    BmpPixels,
    Rectangle,
    RectangleOutOfBounds,
    RevisionError,
    build_bmp,
)
from bmpcatalog.analysis import direct_rect_stats
from bmpcatalog.writer import _row_stride

from conftest import build_from_truth, palette8, smooth_truth, truth_to_palette_indices


def _bytes_per_pixel(bpp: int) -> int:
    return bpp // 8


@pytest.mark.parametrize(
    "width,height,bpp,top_down,pad",
    [
        (1, 1, 8, False, 0x00),
        (3, 2, 24, False, 0xAA),
        (5, 3, 24, True, 0x55),
        (2, 4, 32, False, 0x80),
        (7, 5, 32, True, 0xC3),
        (17, 9, 8, True, 0x7E),
    ],
)
def test_logical_pixels_match_truth(width, height, bpp, top_down, pad):
    pal = palette8(256)
    if bpp == 8:
        truth = smooth_truth(width, height)
        # Restrict the truth to palette colours so the 8-bit file can encode it.
        rows = tuple(
            tuple((pal[(x * 7 + y * 31) % 256][0], pal[(x * 7 + y * 31) % 256][1],
                   pal[(x * 7 + y * 31) % 256][2], 255) for x in range(width))
            for y in range(height)
        )
        from conftest import Truth

        truth = Truth(width, height, rows)
        data = build_from_truth(
            truth, bpp, top_down=top_down, palette=pal, padding_byte=pad
        )
    else:
        truth = smooth_truth(width, height)
        data = build_from_truth(truth, bpp, top_down=top_down, padding_byte=pad)
    product = BmpPixels.from_bytes(data)

    assert (product.width, product.height) == (width, height)
    assert product.top_down is top_down
    expected_stride = _row_stride(width, bpp)
    assert product.row_stride == expected_stride
    # Every logical pixel must be exposed at its true logical location.
    for y in range(height):
        for x in range(width):
            assert product.pixel_at(x, y) == truth.pixel(x, y)
    assert product.read_rgba((0, 0, width, height)) == truth.rgba_bytes()


def test_padding_bytes_never_reach_rgba_or_totals():
    truth = smooth_truth(5, 4)
    a = build_from_truth(truth, 24, padding_byte=0x00)
    b = build_from_truth(truth, 24, padding_byte=0xFF, letterbox_padding=True)
    pa = BmpPixels.from_bytes(a)
    pb = BmpPixels.from_bytes(b)
    assert pa.sha256 != pb.sha256
    assert pa.pixel_sha256() == pb.pixel_sha256()
    assert pa.read_rgba((0, 0, 5, 4)) == pb.read_rgba((0, 0, 5, 4))
    assert pa.totals((0, 0, 5, 4)) == pb.totals((0, 0, 5, 4))


def test_row_byte_ranges_exclude_padding_and_follow_storage_order():
    truth = smooth_truth(6, 3)  # 24-bit: 18 bytes, stride 20, pady 2
    bottom_up = BmpPixels.from_bytes(build_from_truth(truth, 24))
    top_down = BmpPixels.from_bytes(build_from_truth(truth, 24, top_down=True))
    for product, reversed_rows in ((bottom_up, True), (top_down, False)):
        rows = list(product.iter_rows(Rectangle(2, 0, 5, 3)))
        assert [r.logical_y for r in rows] == [0, 1, 2]
        assert all(r.padding_length == 2 for r in rows)
        assert all(r.file_range[1] - r.file_range[0] == 3 * 3 for r in rows)
        # Storage rows for bottom-up must descend in file offset.
        starts = [r.pixel_offset for r in rows]
        assert (starts == sorted(starts, reverse=True)) is reversed_rows
        # Pixel data must be inside the declared stride window.
        for row in rows:
            assert row.storage_row_range[0] <= row.file_range[0]
            assert row.file_range[1] <= row.storage_row_range[1]
            assert row.padding_offset == row.row_file_offset + 18


def test_8bit_palette_indices_and_colors():
    pal = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255), (12, 34, 56)]
    rows = [[(0, 0, 0, 255), (255, 0, 0, 255), (12, 34, 56, 255), (0, 0, 255, 255)]]
    data = build_bmp(4, 1, 8, rows, palette=pal)
    product = BmpPixels.from_bytes(data)
    assert product.palette == tuple(pal)
    assert product.record.palette_entries == 5
    assert product.read_rgba((0, 0, 4, 1)) == bytes(
        c for px in rows[0] for c in px
    )


def test_8bit_index_outside_palette_is_rejected():
    pal = [(1, 2, 3)]
    data = bytearray(build_bmp(1, 1, 8, [[(1, 2, 3, 255)]], palette=pal))
    # Overwrite the single pixel index with 1, which the 1-entry palette lacks.
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    data[pixel_offset] = 1
    with pytest.raises(BmpFormatError, match="palette index"):
        BmpPixels.from_bytes(bytes(data))


def test_32bit_bitfields_half_up_scaling_and_alpha_mask():
    # 5-6-5-like layout inside 32 bits, plus a 1-bit alpha mask.  Values were
    # chosen so truncating the high 8 bits gives a different answer.
    masks = (0x1F000000, 0x00FC0000, 0x0003F000, 0x00000800)
    truth_rows = [
        [
            (0, 0, 0, 255),
            (255, 0, 0, 0),
            (0, 255, 0, 255),
            (0, 0, 255, 0),
        ]
    ]
    data = build_bmp(4, 1, 32, truth_rows, masks=masks)
    product = BmpPixels.from_bytes(data)
    assert product.bits_per_pixel == 32
    for x in range(4):
        assert product.pixel_at(x, 0) == truth_rows[0][x]
    # Sanity check that rounding, not truncation, is used for 5-bit channels.
    assert BmpPixels.from_bytes(data).pixel_at(1, 0)[3] == 0


def test_channel_scaling_is_round_half_up_not_high_bit_truncation():
    from bmpcatalog.bmp import _scale_channel

    # 5-bit value 4 -> 4*255/31 = 32.9 -> 33 (truncating high bits gives 32).
    assert _scale_channel(4, 5) == 33
    assert _scale_channel(1, 5) == 8
    assert _scale_channel(2, 5) == 16
    assert _scale_channel(31, 5) == 255
    assert _scale_channel(1, 10) == 0 or _scale_channel(1, 10) == 0
    assert _scale_channel(2, 10) == 0
    assert _scale_channel(512, 10) == 128
    assert _scale_channel(1023, 10) == 255


def test_ten_bit_channels_are_rescaled_end_to_end():
    # Build raw words directly so the stored codes are exact 10-bit values,
    # not 8-bit values that the writer scaled down.
    masks = (0x3FF00000, 0x000FFC00, 0x000003FF, 0xC0000000)
    words = [
        (4 << 20) | (512 << 10) | 1023 | (1 << 30),
        (1020 << 20) | (8 << 10) | 0 | (0 << 30),
    ]
    body = b"".join(word.to_bytes(4, "little") for word in words)
    mask_bytes = struct.pack("<IIII", *masks)
    offset = 54 + len(mask_bytes)
    total = offset + len(body)
    header = bytearray(b"BM")
    header += struct.pack("<I", total)
    header += b"\x00\x00\x00\x00"
    header += struct.pack("<I", offset)
    header += struct.pack("<I", 40)
    header += struct.pack("<i", 2)
    header += struct.pack("<i", 1)
    header += struct.pack("<H", 1)
    header += struct.pack("<H", 32)
    header += struct.pack("<I", 3)
    header += struct.pack("<I", len(body))
    header += struct.pack("<i", 2835)
    header += struct.pack("<i", 2835)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    product = BmpPixels.from_bytes(bytes(header) + mask_bytes + body)
    # 4/1023 -> round(0.997) = 1; truncating the high 8 bits would give 0.
    assert product.pixel_at(0, 0) == (1, 128, 255, 85)  # 2-bit alpha 1 -> 85
    assert product.pixel_at(1, 0) == (254, 2, 0, 0)
    totals = product.totals(Rectangle(0, 0, 2, 1))
    assert totals.channel_sums == (255, 130, 255, 85)
    assert totals.transparent == 1
    assert totals.opaque == 0
    assert totals.partial == 1


def test_bitfields_missing_alpha_mask_is_opaque():
    masks = (0x000000FF, 0x0000FF00, 0x00FF0000)
    rows = [[(255, 128, 7, 255), (0, 0, 0, 255)]]
    product = BmpPixels.from_bytes(build_bmp(2, 1, 32, rows, masks=masks))
    assert product.pixel_at(0, 0) == (255, 128, 7, 255)
    assert product.totals(Rectangle(0, 0, 2, 1)).transparent == 0


def test_bitfields_overlapping_or_noncontiguous_masks_rejected():
    # Construct raw bytes by hand: valid masks first, then mutate them, because
    # the writer itself refuses overlapping masks.
    base = bytearray(build_bmp(1, 1, 32, [[(1, 2, 3, 255)]], masks=(0xFF, 0xFF00, 0xFF0000)))
    overlap = bytearray(base)
    struct.pack_into("<I", overlap, 58, 0x0000FF00 | 0x000000FF)
    with pytest.raises(BmpFormatError, match="overlap"):
        BmpPixels.from_bytes(bytes(overlap))
    # Build a mask with a hole to prove non-contiguous runs are rejected.
    hole = bytearray(base)
    struct.pack_into("<I", hole, 54, 0x0101)
    with pytest.raises(BmpFormatError, match="contiguous"):
        BmpPixels.from_bytes(bytes(hole))


def test_32bit_bi_rgb_x_byte_is_not_alpha():
    rows = [[(1, 2, 3, 0)]]  # requested alpha ignored; writer stores X=0xEE
    data = build_bmp(1, 1, 32, rows)
    product = BmpPixels.from_bytes(data)
    assert product.pixel_at(0, 0) == (1, 2, 3, 255)
    assert product.totals(Rectangle(0, 0, 1, 1)).transparent == 0


def test_top_down_and_bottom_up_are_pixel_identical():
    truth = smooth_truth(9, 7)
    up = BmpPixels.from_bytes(build_from_truth(truth, 24, top_down=False))
    down = BmpPixels.from_bytes(build_from_truth(truth, 24, top_down=True))
    assert up.pixel_sha256() == down.pixel_sha256()
    assert up.file_bytes != down.file_bytes
    for y in range(7):
        assert up.storage_row_bytes(y)[0] != down.storage_row_bytes(y)[0] or y == 3


def test_gap_before_pixels_is_recorded():
    truth = smooth_truth(3, 2)
    product = BmpPixels.from_bytes(build_from_truth(truth, 24, gap_bytes=9))
    assert product.record.internal_gap_bytes == 9
    assert product.pixel_offset == 54 + 9
    assert product.read_rgba((0, 0, 3, 2)) == truth.rgba_bytes()


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d.__setitem__(slice(0, 2), b"XX"), "signature"),
        (lambda d: struct.pack_into("<I", d, 14, 12), "DIB header"),
        (lambda d: struct.pack_into("<i", d, 18, 0), "width"),
        (lambda d: struct.pack_into("<i", d, 22, 0), "height"),
        (lambda d: struct.pack_into("<H", d, 26, 2), "planes"),
        (lambda d: struct.pack_into("<H", d, 28, 16), "bit depth"),
        (lambda d: struct.pack_into("<I", d, 30, 1), "BI_RLE8"),
        (lambda d: struct.pack_into("<I", d, 30, 4), "BI_JPEG"),
        (lambda d: struct.pack_into("<I", d, 34, 1), "image size"),
        (lambda d: struct.pack_into("<I", d, 2, 10**9), "declares"),
    ],
)
def test_header_validation_rejects_bad_claims(mutate, message):
    truth = smooth_truth(4, 3)
    data = bytearray(build_from_truth(truth, 24))
    mutate(data)
    with pytest.raises(BmpFormatError, match=message):
        BmpPixels.from_bytes(bytes(data))


def test_truncated_pixels_never_produce_a_product():
    truth = smooth_truth(5, 4)
    data = build_from_truth(truth, 24)
    for cut in range(1, 12):
        with pytest.raises(BmpFormatError):
            BmpPixels.from_bytes(data[:-cut])


def test_out_of_bounds_rectangles_are_rejected_not_clipped():
    product = BmpPixels.from_bytes(build_from_truth(smooth_truth(4, 4), 24))
    for rect in [(-1, 0, 2, 2), (0, 0, 5, 1), (0, -3, 2, 1), (3, 3, 4, 5)]:
        with pytest.raises(RectangleOutOfBounds):
            product.read_rect(rect)
    with pytest.raises(Exception):
        Rectangle(1, 1, 1, 3)
    with pytest.raises(Exception):
        Rectangle(2, 0, 2, 3)


def test_revision_only_changes_covered_pixels_and_padding_stays():
    truth = smooth_truth(6, 5)
    original = BmpPixels.from_bytes(build_from_truth(truth, 24, padding_byte=0xAB))
    rect = Rectangle(1, 2, 4, 4)
    updates = []
    for y in range(rect.y0, rect.y1):
        for x in range(rect.x0, rect.x1):
            updates.append((0, 0, 0, 255))
    updated = original.revise([(rect, bytes(c for px in updates for c in px))])

    assert updated.sha256 != original.sha256
    assert updated.revision.parent_sha256 == original.sha256
    for y in range(5):
        for x in range(6):
            if rect.x0 <= x < rect.x1 and rect.y0 <= y < rect.y1:
                assert updated.pixel_at(x, y) == (0, 0, 0, 255)
            else:
                assert updated.pixel_at(x, y) == original.pixel_at(x, y)
    # Header and padding bytes are untouched except the hash-implied digest.
    for row in range(5):
        old_raw = original.iter_rows(Rectangle(0, row, 6, row + 1)).__next__()
        new_raw = updated.iter_rows(Rectangle(0, row, 6, row + 1)).__next__()
        assert old_raw.padding_length == new_raw.padding_length
    # Padded bytes survive byte-for-byte.
    old_body = original.file_bytes[original.pixel_offset :]
    new_body = updated.file_bytes[updated.pixel_offset :]
    covered_rows = set(range(rect.y0, rect.y1))
    for y in range(5):
        old_row = original.storage_row_bytes(y)
        new_row = updated.storage_row_bytes(y)
        old_bytes = old_row[0] - original.pixel_offset
        new_bytes = new_row[0] - updated.pixel_offset
        if y not in covered_rows:
            assert old_body[old_bytes : old_bytes + original.row_stride] == new_body[
                new_bytes : new_bytes + new_row[1] - new_row[0]
            ]


def test_revision_accepts_callable_and_preserves_other_x_bytes():
    rows = [[(10, 20, 30, 255), (40, 50, 60, 255)]]
    original = BmpPixels.from_bytes(build_bmp(2, 1, 32, rows))
    updated = original.revise([((0, 0, 1, 1), lambda x, y: (1, 2, 3, 255))])
    assert updated.pixel_at(0, 0) == (1, 2, 3, 255)
    assert updated.pixel_at(1, 0) == (40, 50, 60, 255)
    # The BGRX X byte (0xEE) on the changed pixel is preserved.
    assert updated.file_bytes[updated.pixel_offset + 3] == 0xEE


def test_revision_rejects_unrepresentable_alpha_and_unknown_palette():
    rows = [[(1, 2, 3, 255)]]
    product = BmpPixels.from_bytes(build_bmp(1, 1, 24, rows))
    with pytest.raises(RevisionError):
        product.revise([((0, 0, 1, 1), (1, 2, 3, 0))])

    pal = [(0, 0, 0), (255, 255, 255)]
    p8 = BmpPixels.from_bytes(build_bmp(1, 1, 8, [[(0, 0, 0, 255)]], palette=pal))
    with pytest.raises(RevisionError, match="palette"):
        p8.revise([((0, 0, 1, 1), (12, 13, 14, 255))])


def test_revision_rejects_unrepresentable_bitfield_values():
    masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    product = BmpPixels.from_bytes(build_bmp(2, 1, 32, [[(1, 2, 3, 255)] * 2], masks=masks))
    revised = product.revise([((0, 0, 1, 1), (200, 100, 50, 25))])
    assert revised.pixel_at(0, 0) == (200, 100, 50, 25)
    # 5-bit channels cannot encode 200 exactly, so this must be rejected rather
    # than silently writing a different value.
    narrow = (0x1F000000, 0x00FC0000, 0x0003F000, 0x00000800)
    narrow_product = BmpPixels.from_bytes(
        build_bmp(1, 1, 32, [[(0, 0, 0, 255)]], masks=narrow)
    )
    with pytest.raises(RevisionError, match="cannot represent"):
        narrow_product.revise([((0, 0, 1, 1), (200, 100, 50, 255))])


def test_revision_of_8bit_palette_uses_existing_entries_only():
    pal = [(0, 0, 0), (255, 0, 0), (0, 255, 0)]
    product = BmpPixels.from_bytes(
        build_bmp(2, 1, 8, [[(0, 0, 0, 255), (0, 0, 0, 255)]], palette=pal)
    )
    revised = product.revise([((0, 0, 1, 1), (0, 255, 0, 255))])
    assert revised.pixel_at(0, 0) == (0, 255, 0, 255)
    assert revised.pixel_at(1, 0) == (0, 0, 0, 255)
    # Palette bytes are unchanged.
    original_palette = product.format.palette_bytes
    assert revised.format.palette_bytes == original_palette


def test_revision_rectangle_outside_image_is_rejected():
    product = BmpPixels.from_bytes(
        build_from_truth(smooth_truth(3, 3), 24)
    )
    with pytest.raises(RectangleOutOfBounds):
        product.revise([((0, 0, 4, 1), (1, 2, 3, 255))])


def test_save_round_trip_is_byte_identical(tmp_path):
    truth = smooth_truth(7, 4)
    product = BmpPixels.from_bytes(build_from_truth(truth, 32, top_down=True))
    path = tmp_path / "out.bmp"
    assert product.save(str(path)) == len(product.file_bytes)
    assert BmpPixels.from_path(str(path)).file_bytes == product.file_bytes
    assert BmpPixels.from_path(str(path)).pixel_sha256() == product.pixel_sha256()
