"""Strict/lax structured JSON path engine.

The engine is deliberately standalone: it has no dependency on the document
repository.  A path is a sequence of structured steps (not a JSONPath string).
Every step is evaluated against the *current sequence* of (path, value) hits.

Semantics implemented here
--------------------------
* ``key``                object member lookup.  Only objects match.
* ``index``              array position, negative indexes count from the end.
* ``array_wildcard``     every array element, in array order.
* ``object_wildcard``    every object member, in Unicode code-point key order.
* ``filter``             applies a boolean expression to each element of the
                         current array.  Only arrays match; the expression is
                         only allowed to address paths relative to one element.

``strict`` mode raises :class:`PathError` at the first type error, missing key
or out-of-range index.  ``lax`` mode silently drops the offending branch.  Lax
mode never flattens arrays and never wraps scalars.

A hit carries the original document path (a tuple of keys and array indexes)
and the original value.  ``{"a": null}`` at ``a`` is one hit whose value is
null; ``{"a": []}`` yields no hit for ``a[*]``; a missing member yields no hit.
Those three cases remain distinguishable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

__all__ = [
    "PathError",
    "Hit",
    "Filter",
    "Key",
    "Index",
    "ArrayWildcard",
    "ObjectWildcard",
    "FilterStep",
    "exists",
    "eq",
    "and_",
    "or_",
    "not_",
    "evaluate",
    "evaluate_filter",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathError(Exception):
    """A locatable strict-mode path failure.

    ``step_index`` is the index in the *path* that failed, ``hit_path`` is the
    document path of the value the failing step was applied to, and
    ``hit_value_type`` names the value's JSON type.  ``reason`` is one of
    ``type``, ``missing_key`` or ``index_out_of_range``.
    """

    reason: str
    step_index: int
    hit_path: tuple[Any, ...]
    hit_value_type: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        where = "root" if not self.hit_path else _render_path(self.hit_path)
        return f"{self.reason} at step {self.step_index} ({where}, {self.hit_value_type}): {self.message}"


def _render_path(path: tuple[Any, ...]) -> str:
    out = []
    for part in path:
        if isinstance(part, int):
            out.append(f"[{part}]")
        elif isinstance(part, str) and part and all(c.isalnum() or c in "_-" for c in part):
            out.append(("." if out else "") + part)
        else:
            out.append(f"[{part!r}]")
    return "".join(out) or "$"


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("key step requires a string name")


@dataclass(frozen=True)
class Index:
    """Array index.  Negative indexes count from the end of the array."""

    index: int

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int):
            raise TypeError("index step requires an integer")


@dataclass(frozen=True)
class ArrayWildcard:
    pass


@dataclass(frozen=True)
class ObjectWildcard:
    pass


# ---------------------------------------------------------------------------
# Filter expressions (three-valued logic)
# ---------------------------------------------------------------------------

UNKNOWN = object()


class _Expr:
    __slots__ = ()


@dataclass(frozen=True)
class _Exists(_Expr):
    rel: tuple[Any, ...]


@dataclass(frozen=True)
class _Eq(_Expr):
    rel: tuple[Any, ...]
    scalar: Any


@dataclass(frozen=True)
class _And(_Expr):
    left: Any
    right: Any


@dataclass(frozen=True)
class _Or(_Expr):
    left: Any
    right: Any


@dataclass(frozen=True)
class _Not(_Expr):
    operand: Any


def _rel(path: Any) -> tuple[Any, ...]:
    """Validate and freeze a relative path.

    A relative path is always interpreted from the current array element.  It
    may use keys, non-escaping indexes (including negative ones) and
    wildcards, but never a parent step, so it cannot leave the element.  A
    nested filter is rejected to keep expressions unambiguous.
    """

    if isinstance(path, (str, bytes)):
        raise TypeError("relative path must be a sequence of steps, not a string")
    steps = []
    for step in path:
        if isinstance(step, (Key, Index, ArrayWildcard, ObjectWildcard)):
            steps.append(step)
        else:
            raise TypeError(f"relative filter paths do not allow {step!r}")
    return tuple(steps)


def exists(relative_path: Iterable[Any]) -> _Expr:
    return _Exists(_rel(relative_path))


def eq(relative_path: Iterable[Any], scalar: Any) -> _Expr:
    if scalar is not None and not isinstance(scalar, (str, int, bool)):
        raise TypeError("eq scalar must be a string, integer, boolean or null")
    if isinstance(scalar, int) and not isinstance(scalar, bool):
        # An int scalar is compared only to integers, never to booleans.
        pass
    return _Eq(_rel(relative_path), scalar)


def and_(*operands: Any) -> _Expr:
    if len(operands) < 2:
        raise TypeError("and_ needs at least two operands")
    for operand in operands:
        _check_expr(operand)
    result = operands[-1]
    for operand in reversed(operands[:-1]):
        result = _And(operand, result)
    return result


def or_(*operands: Any) -> _Expr:
    if len(operands) < 2:
        raise TypeError("or_ needs at least two operands")
    for operand in operands:
        _check_expr(operand)
    result = operands[-1]
    for operand in reversed(operands[:-1]):
        result = _Or(operand, result)
    return result


def not_(operand: Any) -> _Expr:
    _check_expr(operand)
    return _Not(operand)


def _check_expr(expr: Any) -> None:
    if not isinstance(expr, _Expr):
        raise TypeError(f"expected a filter expression, got {expr!r}")


class Filter:
    """A filter step: an expression evaluated per current array element."""

    __slots__ = ("expr",)
    __repr__ = lambda self: f"Filter({self.expr!r})"  # noqa: E731

    def __init__(self, expr: Any) -> None:
        _check_expr(expr)
        self.expr = expr

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Filter) and self.expr == other.expr

    def __hash__(self) -> int:
        return hash((Filter, self.expr))


FilterStep = Filter


# ---------------------------------------------------------------------------
# Hit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    path: tuple[Any, ...]
    value: Any

    def __iter__(self) -> Iterator[Any]:  # allows ``path, value = hit``
        yield self.path
        yield self.value


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _resolve_relative(value: Any, rel: tuple[Any, ...]) -> list[Any]:
    """Return all values reached by ``rel`` from ``value``.

    Resolution always starts at a single array element, so conditions from
    different elements are never combined.  It follows the same step semantics
    as the main engine, including negative indexes and both wildcards, but
    silently ignores type mismatches.
    """

    current = [value]
    for step in rel:
        nxt = []
        for item in current:
            if isinstance(step, Key):
                if isinstance(item, dict) and step.name in item:
                    nxt.append(item[step.name])
            elif isinstance(step, Index):
                if isinstance(item, list):
                    idx = step.index
                    if idx < 0:
                        idx += len(item)
                    if 0 <= idx < len(item):
                        nxt.append(item[idx])
            elif isinstance(step, ArrayWildcard):
                if isinstance(item, list):
                    nxt.extend(item)
            elif isinstance(step, ObjectWildcard):
                if isinstance(item, dict):
                    for key in sorted(item):
                        nxt.append(item[key])
        current = nxt
        if not current:
            return []
    return current


def _tri_eq(hit_values: list[Any], scalar: Any) -> Any:
    """Three-valued equality.

    ``eq`` is comparable only when the relative path hits **exactly one
    scalar**.  Zero hits, multiple hits, or a single non-scalar are therefore
    *unknown*.  Among scalars, differing JSON types are a definite false
    (booleans are not integers), and ``null`` equals only ``null``.
    """

    if len(hit_values) != 1:
        return UNKNOWN
    value = hit_values[0]
    if not (value is None or isinstance(value, (str, bool, int))):
        return UNKNOWN
    # JSON type identity: booleans are not integers; null equals only null.
    if value is None or scalar is None:
        return value is None and scalar is None
    if isinstance(value, bool) or isinstance(scalar, bool):
        return (
            isinstance(value, bool)
            and isinstance(scalar, bool)
            and value == scalar
        )
    if isinstance(value, int) and isinstance(scalar, int):
        return value == scalar
    if isinstance(value, str) and isinstance(scalar, str):
        return value == scalar
    # Different JSON scalar types.
    return False


def _tri(expr: Any, element: Any) -> Any:
    """Evaluate ``expr`` against one array element.  Returns bool or UNKNOWN."""

    if isinstance(expr, _Exists):
        return bool(_resolve_relative(element, expr.rel))
    if isinstance(expr, _Eq):
        return _tri_eq(_resolve_relative(element, expr.rel), expr.scalar)
    if isinstance(expr, _And):
        left = _tri(expr.left, element)
        if left is False:
            return False
        right = _tri(expr.right, element)
        if right is False:
            return False
        if left is UNKNOWN or right is UNKNOWN:
            return UNKNOWN
        return True
    if isinstance(expr, _Or):
        left = _tri(expr.left, element)
        if left is True:
            return True
        right = _tri(expr.right, element)
        if right is True:
            return True
        if left is UNKNOWN or right is UNKNOWN:
            return UNKNOWN
        return False
    if isinstance(expr, _Not):
        inner = _tri(expr.operand, element)
        if inner is UNKNOWN:
            return UNKNOWN
        return not inner
    raise TypeError(f"unknown filter expression {expr!r}")


def evaluate_filter(expr: Any, element: Any) -> Any:
    """Public helper: bool or UNKNOWN for one element."""

    _check_expr(expr)
    return _tri(expr, element)


def _apply_step(
    hits: list[Hit], step: Any, step_index: int, strict: bool, errors: list[PathError]
) -> list[Hit]:
    out: list[Hit] = []
    for hit in hits:
        value = hit.value
        if isinstance(step, Key):
            if isinstance(value, dict):
                if step.name in value:
                    out.append(Hit(hit.path + (step.name,), value[step.name]))
                elif strict:
                    errors.append(
                        PathError("missing_key", step_index, hit.path, "object", f"missing key {step.name!r}")
                    )
            elif strict:
                errors.append(
                    PathError("type", step_index, hit.path, _type_name(value), f"key step requires object, got {_type_name(value)}")
                )
        elif isinstance(step, Index):
            if isinstance(value, list):
                idx = step.index
                if idx < 0:
                    idx += len(value)
                if 0 <= idx < len(value):
                    out.append(Hit(hit.path + (idx,), value[idx]))
                elif strict:
                    errors.append(
                        PathError(
                            "index_out_of_range",
                            step_index,
                            hit.path,
                            "array",
                            f"index {step.index} out of range for array of length {len(value)}",
                        )
                    )
            elif strict:
                errors.append(
                    PathError("type", step_index, hit.path, _type_name(value), f"index step requires array, got {_type_name(value)}")
                )
        elif isinstance(step, ArrayWildcard):
            if isinstance(value, list):
                for i, item in enumerate(value):
                    out.append(Hit(hit.path + (i,), item))
            elif strict:
                errors.append(
                    PathError(
                        "type", step_index, hit.path, _type_name(value), f"array wildcard requires array, got {_type_name(value)}"
                    )
                )
        elif isinstance(step, ObjectWildcard):
            if isinstance(value, dict):
                for key in sorted(value):
                    out.append(Hit(hit.path + (key,), value[key]))
            elif strict:
                errors.append(
                    PathError(
                        "type", step_index, hit.path, _type_name(value), f"object wildcard requires object, got {_type_name(value)}"
                    )
                )
        elif isinstance(step, Filter):
            if isinstance(value, list):
                for i, item in enumerate(value):
                    if _tri(step.expr, item) is True:
                        out.append(Hit(hit.path + (i,), item))
            elif strict:
                errors.append(
                    PathError(
                        "type", step_index, hit.path, _type_name(value), f"filter requires array, got {_type_name(value)}"
                    )
                )
        else:
            raise TypeError(f"unknown path step {step!r}")
    return out


def evaluate(
    document: Any,
    path: Sequence[Any],
    *,
    strict: bool = False,
) -> list[Hit]:
    """Evaluate ``path`` against ``document``.

    Returns every hit in a deterministic order.  In strict mode the first
    failure raises :class:`PathError`; there is no such thing as a partially
    successful strict evaluation.  In lax mode branches that fail are dropped.
    """

    current = [Hit((), document)]
    errors: list[PathError] = []
    for step_index, step in enumerate(path):
        current = _apply_step(current, step, step_index, strict, errors)
        if strict and errors:
            raise errors[0]
    return current


