"""Shared index primitives: scalar encoding and safe atom extraction.

Kept separate from :mod:`docrepo.repository` and :mod:`docrepo.indexing` so
both can use them without a circular import.
"""

from __future__ import annotations

from typing import Any, Sequence

from jsonpath_service.codec import path_to_json
from jsonpath_service.engine import (
    ArrayWildcard,
    Filter,
    Index,
    Key,
    _And,
    _Eq,
)

__all__ = [
    "ANY_VALUE",
    "MAX_DISTINCT_VALUES_PER_ATOM",
    "scalar_key",
    "atom_id",
    "is_plain_path",
    "safe_eq_conjuncts",
    "index_atoms_for_path",
]

# Hard cap on distinct scalar values stored per (document, atom).  A document
# exceeding it is stored under an "any value" marker, keeping it a candidate
# for every equality on that atom.  Safety is preserved; only the candidate
# set can grow.
MAX_DISTINCT_VALUES_PER_ATOM = 256
ANY_VALUE = "\x00any-scalar\x00"


def scalar_key(value: Any) -> str:
    """Index encoding that keeps JSON scalar types apart.

    Booleans never collide with integers, and null is its own key so that
    ``eq(..., null)`` remains indexable.
    """

    if value is None:
        return "null:"
    if isinstance(value, bool):
        return "bool:" + ("true" if value else "false")
    if isinstance(value, int):
        return "int:" + str(value)
    if isinstance(value, str):
        return "str:" + value
    raise TypeError(f"not a JSON scalar: {value!r}")


def atom_id(path: Sequence[Any]) -> str:
    """Canonical identity of an absolute atom path."""

    import json

    return json.dumps(
        path_to_json(path), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def is_plain_path(steps: Sequence[Any]) -> bool:
    """True when ``steps`` are only keys and non-negative indexes."""

    return all(
        isinstance(s, Key) or (isinstance(s, Index) and s.index >= 0) for s in steps
    )


def safe_eq_conjuncts(expr: Any) -> list[tuple[tuple[Any, ...], Any]]:
    """Equality conjuncts that must hold for a filter expression to be true.

    ``and`` distributes over both sides.  ``or``, ``not`` and ``exists``
    contribute nothing.  This is conservative in one direction only: fewer
    constraints means a larger candidate set, never a missed match.  A
    conjunct is usable only when its relative path is a plain non-negative
    key/index walk; wildcards and negative indexes are skipped.
    """

    if isinstance(expr, _Eq):
        return [(expr.rel, expr.scalar)] if is_plain_path(expr.rel) else []
    if isinstance(expr, _And):
        return safe_eq_conjuncts(expr.left) + safe_eq_conjuncts(expr.right)
    return []


def index_atoms_for_path(
    path: Sequence[Any],
) -> list[tuple[str, tuple[Any, ...], Any]]:
    """Return ``[(atom_id, atom_path, scalar)]`` for all safe eq atoms.

    A filter at ``prefix`` contributes ``prefix + [array_wildcard] + rel``:
    the element position is existentially quantified by the filter, so the
    index stores the wildcard variant.  If the prefix already contains a
    wildcard or filter, or is not a plain path, the atom is skipped.  That
    only makes the index smaller, never the answer wrong.
    """

    out: list[tuple[str, tuple[Any, ...], Any]] = []
    prefix: list[Any] = []
    for step in path:
        if isinstance(step, Filter) and is_plain_path(prefix):
            for rel, scalar in safe_eq_conjuncts(step.expr):
                atom_path = tuple(prefix) + (ArrayWildcard(),) + tuple(rel)
                out.append((atom_id(atom_path), atom_path, scalar))
        prefix.append(step)
    return out
