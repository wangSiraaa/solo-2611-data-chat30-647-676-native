"""Small BMP writer used to build fixtures and to export revised products.

It intentionally supports only the subset the catalog understands, which makes
it a useful independent second implementation for round-trip checks: pixel
values are laid out from logical top-left RGBA input, row padding is filled by
a caller-supplied function, and negative height writes top-down rows.
"""

from __future__ import annotations

import struct
from typing import Callable, Sequence

RGBA = tuple[int, int, int, int]
PaddingFn = Callable[[int, int], bytes]


def _scale(value: int, bits: int) -> int:
    maximum = (1 << bits) - 1
    return (2 * value * maximum + 255) // 510


def _row_stride(width: int, bpp: int) -> int:
    return ((width * bpp + 31) // 32) * 4


def _default_padding(row_index: int, length: int) -> bytes:
    return bytes((0xA5,) * length)


def build_bmp(
    width: int,
    height: int,
    bpp: int,
    pixels: Sequence[Sequence[RGBA]],
    *,
    top_down: bool = False,
    palette: Sequence[tuple[int, int, int]] | None = None,
    # Bit-field layout for 32-bit BI_BITFIELDS, ordered (r, g, b, a).
    masks: tuple[int, int, int, int] | None = None,
    padding_fn: PaddingFn | None = None,
    gap_bytes: int = 0,
    declared_file_size: int | None = None,
    declared_image_size: int | None = None,
    extra_after_pixels: bytes = b"",
) -> bytes:
    """Return a BMP byte string.

    ``pixels`` is indexed ``[y][x]`` in logical top-left coordinates.  For
    ``bpp == 8`` each RGBA's RGB triple must exist in ``palette``.  For 24-bit
    input, alpha must be 255.  For 32-bit BI_RGB, alpha is ignored (X bytes are
    preserved as the low padding pattern 0xEE).
    """

    if len(pixels) != height:
        raise ValueError(f"expected {height} rows, got {len(pixels)}")
    if any(len(row) != width for row in pixels):
        raise ValueError("every pixel row must have exactly width entries")
    padding_fn = padding_fn or _default_padding

    compression = 3 if masks is not None else 0
    palette = tuple(palette or ())
    palette_bytes = b""
    palette_entries = 0
    if bpp == 8:
        if not palette:
            raise ValueError("8-bit BMP needs a palette")
        if len(palette) > 256:
            raise ValueError("palette has more than 256 entries")
        palette_entries = len(palette)
        lookup = {rgb: i for i, rgb in enumerate(palette)}
        palette_bytes = b"".join(bytes((b, g, r, 0)) for r, g, b in palette)
        for y, row in enumerate(pixels):
            for x, rgba in enumerate(row):
                if rgba[:3] not in lookup:
                    raise ValueError(f"pixel ({x},{y}) colour {rgba[:3]} missing from palette")
                if rgba[3] != 255:
                    raise ValueError("8-bit BMP has no alpha channel")

    if masks is not None:
        if bpp != 32:
            raise ValueError("bitfields writer only supports 32-bit")
        if len(masks) == 3:
            rm, gm, bm = masks
            am = 0
        else:
            rm, gm, bm, am = masks
        mask_values = (rm, gm, bm, am)
        for i in range(4):
            for j in range(i + 1, 4):
                if mask_values[i] & mask_values[j]:
                    raise ValueError("bit-field masks overlap")
        if not (rm and gm and bm):
            raise ValueError("red, green and blue masks are required")

    stride = _row_stride(width, bpp)
    pixel_row_bytes = width * (bpp // 8)
    pad_len = stride - pixel_row_bytes
    body = bytearray()
    # Storage order: bottom-up unless top-down.  Padding bytes never carry a
    # pixel and are generated per stored row so tests can vary them.
    stored_rows = range(height) if top_down else range(height - 1, -1, -1)
    for stored, logical_y in enumerate(stored_rows):
        row = bytearray()
        if bpp == 8:
            for rgba in pixels[logical_y]:
                row.append(lookup[rgba[:3]])
        elif bpp == 24:
            for rgba in pixels[logical_y]:
                if rgba[3] != 255:
                    raise ValueError("24-bit BMP has no alpha channel")
                r, g, b, _ = rgba
                row.extend((b, g, r))
        elif bpp == 32 and compression == 0:
            for rgba in pixels[logical_y]:
                r, g, b, _ = rgba
                row.extend((b, g, r, 0xEE))
        else:
            for rgba in pixels[logical_y]:
                r, g, b, a = rgba
                if not am and a != 255:
                    raise ValueError("bit-field image without alpha mask must use alpha=255")
                word = (
                    _scale(r, _bits(rm)) << _shift(rm)
                    | _scale(g, _bits(gm)) << _shift(gm)
                    | _scale(b, _bits(bm)) << _shift(bm)
                )
                if am:
                    word |= _scale(a, _bits(am)) << _shift(am)
                row.extend(word.to_bytes(4, "little"))
        if pad_len:
            row.extend(padding_fn(stored, pad_len))
        body.extend(row)

    mask_bytes = b""
    if compression == 3:
        mask_bytes = struct.pack("<III", rm, gm, bm)
        if am:
            mask_bytes += struct.pack("<I", am)

    offset = 14 + 40 + len(mask_bytes) + len(palette_bytes) + gap_bytes
    image_size = len(body)
    file_size = offset + image_size + len(extra_after_pixels)
    header = bytearray()
    header += b"BM"
    header += struct.pack("<I", declared_file_size if declared_file_size is not None else file_size)
    header += b"\x00\x00\x00\x00"
    header += struct.pack("<I", offset)
    header += struct.pack("<I", 40)
    header += struct.pack("<i", width)
    header += struct.pack("<i", -height if top_down else height)
    header += struct.pack("<H", 1)
    header += struct.pack("<H", bpp)
    header += struct.pack("<I", compression)
    header += struct.pack("<I", declared_image_size if declared_image_size is not None else image_size)
    header += struct.pack("<i", 2835)
    header += struct.pack("<i", 2835)
    header += struct.pack("<I", palette_entries)
    header += struct.pack("<I", 0)

    return bytes(header) + mask_bytes + palette_bytes + bytes(gap_bytes) + bytes(body) + extra_after_pixels


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def _bits(mask: int) -> int:
    return (mask >> _shift(mask)).bit_length()


def write_bmp(path: str, *args, **kwargs) -> bytes:
    data = build_bmp(*args, **kwargs)
    with open(path, "wb") as handle:
        handle.write(data)
    return data
