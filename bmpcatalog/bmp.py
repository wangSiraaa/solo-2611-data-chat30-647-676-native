"""Pixel access primitives shared by the catalog and the analysis module.

Supported catalog input: 40-byte ``BITMAPINFOHEADER`` BMP files, uncompressed
8-bit paletted, 24-bit BGR and 32-bit BGRX, and 32-bit ``BI_BITFIELDS`` with
contiguous, pairwise-disjoint channel masks.  RLE and embedded JPEG/PNG are
rejected.  All public coordinates are logical, top-left-origin and half-open.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Iterator

from .errors import BmpFormatError
from .geometry import Rectangle, as_rectangle

_FILE_HEADER_BYTES = 14
_DIB_HEADER_BYTES = 40
_BI_RGB = 0
_BI_RLE8 = 1
_BI_RLE4 = 2
_BI_BITFIELDS = 3
_BI_JPEG = 4
_BI_PNG = 5

_COMPRESSION_NAMES = {
    _BI_RGB: "BI_RGB",
    _BI_RLE8: "BI_RLE8",
    _BI_RLE4: "BI_RLE4",
    _BI_BITFIELDS: "BI_BITFIELDS",
    _BI_JPEG: "BI_JPEG",
    _BI_PNG: "BI_PNG",
}

_SUPPORTED_BPP = (8, 24, 32)


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little", signed=False)


def _i32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little", signed=True)


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little", signed=False)


def _row_stride(width: int, bits_per_pixel: int) -> int:
    """Bytes per stored row: pixel bytes padded to a multiple of four."""

    raw = width * bits_per_pixel
    return ((raw + 31) // 32) * 4


def _scale_channel(value: int, bits: int) -> int:
    """Map an unsigned ``bits``-wide value to 0..255 rounding halves up.

    ``value >> (bits - 8)`` is only correct for 8-bit channels.  For narrower
    masks it truncates low bits instead of rescaling, so this uses the full
    range: floor(value * 255 / (2**bits - 1) + 1/2).
    """

    if bits <= 0:
        return 0
    maximum = (1 << bits) - 1
    return (2 * value * 255 + maximum) // (2 * maximum)


def _unscale_channel(value: int, bits: int) -> int:
    """Closest ``bits``-wide code for an 8-bit value (inverse of scale)."""

    if bits <= 0:
        return 0
    maximum = (1 << bits) - 1
    return (2 * value * maximum + 255) // 510


@dataclass(frozen=True, slots=True)
class ChannelMask:
    """A contiguous run of set bits inside a little-endian pixel word."""

    shift: int
    bits: int

    @classmethod
    def from_mask(cls, mask: int, bits_per_pixel: int, name: str) -> "ChannelMask | None":
        if mask == 0:
            return None
        if mask < 0 or mask >> bits_per_pixel:
            raise BmpFormatError(
                f"{name} mask 0x{mask:08x} does not fit in {bits_per_pixel} bits"
            )
        low_bit = (mask & -mask).bit_length() - 1
        run = mask >> low_bit
        if run & (run + 1):
            raise BmpFormatError(
                f"{name} mask 0x{mask:08x} is not one contiguous run of bits"
            )
        return cls(shift=low_bit, bits=run.bit_length())

    @property
    def mask(self) -> int:
        return ((1 << self.bits) - 1) << self.shift

    def extract(self, word: int) -> int:
        return (word >> self.shift) & ((1 << self.bits) - 1)

    def decode(self, word: int) -> int:
        return _scale_channel(self.extract(word), self.bits)

    def encode(self, byte_value: int) -> int:
        return _unscale_channel(byte_value, self.bits) << self.shift


@dataclass(frozen=True, slots=True)
class PixelTotals:
    """Exact per-channel sums and alpha population counts for a pixel set."""

    count: int
    r: int
    g: int
    b: int
    a: int
    transparent: int = 0
    opaque: int = 0
    partial: int = 0

    @classmethod
    def empty(cls) -> "PixelTotals":
        return cls(0, 0, 0, 0, 0, 0, 0, 0)

    def __add__(self, other: "PixelTotals") -> "PixelTotals":
        return PixelTotals(
            self.count + other.count,
            self.r + other.r,
            self.g + other.g,
            self.b + other.b,
            self.a + other.a,
            self.transparent + other.transparent,
            self.opaque + other.opaque,
            self.partial + other.partial,
        )

    @property
    def channel_sums(self) -> tuple[int, int, int, int]:
        return (self.r, self.g, self.b, self.a)

    @property
    def means(self) -> tuple[float, float, float, float]:
        if self.count == 0:
            return (0.0, 0.0, 0.0, 0.0)
        n = self.count
        return (self.r / n, self.g / n, self.b / n, self.a / n)


@dataclass(frozen=True, slots=True)
class PixelFormat:
    """Everything needed to translate stored row bytes into RGBA values."""

    bpp: int
    compression: int
    top_down: bool
    row_stride: int
    pixel_offset: int
    pixel_row_bytes: int
    palette: tuple[tuple[int, int, int], ...] = ()
    palette_bytes: bytes = b""
    palette_entries: int = 0
    r_mask: ChannelMask | None = None
    g_mask: ChannelMask | None = None
    b_mask: ChannelMask | None = None
    a_mask: ChannelMask | None = None

    @property
    def bytes_per_pixel(self) -> int:
        return self.bpp // 8

    @property
    def padding_per_row(self) -> int:
        return self.row_stride - self.pixel_row_bytes

    def byte_offsets(self) -> tuple[int, int, int, int] | None:
        """Byte offsets for RGBA when all four channels are byte aligned.

        Returns ``None`` for generic bit-field formats.  The fourth entry is
        ``-1`` when alpha is implicit (opaque).
        """

        masks = (self.r_mask, self.g_mask, self.b_mask)
        if self.bpp != 32 or any(m is None or m.bits != 8 or m.shift % 8 for m in masks):
            return None
        offsets = [m.shift // 8 for m in masks]  # type: ignore[union-attr]
        if self.a_mask is None:
            return (offsets[0], offsets[1], offsets[2], -1)
        if self.a_mask.bits != 8 or self.a_mask.shift % 8:
            return None
        return (offsets[0], offsets[1], offsets[2], self.a_mask.shift // 8)

    def alpha_is_opaque(self) -> bool:
        return self.a_mask is None or self.bpp == 24 or self.compression == _BI_RGB

    def decode_word(self, word: int) -> tuple[int, int, int, int]:
        r = self.r_mask.decode(word) if self.r_mask else 0
        g = self.g_mask.decode(word) if self.g_mask else 0
        b = self.b_mask.decode(word) if self.b_mask else 0
        a = self.a_mask.decode(word) if self.a_mask else 255
        return (r, g, b, a)

    def decode_pixel(self, raw: bytes | memoryview, index: int) -> tuple[int, int, int, int]:
        bpp = self.bpp
        if bpp == 8:
            base = index
            pal_index = raw[base]
            r, g, b = self.palette[pal_index]
            return (r, g, b, 255)
        if bpp == 24:
            base = index * 3
            b = raw[base]
            g = raw[base + 1]
            r = raw[base + 2]
            return (r, g, b, 255)
        base = index * 4
        if self.compression == _BI_RGB:
            # Uncompressed 32-bit is BGRX: the fourth byte is not alpha.
            b = raw[base]
            g = raw[base + 1]
            r = raw[base + 2]
            return (r, g, b, 255)
        word = (
            raw[base]
            | (raw[base + 1] << 8)
            | (raw[base + 2] << 16)
            | (raw[base + 3] << 24)
        )
        return self.decode_word(word)

    def encode_pixel(
        self,
        existing: bytes | memoryview,
        index: int,
        rgba: tuple[int, int, int, int],
    ) -> bytes:
        """Encode one requested RGBA value, preserving non-channel bits.

        Raises :class:`BmpFormatError` when the requested alpha cannot be
        represented by the format (paletted/24-bit/32-bit BI_RGB have no
        alpha channel, and a bit-field map without an alpha mask is opaque).
        """

        r, g, b, a = rgba
        bpp = self.bpp
        if bpp == 8:
            raise BmpFormatError("8-bit palette encoding is handled by the revision builder")
        if bpp == 24:
            if a != 255:
                raise BmpFormatError("24-bit BGR has no alpha channel")
            return bytes((b, g, r))
        base = index * 4
        word = (
            existing[base]
            | (existing[base + 1] << 8)
            | (existing[base + 2] << 16)
            | (existing[base + 3] << 24)
        )
        if self.compression == _BI_RGB:
            if a != 255:
                raise BmpFormatError("32-bit BGRX has no alpha channel (X is ignored)")
            return bytes(
                (
                    b,
                    g,
                    r,
                    existing[base + 3],
                )
            )
        if self.a_mask is None and a != 255:
            raise BmpFormatError("bit-field format has no alpha mask; alpha must be 255")
        new = word
        for mask, value in (
            (self.r_mask, r),
            (self.g_mask, g),
            (self.b_mask, b),
            (self.a_mask, a),
        ):
            if mask is None:
                continue
            new = (new & ~mask.mask) | mask.encode(value)
        return new.to_bytes(4, "little")


def _palette_rgba_table(palette: tuple[tuple[int, int, int], ...]) -> bytes:
    table = bytearray(256 * 4)
    for index, (r, g, b) in enumerate(palette):
        table[4 * index : 4 * index + 4] = bytes((r, g, b, 255))
    return bytes(table)


@dataclass(frozen=True, slots=True)
class RowView:
    """One logical row segment, excluding row padding bytes.

    ``raw`` contains exactly the real pixel bytes for logical x in
    ``[x0, x1)``.  Padding is described separately by ``padding_offset`` and
    ``padding_length`` and is never included in ``raw`` or any digest.
    """

    logical_y: int
    x0: int
    x1: int
    raw: bytes
    pixel_offset: int
    row_file_offset: int
    padding_offset: int
    padding_length: int
    storage_row: int
    fmt: PixelFormat

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def file_range(self) -> tuple[int, int]:
        return (self.pixel_offset, self.pixel_offset + len(self.raw))

    @property
    def storage_row_range(self) -> tuple[int, int]:
        return (self.row_file_offset, self.row_file_offset + self.fmt.row_stride)

    def pixel_at(self, local_x: int) -> tuple[int, int, int, int]:
        if not 0 <= local_x < self.width:
            raise IndexError(f"local x {local_x} outside row segment width {self.width}")
        return self.fmt.decode_pixel(self.raw, local_x)

    def rgba(self) -> bytes:
        out = bytearray(self.width * 4)
        fmt = self.fmt
        if fmt.bpp == 8:
            table = _palette_rgba_table(fmt.palette)
            for i, index in enumerate(self.raw):
                start = 4 * index
                out[4 * i : 4 * i + 4] = table[start : start + 4]
            return bytes(out)
        if fmt.bpp == 24:
            for i in range(self.width):
                b = self.raw[3 * i]
                g = self.raw[3 * i + 1]
                r = self.raw[3 * i + 2]
                j = 4 * i
                out[j] = r
                out[j + 1] = g
                out[j + 2] = b
                out[j + 3] = 255
            return bytes(out)
        offsets = fmt.byte_offsets()
        if offsets is not None and fmt.compression == _BI_RGB:
            # BGRX in uncompressed 32-bit mode.
            out[2::4] = self.raw[0::4]
            out[1::4] = self.raw[1::4]
            out[0::4] = self.raw[2::4]
            out[3::4] = b"\xff" * self.width
            return bytes(out)
        for i in range(self.width):
            r, g, b, a = fmt.decode_pixel(self.raw, i)
            j = 4 * i
            out[j] = r
            out[j + 1] = g
            out[j + 2] = b
            out[j + 3] = a
        return bytes(out)

    def channel_totals(self) -> PixelTotals:
        """Exact sums for this row segment, computed without copying pixels.

        The implementations below rely on ``sum(bytes)`` and ``Counter`` which
        run at C speed; generic bit-field layouts fall back to a per-word loop.
        Padding bytes were removed before construction, so they cannot affect
        colours, alpha counts or digests.
        """

        width = self.width
        fmt = self.fmt
        if width == 0:
            return PixelTotals.empty()
        if fmt.bpp == 8:
            from collections import Counter

            counts = Counter(self.raw)
            r = g = b = 0
            for index, n in counts.items():
                pr, pg, pb = fmt.palette[index]
                r += pr * n
                g += pg * n
                b += pb * n
            return PixelTotals(width, r, g, b, 255 * width, 0, width, 0)
        if fmt.bpp == 24:
            raw = self.raw
            r = sum(raw[2::3])
            g = sum(raw[1::3])
            b = sum(raw[0::3])
            return PixelTotals(width, r, g, b, 255 * width, 0, width, 0)
        if fmt.compression == _BI_RGB:
            raw = self.raw
            r = sum(raw[2::4])
            g = sum(raw[1::4])
            b = sum(raw[0::4])
            return PixelTotals(width, r, g, b, 255 * width, 0, width, 0)

        offsets = fmt.byte_offsets()
        if offsets is not None:
            ro, go, bo, ao = offsets
            raw = self.raw
            r = sum(raw[ro::4])
            g = sum(raw[go::4])
            b = sum(raw[bo::4])
            if ao < 0:
                return PixelTotals(width, r, g, b, 255 * width, 0, width, 0)
            alpha = raw[ao::4]
            a_sum = sum(alpha)
            transparent = alpha.count(0)
            opaque = alpha.count(255)
            partial = width - transparent - opaque
            return PixelTotals(
                width, r, g, b, a_sum, transparent, opaque, partial
            )

        r_mask, g_mask, b_mask, a_mask = (
            fmt.r_mask,
            fmt.g_mask,
            fmt.b_mask,
            fmt.a_mask,
        )
        r_sum = g_sum = b_sum = a_sum = 0
        transparent = opaque = partial = 0
        for (word,) in struct.iter_unpack("<I", self.raw):
            r_sum += r_mask.decode(word) if r_mask else 0
            g_sum += g_mask.decode(word) if g_mask else 0
            b_sum += b_mask.decode(word) if b_mask else 0
            if a_mask is None:
                a_sum += 255
            else:
                alpha = a_mask.decode(word)
                a_sum += alpha
                if alpha == 0:
                    transparent += 1
                elif alpha == 255:
                    opaque += 1
                else:
                    partial += 1
        if a_mask is None:
            opaque = width
        return PixelTotals(
            width, r_sum, g_sum, b_sum, a_sum, transparent, opaque, partial
        )


@dataclass(frozen=True, slots=True)
class PixelBlock:
    """Result of a logical rectangle read."""

    rectangle: Rectangle
    width: int
    height: int
    rgba: bytes
    rows: tuple[RowView, ...]

    @property
    def byte_length(self) -> int:
        return len(self.rgba)

    def row(self, local_y: int) -> bytes:
        if not 0 <= local_y < self.height:
            raise IndexError(f"row {local_y} outside block height {self.height}")
        start = local_y * self.width * 4
        return self.rgba[start : start + self.width * 4]

    def pixel_at(self, x: int, y: int) -> tuple[int, int, int, int]:
        """Pixel at logical image coordinates."""
        local_x = x - self.rectangle.x0
        local_y = y - self.rectangle.y0
        if not (0 <= local_x < self.width and 0 <= local_y < self.height):
            raise IndexError(f"point ({x},{y}) outside block {self.rectangle}")
        start = (local_y * self.width + local_x) * 4
        return tuple(self.rgba[start : start + 4])  # type: ignore[return-value]


def _parse_channel_masks(
    data: bytes, bpp: int, pixel_offset: int
) -> tuple[ChannelMask, ChannelMask, ChannelMask, ChannelMask | None]:
    mask_end = _FILE_HEADER_BYTES + _DIB_HEADER_BYTES + 12
    if len(data) < mask_end:
        raise BmpFormatError("BI_BITFIELDS needs three channel masks at offset 54")
    if pixel_offset < mask_end:
        raise BmpFormatError("BI_BITFIELDS channel masks overlap the pixel data")
    raw_masks = [_u32(data, _FILE_HEADER_BYTES + _DIB_HEADER_BYTES),
                 _u32(data, _FILE_HEADER_BYTES + _DIB_HEADER_BYTES + 4),
                 _u32(data, _FILE_HEADER_BYTES + _DIB_HEADER_BYTES + 8)]
    names = ("red", "green", "blue")
    masks: list[ChannelMask] = []
    for name, value in zip(names, raw_masks):
        if value == 0:
            raise BmpFormatError(f"{name} channel mask is missing")
        cm = ChannelMask.from_mask(value, bpp, name)
        assert cm is not None
        masks.append(cm)
    for i in range(3):
        for j in range(i + 1, 3):
            if masks[i].mask & masks[j].mask:
                raise BmpFormatError(
                    f"channel masks {names[i]} and {names[j]} overlap"
                )
    alpha: ChannelMask | None = None
    # A 40-byte DIB header only defines three masks.  Some writers append a
    # fourth double word for alpha.  Require the pixel data to start exactly
    # after that fourth word, so gap bytes can never be mistaken for alpha.
    alpha_word_offset = _FILE_HEADER_BYTES + _DIB_HEADER_BYTES + 12
    if pixel_offset == alpha_word_offset + 4 and len(data) >= alpha_word_offset + 4:
        value = _u32(data, alpha_word_offset)
        if value:
            try:
                candidate = ChannelMask.from_mask(value, bpp, "alpha")
            except BmpFormatError:
                candidate = None
            if candidate is not None and not any(candidate.mask & m.mask for m in masks):
                alpha = candidate
    return (masks[0], masks[1], masks[2], alpha)


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    """Auditable facts about the source file and its pixel geometry."""

    file_name: str
    file_bytes: int
    declared_file_bytes: int
    sha256: str
    width: int
    height: int
    bits_per_pixel: int
    compression: str
    top_down: bool
    pixel_offset: int
    row_stride: int
    pixel_bytes_per_row: int
    padding_per_row: int
    declared_image_bytes: int
    required_pixel_bytes: int
    palette_entries: int
    internal_gap_bytes: int
    masks: dict[str, str | None]

    def as_dict(self) -> dict[str, object]:
        return {
            "file_name": self.file_name,
            "file_bytes": self.file_bytes,
            "declared_file_bytes": self.declared_file_bytes,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "bits_per_pixel": self.bits_per_pixel,
            "compression": self.compression,
            "top_down": self.top_down,
            "pixel_offset": self.pixel_offset,
            "row_stride": self.row_stride,
            "pixel_bytes_per_row": self.pixel_bytes_per_row,
            "padding_per_row": self.padding_per_row,
            "declared_image_bytes": self.declared_image_bytes,
            "required_pixel_bytes": self.required_pixel_bytes,
            "palette_entries": self.palette_entries,
            "internal_gap_bytes": self.internal_gap_bytes,
            "masks": dict(self.masks),
        }


class BmpPixels:
    """An immutable, fully validated BMP product.

    Construction validates every header claim that affects pixel addressing.
    Invalid or truncated files raise :class:`BmpFormatError`; no partially
    usable catalog object is produced.
    """

    __slots__ = (
        "_data",
        "_name",
        "_digest",
        "_width",
        "_height",
        "_fmt",
        "_record",
        "_pixel_digest",
        "_revision",
    )

    def __init__(
        self,
        data: bytes,
        file_name: str,
        digest: str,
        width: int,
        height: int,
        fmt: PixelFormat,
        declared_file_size: int,
        declared_image_size: int,
        record: CatalogRecord,
        revision=None,
    ) -> None:
        self._data = data
        self._name = file_name
        self._digest = digest
        self._width = width
        self._height = height
        self._fmt = fmt
        self._record = record
        self._pixel_digest = None
        self._revision = revision

    # ------------------------------------------------------------------ load
    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview, file_name: str = "<bytes>") -> "BmpPixels":
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("BMP data must be bytes-like")
        raw = bytes(data)
        if len(raw) < _FILE_HEADER_BYTES + _DIB_HEADER_BYTES:
            raise BmpFormatError(
                f"file is {len(raw)} bytes, shorter than a BMP file header plus "
                "BITMAPINFOHEADER"
            )
        if raw[0:2] != b"BM":
            raise BmpFormatError("missing BM signature")
        declared_file_size = _u32(raw, 2)
        pixel_offset = _u32(raw, 10)
        header_size = _u32(raw, 14)
        if header_size != _DIB_HEADER_BYTES:
            raise BmpFormatError(
                f"unsupported DIB header size {header_size}; only BITMAPINFOHEADER (40) is supported"
            )
        width = _i32(raw, 18)
        height = _i32(raw, 22)
        planes = _u16(raw, 26)
        bpp = _u16(raw, 28)
        compression = _u32(raw, 30)
        declared_image_size = _u32(raw, 34)
        colors_used = _u32(raw, 46)

        if width <= 0:
            raise BmpFormatError(f"width must be positive, got {width}")
        if height == 0:
            raise BmpFormatError("height must be non-zero")
        if planes != 1:
            raise BmpFormatError(f"planes must be 1, got {planes}")
        if bpp not in _SUPPORTED_BPP:
            raise BmpFormatError(f"unsupported bit depth {bpp}; expected 8, 24 or 32")
        if compression not in _COMPRESSION_NAMES:
            raise BmpFormatError(f"unsupported compression {compression}")
        if compression in (_BI_RLE8, _BI_RLE4, _BI_JPEG, _BI_PNG):
            raise BmpFormatError(
                f"{_COMPRESSION_NAMES[compression]} is not supported"
            )
        if bpp == 8 and compression != _BI_RGB:
            raise BmpFormatError("8-bit images must be uncompressed BI_RGB")
        if bpp == 24 and compression != _BI_RGB:
            raise BmpFormatError("24-bit images must be uncompressed BI_RGB")
        if bpp == 32 and compression not in (_BI_RGB, _BI_BITFIELDS):
            raise BmpFormatError("32-bit images must be BI_RGB or BI_BITFIELDS")
        if compression == _BI_BITFIELDS and bpp != 32:
            raise BmpFormatError("BI_BITFIELDS is only supported for 32-bit images")

        top_down = height < 0
        height_abs = -height if height < 0 else height
        stride = _row_stride(width, bpp)
        pixel_bytes_per_row = width * (bpp // 8)
        padding = stride - pixel_bytes_per_row
        required_pixel_bytes = stride * height_abs

        palette: tuple[tuple[int, int, int], ...] = ()
        palette_bytes = b""
        palette_entries = 0
        if bpp == 8:
            if colors_used > 256:
                raise BmpFormatError(f"palette size {colors_used} exceeds 256")
            if pixel_offset < _FILE_HEADER_BYTES + _DIB_HEADER_BYTES:
                raise BmpFormatError("pixel data offset overlaps the DIB header")
            available = (pixel_offset - (_FILE_HEADER_BYTES + _DIB_HEADER_BYTES)) // 4
            if colors_used:
                palette_entries = colors_used
                if available < palette_entries:
                    raise BmpFormatError(
                        f"palette declares {palette_entries} entries but only "
                        f"{available} fit before the pixel data"
                    )
            else:
                # biClrUsed == 0 means the maximum palette size for the depth.
                palette_entries = min(256, available)
                if palette_entries == 0:
                    raise BmpFormatError("8-bit image has no palette entries")
            palette_start = _FILE_HEADER_BYTES + _DIB_HEADER_BYTES
            palette_bytes = raw[palette_start : palette_start + 4 * palette_entries]
            palette = tuple(
                (
                    palette_bytes[4 * i + 2],
                    palette_bytes[4 * i + 1],
                    palette_bytes[4 * i],
                )
                for i in range(palette_entries)
            )
        elif pixel_offset < _FILE_HEADER_BYTES + _DIB_HEADER_BYTES:
            raise BmpFormatError("pixel data offset overlaps the DIB header")

        r_mask = g_mask = b_mask = None
        a_mask = None
        if compression == _BI_BITFIELDS:
            r_mask, g_mask, b_mask, a_mask = _parse_channel_masks(raw, bpp, pixel_offset)
        elif bpp == 32:
            # BI_RGB 32-bit is BGRX.  Model it with masks so decoding is
            # explicit rather than dependent on host byte order.
            r_mask = ChannelMask(16, 8)
            g_mask = ChannelMask(8, 8)
            b_mask = ChannelMask(0, 8)
            a_mask = None
        elif bpp == 24:
            # 24-bit is fixed BGR; decode_pixel handles it directly.
            r_mask = g_mask = b_mask = None
        else:
            r_mask = g_mask = b_mask = None

        # ---- file length and header claims
        if pixel_offset > len(raw):
            raise BmpFormatError(
                f"pixel offset {pixel_offset} is beyond the real file length {len(raw)}"
            )
        required_end = pixel_offset + required_pixel_bytes
        if len(raw) < required_end:
            raise BmpFormatError(
                f"truncated pixel data: need {required_end} bytes, file has {len(raw)}"
            )
        if declared_file_size:
            if declared_file_size > len(raw):
                raise BmpFormatError(
                    f"header declares {declared_file_size} bytes but file has {len(raw)}"
                )
            if declared_file_size < required_end:
                raise BmpFormatError(
                    f"header declares {declared_file_size} bytes but pixel data ends at {required_end}"
                )
        if declared_image_size:
            if declared_image_size < required_pixel_bytes:
                raise BmpFormatError(
                    f"header image size {declared_image_size} is smaller than "
                    f"the required {required_pixel_bytes} bytes"
                )
            if pixel_offset + declared_image_size > len(raw):
                raise BmpFormatError(
                    f"header image size {declared_image_size} runs past the real file"
                )

        fmt = PixelFormat(
            bpp=bpp,
            compression=compression,
            top_down=top_down,
            row_stride=stride,
            pixel_offset=pixel_offset,
            pixel_row_bytes=pixel_bytes_per_row,
            palette=palette,
            palette_bytes=palette_bytes,
            palette_entries=palette_entries,
            r_mask=r_mask,
            g_mask=g_mask,
            b_mask=b_mask,
            a_mask=a_mask,
        )

        # ---- paletted indices must exist.  Padding bytes are deliberately
        # skipped, and errors name the logical row so source positions are
        # traceable.
        if bpp == 8:
            entries = palette_entries
            for logical_y in range(height_abs):
                stored = (height_abs - 1 - logical_y) if not top_down else logical_y
                start = pixel_offset + stored * stride
                row = raw[start : start + pixel_bytes_per_row]
                if row and max(row) >= entries:
                    raise BmpFormatError(
                        f"palette index {max(row)} at logical row {logical_y} "
                        f"is not present in the {entries}-entry palette"
                    )

        digest = hashlib.sha256(raw).hexdigest()
        gap = pixel_offset - (
            _FILE_HEADER_BYTES + _DIB_HEADER_BYTES + (4 * palette_entries if bpp == 8 else 0)
        )
        record = CatalogRecord(
            file_name=file_name,
            file_bytes=len(raw),
            declared_file_bytes=declared_file_size,
            sha256=digest,
            width=width,
            height=height_abs,
            bits_per_pixel=bpp,
            compression=_COMPRESSION_NAMES[compression],
            top_down=top_down,
            pixel_offset=pixel_offset,
            row_stride=stride,
            pixel_bytes_per_row=pixel_bytes_per_row,
            padding_per_row=padding,
            declared_image_bytes=declared_image_size,
            required_pixel_bytes=required_pixel_bytes,
            palette_entries=palette_entries,
            internal_gap_bytes=max(0, gap),
            masks={
                "red": f"0x{r_mask.mask:08x}" if r_mask else None,
                "green": f"0x{g_mask.mask:08x}" if g_mask else None,
                "blue": f"0x{b_mask.mask:08x}" if b_mask else None,
                "alpha": f"0x{a_mask.mask:08x}" if a_mask else None,
            },
        )
        return cls(
            raw,
            file_name,
            digest,
            width,
            height_abs,
            fmt,
            declared_file_size,
            declared_image_size,
            record,
        )

    @classmethod
    def from_path(cls, path: str | "os.PathLike[str]") -> "BmpPixels":
        import os

        with open(path, "rb") as handle:
            data = handle.read()
        return cls.from_bytes(data, os.fspath(path))

    # ------------------------------------------------------------- metadata
    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def bits_per_pixel(self) -> int:
        return self._fmt.bpp

    @property
    def top_down(self) -> bool:
        return self._fmt.top_down

    @property
    def row_stride(self) -> int:
        return self._fmt.row_stride

    @property
    def pixel_offset(self) -> int:
        return self._fmt.pixel_offset

    @property
    def file_name(self) -> str:
        return self._name

    @property
    def sha256(self) -> str:
        return self._digest

    @property
    def file_bytes(self) -> bytes:
        return self._data

    @property
    def file_size(self) -> int:
        return len(self._data)

    @property
    def palette(self) -> tuple[tuple[int, int, int], ...]:
        return self._fmt.palette

    @property
    def format(self) -> PixelFormat:
        return self._fmt

    @property
    def record(self) -> CatalogRecord:
        return self._record

    def catalog_record(self) -> dict[str, object]:
        return self._record.as_dict()

    def pixel_sha256(self) -> str:
        """Digest of logical RGBA pixels only; padding is excluded."""

        if self._pixel_digest is None:
            digest = hashlib.sha256()
            for _, row in self.iter_rgba_rows(Rectangle(0, 0, self._width, self._height)):
                digest.update(row)
            self._pixel_digest = digest.hexdigest()
        return self._pixel_digest

    # --------------------------------------------------------- pixel access
    def _storage_row(self, logical_y: int) -> int:
        if not 0 <= logical_y < self._height:
            raise IndexError(f"logical y {logical_y} outside image height {self._height}")
        return (self._height - 1 - logical_y) if not self._fmt.top_down else logical_y

    def storage_row_bytes(self, logical_y: int) -> tuple[int, int]:
        """File range of the complete stored row, including padding."""

        stored = self._storage_row(logical_y)
        start = self._fmt.pixel_offset + stored * self._fmt.row_stride
        return (start, start + self._fmt.row_stride)

    def iter_rows(self, rectangle: Rectangle | tuple[int, int, int, int]) -> Iterator[RowView]:
        rect = as_rectangle(rectangle).validate_inside(self._width, self._height)
        fmt = self._fmt
        bpp_bytes = fmt.bytes_per_pixel
        pad = fmt.padding_per_row
        for logical_y in range(rect.y0, rect.y1):
            stored = self._storage_row(logical_y)
            row_start = fmt.pixel_offset + stored * fmt.row_stride
            pixel_start = row_start + rect.x0 * bpp_bytes
            pixel_end = row_start + rect.x1 * bpp_bytes
            raw = self._data[pixel_start:pixel_end]
            padding_offset = row_start + fmt.pixel_row_bytes
            yield RowView(
                logical_y=logical_y,
                x0=rect.x0,
                x1=rect.x1,
                raw=raw,
                pixel_offset=pixel_start,
                row_file_offset=row_start,
                padding_offset=padding_offset,
                padding_length=pad,
                storage_row=stored,
                fmt=fmt,
            )

    def iter_rgba_rows(
        self, rectangle: Rectangle | tuple[int, int, int, int]
    ) -> Iterator[tuple[int, bytes]]:
        for row in self.iter_rows(rectangle):
            yield row.logical_y, row.rgba()

    def read_rect(self, rectangle: Rectangle | tuple[int, int, int, int]) -> PixelBlock:
        rect = as_rectangle(rectangle)
        rows = tuple(self.iter_rows(rect))
        body = bytearray()
        for row in rows:
            body.extend(row.rgba())
        return PixelBlock(
            rectangle=rect,
            width=rect.width,
            height=rect.height,
            rgba=bytes(body),
            rows=rows,
        )

    def read_rgba(self, rectangle: Rectangle | tuple[int, int, int, int]) -> bytes:
        return self.read_rect(rectangle).rgba

    def pixel_at(self, x: int, y: int) -> tuple[int, int, int, int]:
        rect = Rectangle(x, y, x + 1, y + 1).validate_inside(self._width, self._height)
        row = next(self.iter_rows(rect))
        return row.pixel_at(0)

    def totals(self, rectangle: Rectangle | tuple[int, int, int, int]) -> PixelTotals:
        """Exact totals for one rectangle, using the public row API."""

        total = PixelTotals.empty()
        for row in self.iter_rows(rectangle):
            total = total + row.channel_totals()
        return total

    # ------------------------------------------------------------- revision
    def revise(self, updates) -> "BmpPixels":
        from .revision import revise_product

        return revise_product(self, updates)

    @property
    def revision(self):
        """Revision bookkeeping, or ``None`` for an original product."""

        return self._revision

    def _attach_revision(self, info) -> None:
        # Only the revision builder reaches this; the product stays immutable
        # to callers because the attribute is never exposed for assignment.
        object.__setattr__(self, "_revision", info)

    def save(self, path: str) -> int:
        """Write the exact product bytes to ``path``; returns byte count."""

        with open(path, "wb") as handle:
            return handle.write(self._data)
