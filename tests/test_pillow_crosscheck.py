"""Cross-checks against Pillow, used only as an independent decoder.

Pillow is optional for the production chain: all required checks live in the
truth-matrix tests.  These tests prove that exported BMPs are real BMP files
that another mature library decodes to the same logical top-left pixels.
"""

from __future__ import annotations

import io
import struct

import pytest

from bmpcatalog import BmpPixels, build_bmp

from conftest import build_from_truth, palette8, smooth_truth

Image = pytest.importorskip("PIL.Image")


def _pillow_rgba(data: bytes) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        rgba = image.convert("RGBA")
        width, height = rgba.size
        pixels = list(rgba.getdata())
    return width, height, pixels


def _our_pixels(product: BmpPixels) -> list[tuple[int, int, int, int]]:
    return [
        product.pixel_at(x, y)
        for y in range(product.height)
        for x in range(product.width)
    ]


@pytest.mark.parametrize("top_down", [False, True])
def test_exported_24bit_matches_pillow(top_down):
    truth = smooth_truth(7, 5)
    data = build_from_truth(truth, 24, top_down=top_down, padding_byte=0x00)
    product = BmpPixels.from_bytes(data)
    width, height, pixels = _pillow_rgba(data)
    assert (width, height) == (7, 5)
    assert pixels == _our_pixels(product) == [px for row in truth.rows for px in row]


def test_exported_8bit_palette_matches_pillow():
    pal = palette8(256)
    rows = tuple(
        tuple((pal[(x * 5 + y * 9) % 256] + (255,)) for x in range(6))
        for y in range(4)
    )
    from conftest import Truth

    truth = Truth(6, 4, rows)
    data = build_from_truth(truth, 8, palette=pal, padding_byte=0x00)
    product = BmpPixels.from_bytes(data)
    width, height, pixels = _pillow_rgba(data)
    assert pixels == _our_pixels(product)
    assert pixels == [px for row in truth.rows for px in row]


def test_exported_32bit_bgra_bitfields_matches_pillow_rgb():
    # Pillow only reads three masks for a 40-byte BITMAPINFOHEADER, so it
    # reports alpha=255.  Our optional fourth mask word is an extension that
    # carries real alpha; RGB must still agree with Pillow.
    truth = smooth_truth(5, 3, alpha=True)
    masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    data = build_from_truth(truth, 32, masks=masks, padding_byte=0x00)
    product = BmpPixels.from_bytes(data)
    width, height, pixels = _pillow_rgba(data)
    assert pixels == [px[:3] + (255,) for px in _our_pixels(product)]
    assert _our_pixels(product) == [px for row in truth.rows for px in row]


def test_exported_32bit_bgrx_is_read_as_rgb_by_pillow():
    truth = smooth_truth(4, 3)
    data = build_from_truth(truth, 32, padding_byte=0x00)
    product = BmpPixels.from_bytes(data)
    _width, _height, pixels = _pillow_rgba(data)
    # The X byte (0xEE) must be ignored; Pillow also reports alpha 255.
    assert pixels == _our_pixels(product)
    assert all(px[3] == 255 for px in pixels)


def test_pillow_written_24bit_is_read_by_catalog():
    image = Image.new("RGB", (5, 4))
    image.putdata(
        [
            ((x * 17) % 256, (y * 37) % 256, (x * y * 11) % 256)
            for y in range(4)
            for x in range(5)
        ]
    )
    buffer = io.BytesIO()
    image.save(buffer, "BMP")
    data = buffer.getvalue()
    product = BmpPixels.from_bytes(data, "pillow24.bmp")
    _w, _h, pixels = _pillow_rgba(data)
    assert _our_pixels(product) == pixels
    assert product.record.bits_per_pixel == 24
    assert product.record.compression == "BI_RGB"


def test_pillow_written_8bit_palette_is_read_by_catalog():
    image = Image.new("P", (5, 4))
    image.putpalette([v for rgb in palette8(256) for v in rgb])
    image.putdata([(y * 5 + x) % 256 for y in range(4) for x in range(5)])
    buffer = io.BytesIO()
    image.save(buffer, "BMP")
    data = buffer.getvalue()
    product = BmpPixels.from_bytes(data, "pillow8.bmp")
    _w, _h, pixels = _pillow_rgba(data)
    assert _our_pixels(product) == pixels
    assert product.record.bits_per_pixel == 8
    assert product.record.palette_entries == 256


def test_revision_exported_file_still_reads_correctly_in_pillow():
    truth = smooth_truth(6, 6, alpha=True)
    masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    product = BmpPixels.from_bytes(
        build_from_truth(truth, 32, masks=masks, padding_byte=0x00)
    )
    revised = product.revise([((1, 1, 4, 4), (250, 240, 230, 220))])
    _w, _h, pixels = _pillow_rgba(revised.file_bytes)
    # Pillow ignores the fourth (alpha) mask word, so compare RGB bytes.
    assert pixels == [px[:3] + (255,) for px in _our_pixels(revised)]
    for y in range(6):
        for x in range(6):
            expected = (250, 240, 230, 220) if 1 <= x < 4 and 1 <= y < 4 else truth.pixel(x, y)
            assert revised.pixel_at(x, y) == expected
