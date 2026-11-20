"""Exact-rational planar triangle mesh service and conservative field warehouse.

Public modules:

``conservative_remap.geometry``
    Exact rational polygon clipping and mesh validation primitives.

``conservative_remap.mesh``
    ``MeshService``: import, edge adjacency, split-edge preview/commit and
    publication, persisted in SQLite.

``conservative_remap.field``
    ``FieldWarehouse``: cell-constant density import, conservative remap runs,
    rectangular integration and contribution tracing, persisted in SQLite.
"""

from .geometry import (
    GeometryPolicy,
    MeshSnapshot,
    MeshValidationError,
    coerce_snapshot,
    intersection_area,
    clipped_polygon,
    signed_area,
    to_fraction,
)
from .mesh import MeshOperationError, MeshService
from .field import (
    FieldError,
    FieldWarehouse,
    IncompleteFieldError,
    IntegrationResult,
)

__all__ = [
    "GeometryPolicy",
    "MeshSnapshot",
    "MeshValidationError",
    "coerce_snapshot",
    "intersection_area",
    "clipped_polygon",
    "signed_area",
    "to_fraction",
    "MeshService",
    "MeshOperationError",
    "FieldWarehouse",
    "FieldError",
    "IncompleteFieldError",
    "IntegrationResult",
]
