"""Exception types used by the BMP catalog and region statistics warehouse.

The package deliberately uses explicit exception types so callers can tell a
malformed source image apart from a bad query (for example an out-of-bounds
rectangle).
"""

from __future__ import annotations


class BmpCatalogError(Exception):
    """Base class for every error raised by this package."""


class BmpFormatError(BmpCatalogError):
    """The input is not a BMP file in the supported subset.

    No partially decoded product is ever returned when this is raised.
    """


class RectangleError(BmpCatalogError, ValueError):
    """A rectangle is not a valid half-open integer rectangle."""


class RectangleOutOfBounds(RectangleError):
    """A rectangle leaves the image.  The library never silently clips it."""


class RevisionError(BmpCatalogError):
    """A revision request cannot be represented by the source BMP format."""
