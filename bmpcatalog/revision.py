"""Revision support: produce a new immutable BMP product in place.

A revision only rewrites the real pixel bytes covered by the requested
rectangles (and, when required, nothing else).  Row padding, palette data,
uncovered pixels and all unrelated header bytes are copied verbatim.  The
result is re-validated through :meth:`BmpPixels.from_bytes`, so a revision can
never yield a malformed catalog product.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .bmp import _BI_BITFIELDS
from .errors import BmpFormatError, RevisionError
from .geometry import Rectangle, as_rectangle

RGBA = tuple[int, int, int, int]
PixelProvider = bytes | bytearray | memoryview | RGBA | Callable[[int, int], RGBA]


@dataclass(frozen=True, slots=True)
class RevisionUpdate:
    rectangle: Rectangle
    provider: PixelProvider

    def value_at(self, x: int, y: int, local_index: int) -> RGBA:
        provider = self.provider
        if isinstance(provider, (bytes, bytearray, memoryview)):
            offset = local_index * 4
            if offset + 4 > len(provider):
                raise RevisionError(
                    "RGBA byte payload is shorter than the rectangle area"
                )
            return tuple(provider[offset : offset + 4])  # type: ignore[return-value]
        if callable(provider):
            return _validate_rgba(provider(x, y))
        return _validate_rgba(provider)


@dataclass(frozen=True, slots=True)
class RevisionInfo:
    """Bookkeeping attached to a revised product."""

    parent_sha256: str
    updates: tuple[Rectangle, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "parent_sha256": self.parent_sha256,
            "updates": [rect.to_tuple() for rect in self.updates],
        }


def _validate_rgba(value: Sequence[int]) -> RGBA:
    if len(value) != 4:
        raise RevisionError("pixel provider must yield 4 channel values")
    channels = []
    for channel in value:
        if isinstance(channel, bool) or not isinstance(channel, int):
            raise RevisionError("channel values must be ints")
        if not 0 <= channel <= 255:
            raise RevisionError(f"channel value {channel} is outside 0..255")
        channels.append(channel)
    return (channels[0], channels[1], channels[2], channels[3])


def _normalise_updates(updates) -> list[RevisionUpdate]:
    normalised: list[RevisionUpdate] = []
    for item in updates:
        if isinstance(item, RevisionUpdate):
            update = item
        else:
            try:
                rect_value, provider = item
            except (TypeError, ValueError) as exc:
                raise RevisionError(
                    "each update must be RevisionUpdate or (rectangle, provider)"
                ) from exc
            update = RevisionUpdate(as_rectangle(rect_value), provider)
        if isinstance(update.provider, (bytes, bytearray, memoryview)):
            expected = update.rectangle.area * 4
            if len(update.provider) != expected:
                raise RevisionError(
                    f"RGBA payload length {len(update.provider)} does not match "
                    f"{update.rectangle.area} pixels ({expected} bytes)"
                )
        normalised.append(update)
    return normalised


def revise_product(product, updates: Iterable) -> "object":
    normalised = _normalise_updates(updates)
    width, height = product.width, product.height
    for update in normalised:
        update.rectangle.validate_inside(width, height)

    # Later updates deliberately win when rectangles overlap.
    data = bytearray(product.file_bytes)
    fmt = product.format
    palette_lookup = {rgb: index for index, rgb in enumerate(fmt.palette)}
    bpp_bytes = fmt.bytes_per_pixel

    for update in normalised:
        rect = update.rectangle
        local_index = 0
        for row in product.iter_rows(rect):
            row_start = row.pixel_offset
            for local_x in range(rect.width):
                x = rect.x0 + local_x
                y = row.logical_y
                rgba = update.value_at(x, y, local_index)
                local_index += 1
                if fmt.bpp == 8:
                    if rgba[3] != 255:
                        raise RevisionError(
                            "8-bit paletted BMP has no alpha channel; alpha must be 255"
                        )
                    index = palette_lookup.get(rgba[:3])
                    if index is None:
                        raise RevisionError(
                            f"colour {rgba[:3]} is not present in the existing palette; "
                            "revisions never rewrite palette data"
                        )
                    data[row_start + local_x] = index
                else:
                    try:
                        encoded = fmt.encode_pixel(
                            memoryview(data)[row_start:], local_x, rgba
                        )
                    except BmpFormatError as exc:
                        raise RevisionError(str(exc)) from exc
                    if fmt.compression == _BI_BITFIELDS:
                        actual = fmt.decode_pixel(encoded, 0)
                        if actual != rgba:
                            raise RevisionError(
                                f"bit-field layout cannot represent RGBA {rgba} at "
                                f"({x},{y}); it would decode as {actual}"
                            )
                    offset = row_start + local_x * bpp_bytes
                    data[offset : offset + bpp_bytes] = encoded

    # Re-validate from scratch.  This also recomputes digest/record and proves
    # that the resulting bytes are still a supported, untruncated BMP.
    revised = type(product).from_bytes(bytes(data), product.file_name)
    info = RevisionInfo(
        parent_sha256=product.sha256,
        updates=tuple(update.rectangle for update in normalised),
    )
    revised._attach_revision(info)
    return revised
