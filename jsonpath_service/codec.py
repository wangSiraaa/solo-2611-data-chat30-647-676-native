"""JSON (de)serialisation for structured paths and filter expressions.

Both the standalone service and the repository use this module, so a query
persisted by one is interpreted identically by the other.
"""

from __future__ import annotations

from typing import Any, Sequence

from .engine import (
    ArrayWildcard,
    Filter,
    Index,
    Key,
    ObjectWildcard,
    _And,
    _Eq,
    _Exists,
    _Not,
    _Or,
    and_,
    eq,
    exists,
    not_,
    or_,
)

__all__ = ["path_to_json", "path_from_json", "expr_to_json", "expr_from_json"]


def path_to_json(path: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for step in path:
        if isinstance(step, Key):
            out.append({"key": step.name})
        elif isinstance(step, Index):
            out.append({"index": step.index})
        elif isinstance(step, ArrayWildcard):
            out.append({"wildcard": "array"})
        elif isinstance(step, ObjectWildcard):
            out.append({"wildcard": "object"})
        elif isinstance(step, Filter):
            out.append({"filter": expr_to_json(step.expr)})
        else:  # pragma: no cover - defensive
            raise TypeError(f"cannot serialise step {step!r}")
    return out


def expr_to_json(expr: Any) -> Any:
    if isinstance(expr, _Exists):
        return {"exists": path_to_json(expr.rel)}
    if isinstance(expr, _Eq):
        return {"eq": [path_to_json(expr.rel), expr.scalar]}
    if isinstance(expr, _And):
        return {"and": [expr_to_json(expr.left), expr_to_json(expr.right)]}
    if isinstance(expr, _Or):
        return {"or": [expr_to_json(expr.left), expr_to_json(expr.right)]}
    if isinstance(expr, _Not):
        return {"not": expr_to_json(expr.operand)}
    raise TypeError(f"cannot serialise expression {expr!r}")


def _rel_from_json(raw: Any) -> tuple[Any, ...]:
    if not isinstance(raw, list):
        raise ValueError("relative path must be an array of steps")
    steps = []
    for item in raw:
        step = _step_from_json(item)
        if not isinstance(step, (Key, Index, ArrayWildcard, ObjectWildcard)):
            raise ValueError("relative path steps must be key, index or wildcard")
        steps.append(step)
    return tuple(steps)


def _step_from_json(raw: Any) -> Any:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError(f"bad step {raw!r}; expected a single-key object")
    (kind, body), = raw.items()
    if kind == "key":
        return Key(body)
    if kind == "index":
        return Index(body)
    if kind == "wildcard":
        if body == "array":
            return ArrayWildcard()
        if body == "object":
            return ObjectWildcard()
        raise ValueError(f"unknown wildcard kind {body!r}")
    if kind == "filter":
        return Filter(expr_from_json(body))
    raise ValueError(f"unknown step kind {kind!r}")


def expr_from_json(raw: Any) -> Any:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError(f"bad filter expression {raw!r}")
    (kind, body), = raw.items()
    if kind == "exists":
        return exists(_rel_from_json(body))
    if kind == "eq":
        if not isinstance(body, list) or len(body) != 2:
            raise ValueError("eq takes [relative_path, scalar]")
        return eq(_rel_from_json(body[0]), body[1])
    if kind == "and":
        if not isinstance(body, list) or len(body) != 2:
            raise ValueError("and takes [left, right]")
        return and_(expr_from_json(body[0]), expr_from_json(body[1]))
    if kind == "or":
        if not isinstance(body, list) or len(body) != 2:
            raise ValueError("or takes [left, right]")
        return or_(expr_from_json(body[0]), expr_from_json(body[1]))
    if kind == "not":
        return not_(expr_from_json(body))
    raise ValueError(f"unknown expression kind {kind!r}")


def path_from_json(raw: Any) -> list[Any]:
    if not isinstance(raw, list):
        raise ValueError("path must be an array of steps")
    return [_step_from_json(item) for item in raw]
