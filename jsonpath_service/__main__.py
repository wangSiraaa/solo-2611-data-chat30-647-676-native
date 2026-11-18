"""Command line entry point for the standalone path engine.

Examples
--------
    python -m jsonpath_service --doc '{"a":[{"x":1},{"x":null},{"y":2}]}' \
        --path '[{"key":"a"},{"wildcard":"array"},{"key":"x"}]' --mode lax

    python -m jsonpath_service --doc-file doc.json --path-file path.json --strict

The path is a JSON array of structured steps; the accepted step encodings are
``{"key": "..."}``, ``{"index": n}``, ``{"wildcard": "array"}``,
``{"wildcard": "object"}`` and ``{"filter": <expr>}``.  Filter expressions use
``{"exists": rel}``, ``{"eq": [rel, scalar]}``, ``{"and": [a, b]}``,
``{"or": [a, b]}`` and ``{"not": a}`` where ``rel`` is itself an array of
``{"key": ...}`` / ``{"index": ...}`` steps.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .engine import (
    ArrayWildcard,
    Filter,
    Index,
    Key,
    ObjectWildcard,
    PathError,
    and_,
    eq,
    evaluate,
    exists,
    not_,
    or_,
)


def parse_step(raw: Any) -> Any:
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
        return Filter(parse_expr(body))
    raise ValueError(f"unknown step kind {kind!r}")


def parse_rel(raw: Any) -> tuple[Any, ...]:
    if not isinstance(raw, list):
        raise ValueError("relative path must be an array of steps")
    steps = []
    for item in raw:
        step = parse_step(item)
        if not isinstance(step, (Key, Index)):
            raise ValueError("relative path steps must be key or index")
        steps.append(step)
    return tuple(steps)


def parse_expr(raw: Any) -> Any:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError(f"bad filter expression {raw!r}")
    (kind, body), = raw.items()
    if kind == "exists":
        return exists(parse_rel(body))
    if kind == "eq":
        if not isinstance(body, list) or len(body) != 2:
            raise ValueError("eq takes [relative_path, scalar]")
        return eq(parse_rel(body[0]), body[1])
    if kind == "and":
        return and_(parse_expr(body[0]), parse_expr(body[1]))
    if kind == "or":
        return or_(parse_expr(body[0]), parse_expr(body[1]))
    if kind == "not":
        return not_(parse_expr(body))
    raise ValueError(f"unknown expression kind {kind!r}")


def parse_path(raw: Any) -> list[Any]:
    if not isinstance(raw, list):
        raise ValueError("path must be an array of steps")
    return [parse_step(item) for item in raw]


def hit_to_json(hit: Any) -> dict[str, Any]:
    return {"path": list(hit.path), "value": hit.value}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate a structured JSON path against a document.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--doc", help="document JSON on the command line")
    group.add_argument("--doc-file", help="path to a document JSON file")
    parser.add_argument("--path", help="path JSON on the command line")
    parser.add_argument("--path-file", help="path to a path JSON file")
    parser.add_argument("--strict", action="store_true", help="raise on the first type/missing/range error")
    args = parser.parse_args(argv)

    if args.doc is not None:
        document = json.loads(args.doc)
    else:
        with open(args.doc_file, encoding="utf-8") as fh:
            document = json.load(fh)

    raw_path = args.path
    if raw_path is None:
        if not args.path_file:
            parser.error("provide --path or --path-file")
        with open(args.path_file, encoding="utf-8") as fh:
            raw_path = fh.read()

    try:
        path = parse_path(json.loads(raw_path))
        hits = evaluate(document, path, strict=args.strict)
    except (PathError, ValueError, TypeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps({"hits": [hit_to_json(h) for h in hits]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
