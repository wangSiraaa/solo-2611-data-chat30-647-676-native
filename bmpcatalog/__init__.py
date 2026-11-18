"""BMP pixel catalog and exact region statistics warehouse.

Public entry points
-------------------
* :class:`~bmpcatalog.bmp.BmpPixels` — immutable, validated pixel catalog.
* :class:`~bmpcatalog.geometry.Rectangle` — logical half-open rectangle.
* :class:`~bmpcatalog.analysis.RegionAnalyzer` — tile-cached exact stats.
* :func:`~bmpcatalog.writer.build_bmp` — fixture writer for the same subset.
"""

from .analysis import (
    RegionAnalyzer,
    RegionReport,
    RegionStats,
    UnionStats,
    direct_rect_stats,
    direct_union_stats,
    union_of,
)
from .bmp import (
    BmpPixels,
    CatalogRecord,
    ChannelMask,
    PixelBlock,
    PixelFormat,
    PixelTotals,
    RowView,
)
from .errors import (
    BmpCatalogError,
    BmpFormatError,
    RectangleError,
    RectangleOutOfBounds,
    RevisionError,
)
from .geometry import Rectangle, as_rectangle, coerce_rectangles
from .revision import RevisionInfo, RevisionUpdate
from .writer import build_bmp, write_bmp

__all__ = [
    "BmpPixels",
    "BmpCatalogError",
    "BmpFormatError",
    "CatalogRecord",
    "ChannelMask",
    "PixelBlock",
    "PixelFormat",
    "PixelTotals",
    "Rectangle",
    "RectangleError",
    "RectangleOutOfBounds",
    "RegionAnalyzer",
    "RegionReport",
    "RegionStats",
    "RevisionError",
    "RevisionInfo",
    "RevisionUpdate",
    "RowView",
    "UnionStats",
    "as_rectangle",
    "build_bmp",
    "coerce_rectangles",
    "direct_rect_stats",
    "direct_union_stats",
    "union_of",
    "write_bmp",
]
