"""End-to-end demo of both entry points.

Run with::

    python3 -m examples.demo

It prints, step by step:

1. the standalone path service on tricky documents (missing / null / empty
   array, negative index, object order, strict local error, three-valued
   filters);
2. the document repository: register queries, index-assisted search against
   the index-free reference, a field deletion that removes an index hit, a
   stale write that is rejected, an array move that changes evidence, a delete,
   and a close/reopen that preserves named queries and results.
"""

from __future__ import annotations

import json
import os
import tempfile

from docrepo import Repository, StaleRevision
from jsonpath_service.engine import (
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

BOLD = "\033[1m"
RESET = "\033[0m"


def section(title: str) -> None:
    print(f"\n{BOLD}== {title} =={RESET}")


def show(label: str, value: object) -> None:
    print(f"{label}: {json.dumps(value, ensure_ascii=False, default=str)}")


def engine_demo() -> None:
    section("1. Standalone path engine")

    doc = {
        "a": [{"x": 1}, {"x": None}, {"y": 2}],
        "empty": [],
        "tags": ["x", "y"],
        "obj": {"b": 1, "A": 2, "a": 3},
        "matrix": [[1, 2], [3]],
    }

    show(
        "a[*].x (lax, missing skipped)",
        [{"path": list(h.path), "value": h.value}
         for h in evaluate(doc, [Key("a"), ArrayWildcard(), Key("x")])],
    )
    try:
        evaluate(doc, [Key("a"), ArrayWildcard(), Key("x")], strict=True)
    except PathError as exc:
        show("strict error (locatable)", {
            "reason": exc.reason,
            "step_index": exc.step_index,
            "hit_path": list(exc.hit_path),
            "hit_value_type": exc.hit_value_type,
        })

    show("missing key", [list(h.path) for h in evaluate({}, [Key("a")])])
    show("explicit null", [{"path": list(h.path), "value": h.value}
                           for h in evaluate({"a": None}, [Key("a")])])
    show("empty array wildcard", [list(h.path) for h in evaluate({"empty": []},
                                                                  [Key("empty"), ArrayWildcard()])])
    show("empty array object itself", [h.value for h in evaluate({"empty": []}, [Key("empty")])])
    show("negative index", [{"path": list(h.path), "value": h.value}
                            for h in evaluate(doc, [Key("tags"), Index(-1)])])
    show("object wildcard code-point order",
         [list(h.path) for h in evaluate(doc, [Key("obj"), ObjectWildcard()])])

    items = {"items": [{"kind": "alpha", "n": 1}, {"kind": "beta"},
                       {"kind": "alpha"}, {"n": 2}, {"kind": "alpha", "n": None}]}
    show("eq filter keeps only true", [list(h.path) for h in evaluate(
        items, [Key("items"), Filter(eq([Key("kind")], "alpha"))])])
    show("exists counts null as present", [list(h.path) for h in evaluate(
        items, [Key("items"), Filter(exists([Key("n")]))])])
    show("eq(..., null) only matches null", [list(h.path) for h in evaluate(
        items, [Key("items"), Filter(eq([Key("n")], None))])])
    show("unknown OR unknown drops element", [list(h.path) for h in evaluate(
        {"items": [{}]},
        [Key("items"), Filter(or_(eq([Key("k")], 1), not_(eq([Key("k")], 1))))])])
    show("conditions never cross elements", [list(h.path) for h in evaluate(
        {"items": [{"x": 1}, {"y": 1}]},
        [Key("items"), Filter(and_(exists([Key("x")]), eq([Key("y")], 1)))])])


def repo_demo(db_path: str) -> None:
    section("2. Repository: register, index, exact evidence")
    with Repository(db_path) as repo:
        repo.replace("cust-1", {
            "tenant": "acme",
            "items": [{"kind": "alpha", "n": 1}, {"kind": "beta"}],
        })
        repo.replace("cust-2", {
            "tenant": "acme",
            "items": [{"kind": "alpha"}, {"kind": "alpha", "n": 2}],
        })
        repo.replace("cust-3", {"tenant": "beta", "items": []})

        path = [Key("items"), Filter(and_(eq([Key("kind")], "alpha"), exists([Key("n")])))]
        repo.register_query("alpha-with-n", path)
        repo.register_schema("customer", {"required": ["tenant", "items"]})
        print("index build scanned:", repo.ensure_indexes(), "documents")

        explanation = repo.explain("alpha-with-n")
        show("explain", {
            "total": explanation.total_documents,
            "candidates": explanation.candidate_documents,
            "matched": explanation.matched_documents,
            "used_index": explanation.used_index,
            "plan_atoms": [p.get("atom") for p in explanation.index_plan],
            "matches": [
                {"doc_id": m.doc_id, "hits": m.evidence()} for m in explanation.matches
            ],
        })

        indexed = repo.search("alpha-with-n")
        scanned, _ = repo.scan_all(path)
        show("indexed search == scan_all (doc ids + paths)",
             [r.doc_id for r in indexed] == [r.doc_id for r in scanned]
             and [h.path for r in indexed for h in r.hits]
             == [h.path for r in scanned for h in r.hits])

    section("3. Field deletion removes the old index hit")
    with Repository(db_path) as repo:
        repo.replace("cust-1", {"tenant": "acme", "items": [{"kind": "beta"}]})
        show("after deletion", [r.doc_id for r in repo.search("alpha-with-n")])
        show("scan reference", [r.doc_id for r in repo.scan_all(path)[0]])

    section("4. Optimistic concurrency: an old revision cannot overwrite")
    with Repository(db_path) as repo:
        revision = repo.replace("cust-2", {"tenant": "acme", "items": [{"kind": "alpha", "n": 99}]})
        late = revision - 1
        try:
            repo.replace("cust-2", {"tenant": "acme", "items": []}, expected_revision=late)
        except StaleRevision as exc:
            show("stale replace rejected", {"expected": exc.expected, "actual": exc.actual})
        show("current revision", repo.get("cust-2")[0])

    section("5. Array move changes evidence, not stale positions")
    with Repository(db_path) as repo:
        repo.replace("cust-2", {"tenant": "acme", "items": [{"kind": "beta"}, {"kind": "alpha", "n": 2}]})
        result = repo.search("alpha-with-n")
        show("evidence after move", [
            {"doc_id": r.doc_id, "paths": [list(h.path) for h in r.hits]} for r in result
        ])
        show("matches scan_all", [
            [h.path for h in r.hits] for r in result
        ] == [[h.path for h in r.hits] for r in repo.scan_all(path)[0]])

    section("6. Delete then reopen")
    with Repository(db_path) as repo:
        repo.delete("cust-3", expected_revision=1)
    with Repository(db_path) as repo:
        show("after reopen: queries", repo.list_queries())
        show("after reopen: schemas", repo.list_schemas())
        show("after reopen: documents", repo.document_ids())
        show("after reopen: search", [
            {"doc_id": r.doc_id, "paths": [list(h.path) for h in r.hits]}
            for r in repo.search("alpha-with-n")
        ])


def main() -> None:
    engine_demo()
    fd, path = tempfile.mkstemp(prefix="docrepo-demo-", suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        repo_demo(path)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass
    print(f"\n{BOLD}demo complete{RESET}")


if __name__ == "__main__":
    main()
