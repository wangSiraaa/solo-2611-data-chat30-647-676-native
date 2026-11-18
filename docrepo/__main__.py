"""Command line entry point for the document repository.

All commands print one JSON object.  ``search`` and ``explain`` are the main
entry points; ``scan`` is the index-free reference used to verify them.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from jsonpath_service.codec import path_from_json

from .repository import (
    DocNotFound,
    QueryConflict,
    RepoError,
    Repository,
    StaleRevision,
)


def _read_text(inline: str | None, filename: str | None) -> str:
    if inline is not None:
        return inline
    if filename is None:
        raise SystemExit("provide an inline JSON argument or a file")
    with open(filename, encoding="utf-8") as fh:
        return fh.read()


def _result_json(results: list[Any]) -> list[dict[str, Any]]:
    return [
        {"doc_id": r.doc_id, "revision": r.revision, "hits": r.evidence()}
        for r in results
    ]


def _errors_json(errors: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"doc_id": doc_id, "error": error} for doc_id, error in errors]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Updateable document search repository.")
    sub = parser.add_subparsers(dest="command", required=True)

    def db_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", required=True, help="SQLite database path")

    p = sub.add_parser("replace", help="insert or replace a document")
    db_arg(p)
    p.add_argument("--doc-id", required=True)
    p.add_argument("--doc", help="document JSON inline")
    p.add_argument("--doc-file")
    p.add_argument("--expected-revision", type=int)

    p = sub.add_parser("delete", help="delete a document")
    db_arg(p)
    p.add_argument("--doc-id", required=True)
    p.add_argument("--expected-revision", type=int)

    p = sub.add_parser("register-query", help="register a named structured path query")
    db_arg(p)
    p.add_argument("--name", required=True)
    p.add_argument("--path", help="path JSON inline")
    p.add_argument("--path-file")
    p.add_argument("--mode", choices=["lax", "strict"], default="lax")

    p = sub.add_parser("register-schema", help="register a named schema document")
    db_arg(p)
    p.add_argument("--name", required=True)
    p.add_argument("--definition", help="schema JSON inline")
    p.add_argument("--definition-file")

    p = sub.add_parser("search", help="run a registered query (index-assisted)")
    db_arg(p)
    p.add_argument("--name", required=True)

    p = sub.add_parser("explain", help="candidates, exact evidence and full-scan comparison")
    db_arg(p)
    p.add_argument("--name", required=True)
    p.add_argument("--compare-scan", action="store_true", help="also run the index-free reference")

    p = sub.add_parser("scan", help="index-free reference for an arbitrary path")
    db_arg(p)
    p.add_argument("--path", help="path JSON inline")
    p.add_argument("--path-file")
    p.add_argument("--mode", choices=["lax", "strict"], default="lax")

    p = sub.add_parser("ensure-indexes", help="build pending index atoms")
    db_arg(p)

    p = sub.add_parser("stats", help="database and index statistics")
    db_arg(p)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "replace":
            document = json.loads(_read_text(args.doc, args.doc_file))
            with Repository(args.db) as repo:
                revision = repo.replace(
                    args.doc_id, document, expected_revision=args.expected_revision
                )
            print(json.dumps({"doc_id": args.doc_id, "revision": revision}))
            return 0

        if args.command == "delete":
            with Repository(args.db) as repo:
                removed = repo.delete(args.doc_id, expected_revision=args.expected_revision)
            print(json.dumps({"doc_id": args.doc_id, "deleted_revision": removed}))
            return 0

        if args.command == "register-query":
            path = path_from_json(json.loads(_read_text(args.path, args.path_file)))
            with Repository(args.db) as repo:
                q = repo.register_query(args.name, path, mode=args.mode)
            print(json.dumps({"name": q.name, "mode": q.mode, "created_revision": q.created_revision}))
            return 0

        if args.command == "register-schema":
            definition = json.loads(_read_text(args.definition, args.definition_file))
            with Repository(args.db) as repo:
                revision = repo.register_schema(args.name, definition)
            print(json.dumps({"name": args.name, "created_revision": revision}))
            return 0

        if args.command == "search":
            with Repository(args.db) as repo:
                results, errors = repo.search_with_errors(args.name)
            print(json.dumps({"query": args.name, "matches": _result_json(results), "errors": _errors_json(errors)}, ensure_ascii=False))
            return 0

        if args.command == "explain":
            with Repository(args.db) as repo:
                explanation = repo.explain(args.name)
                payload = explanation.to_json()
                if args.compare_scan:
                    query = repo.get_query(args.name)
                    scan_results, scan_errors = repo.scan_all(query.path, mode=query.mode)
                    payload["scan_reference"] = {
                        "matches": _result_json(scan_results),
                        "errors": _errors_json(scan_errors),
                        "identical": _same(_result_json(scan_results), payload["matches"])
                        and _same(_errors_json(scan_errors), payload["errors"]),
                    }
            print(json.dumps(payload, ensure_ascii=False))
            return 0

        if args.command == "scan":
            path = path_from_json(json.loads(_read_text(args.path, args.path_file)))
            with Repository(args.db) as repo:
                results, errors = repo.scan_all(path, mode=args.mode)
            print(json.dumps({"matches": _result_json(results), "errors": _errors_json(errors)}, ensure_ascii=False))
            return 0

        if args.command == "ensure-indexes":
            with Repository(args.db) as repo:
                scanned = repo.ensure_indexes()
                stats = repo.index_stats()
            print(json.dumps({"documents_scanned": scanned, "index": stats}, ensure_ascii=False))
            return 0

        if args.command == "stats":
            with Repository(args.db) as repo:
                payload = {
                    "documents": repo.count(),
                    "queries": repo.list_queries(),
                    "schemas": repo.list_schemas(),
                    "index": repo.index_stats(),
                }
            print(json.dumps(payload, ensure_ascii=False))
            return 0

    except (StaleRevision, QueryConflict, DocNotFound, RepoError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 1

    parser.error("unknown command")
    return 2


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(
        right, sort_keys=True, ensure_ascii=False
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
