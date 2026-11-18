"""Updateable document search repository built on the standalone path engine."""

from .repository import (
    DocNotFound,
    Explanation,
    QueryConflict,
    QueryDef,
    Repository,
    SearchResult,
    StaleRevision,
)

__all__ = [
    "DocNotFound",
    "Explanation",
    "QueryConflict",
    "QueryDef",
    "Repository",
    "SearchResult",
    "StaleRevision",
]
