"""End-to-end tests covering the scenarios named in the brief.

Each test uses a small synthetic corpus and checks the **complete** doc id set
and original-path evidence, not just counts.
"""

from __future__ import annotations

import unittest

from docrepo import Repository
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

from tests.helpers import make_document, result_payload, temp_db


def _payload(results) -> list[dict]:
    return [
        {"doc_id": r.doc_id, "revision": r.revision, "paths": [list(h.path) for h in r.hits],
         "values": [h.value for h in r.hits]}
        for r in results
    ]


class MissingNullEmptyTest(unittest.TestCase):
    """Missing field, explicit null and empty array must remain distinct."""

    def test_engine_and_repository(self) -> None:
        docs = {
            "missing": {"items": [{}]},
            "null": {"items": [{"x": None}]},
            "empty": {"items": [{"x": []}]},
            "scalar": {"items": [{"x": 0}]},
        }
        with temp_db() as db, Repository(db) as repo:
            repo.bulk_load(sorted(docs.items()))
            # Direct wildcard path: empty array yields nothing, null yields a hit.
            results, _ = repo.scan_all([Key("items"), ArrayWildcard(), Key("x")])
            self.assertEqual(
                _payload(results),
                [
                    {"doc_id": "empty", "revision": 1, "paths": [["items", 0, "x"]], "values": [[]]},
                    {"doc_id": "null", "revision": 1, "paths": [["items", 0, "x"]], "values": [None]},
                    {"doc_id": "scalar", "revision": 1, "paths": [["items", 0, "x"]], "values": [0]},
                ],
            )
            # eq(null) matches only the explicit null, not missing nor 0.
            repo.register_query("is_null", [Key("items"), Filter(eq([Key("x")], None))])
            repo.ensure_indexes()
            self.assertEqual(
                [r.doc_id for r in repo.search("is_null")], ["null"]
            )
            # exists matches null and empty array but not missing.
            repo.register_query("has_x", [Key("items"), Filter(exists([Key("x")]))])
            self.assertEqual(
                [r.doc_id for r in repo.search("has_x")], ["empty", "null", "scalar"]
            )


class RepeatedValueTest(unittest.TestCase):
    def test_same_value_twice_is_two_hits(self) -> None:
        doc = {"items": [{"k": "r"}, {"k": "r"}, {"k": "s"}]}
        with temp_db() as db, Repository(db) as repo:
            repo.replace("d", doc)
            path = [Key("items"), Filter(eq([Key("k")], "r"))]
            repo.register_query("q", path)
            repo.ensure_indexes()
            self.assertEqual(
                [list(h.path) for h in repo.search("q")[0].hits],
                [["items", 0], ["items", 1]],
            )
            self.assertEqual(result_payload(repo.search("q")),
                             result_payload(repo.scan_all(path)[0]))


class NegativeIndexTest(unittest.TestCase):
    def test_negative_index_after_shrink(self) -> None:
        with temp_db() as db, Repository(db) as repo:
            repo.replace("d", {"tags": ["a", "b", "c"]})
            path = [Key("tags"), Index(-1)]
            repo.register_query("q", path)
            self.assertEqual([h.value for h in repo.search("q")[0].hits], ["c"])
            repo.replace("d", {"tags": ["a", "b"]})
            self.assertEqual([h.value for h in repo.search("q")[0].hits], ["b"])
            repo.replace("d", {"tags": []})
            self.assertEqual(repo.search("q"), [])


class NestedDifferentElementsTest(unittest.TestCase):
    def test_no_cross_element_conjunction(self) -> None:
        doc = {"groups": [
            {"members": [{"x": 1}, {"y": 1}]},
            {"members": [{"x": 1, "y": 1}]},
            {"members": []},
        ]}
        path = [
            Key("groups"),
            Filter(exists([Key("members"), ArrayWildcard(), Key("x")])),
            Key("members"),
            Filter(and_(eq([Key("x")], 1), eq([Key("y")], 1))),
        ]
        hits = evaluate(doc, path)
        # Only the second group's single element has both x and y.
        self.assertEqual([h.path for h in hits], [("groups", 1, "members", 0)])
        # The first group has a member with x and a different member with y;
        # it must not produce a hit.
        self.assertNotIn(("groups", 0), [h.path[:2] for h in hits])


class StrictLocalErrorTest(unittest.TestCase):
    def test_error_location(self) -> None:
        doc = {"a": [{"b": 1}, {"c": 2}, {"b": 3}]}
        with self.assertRaises(PathError) as ctx:
            evaluate(doc, [Key("a"), ArrayWildcard(), Key("b")], strict=True)
        err = ctx.exception
        self.assertEqual(err.reason, "missing_key")
        self.assertEqual(err.step_index, 2)
        self.assertEqual(err.hit_path, ("a", 1))

    def test_repository_strict_matches_scan(self) -> None:
        with temp_db() as db, Repository(db) as repo:
            for i in range(10):
                doc = make_document(i)
                if i % 3 == 0:
                    doc["items"][0].pop("kind", None)
                repo.replace(f"d{i}", doc)
            path = [Key("items"), ArrayWildcard(), Key("kind")]
            repo.register_query("s", path, mode="strict")
            results, errors = repo.search_with_errors("s")
            ref_results, ref_errors = repo.scan_all(path, mode="strict")
            self.assertEqual(_payload(results), _payload(ref_results))
            self.assertEqual(errors, ref_errors)
            self.assertTrue(errors)


class AsyncUpdateTest(unittest.TestCase):
    """Search before and after an update; evidence must follow the data."""

    def test_before_and_after(self) -> None:
        path = [Key("items"), Filter(and_(eq([Key("kind")], "alpha"), exists([Key("n")])))]
        with temp_db() as db, Repository(db) as repo:
            repo.replace("d", {"items": [{"kind": "alpha"}]})  # no n: no match
            repo.register_query("q", path)
            repo.ensure_indexes()
            self.assertEqual(repo.search("q"), [])
            # Async-style update: replace after the index was built.
            revision = repo.replace("d", {"items": [{"kind": "alpha", "n": 7}]})
            result = repo.search("q")
            self.assertEqual([r.doc_id for r in result], ["d"])
            self.assertEqual([list(h.path) for h in result[0].hits], [["items", 0]])
            self.assertEqual(result[0].revision, revision)
            # Update again, moving the field to another index.
            repo.replace("d", {"items": [{"kind": "beta"}, {"kind": "alpha", "n": 8}]})
            moved = repo.search("q")
            self.assertEqual([list(h.path) for h in moved[0].hits], [["items", 1]])
            self.assertEqual(_payload(moved), _payload(repo.scan_all(path)[0]))
            # Remove the field: match disappears.
            repo.replace("d", {"items": [{"kind": "beta"}, {"kind": "alpha"}]})
            self.assertEqual(repo.search("q"), [])


class CloseReopenTest(unittest.TestCase):
    def test_full_state_survives(self) -> None:
        path = [Key("items"), Filter(eq([Key("kind")], "alpha"))]
        with temp_db() as db:
            with Repository(db) as repo:
                repo.bulk_load((f"d{i}", make_document(i)) for i in range(25))
                repo.register_query("q", path)
                repo.register_schema("customer", {"type": "object"})
                repo.ensure_indexes()
                before = _payload(repo.search("q"))
            with Repository(db) as repo:
                self.assertEqual(repo.count(), 25)
                self.assertEqual(repo.list_queries(), ["q"])
                self.assertEqual(repo.list_schemas(), ["customer"])
                self.assertEqual(_payload(repo.search("q")), before)
                self.assertEqual(
                    _payload(repo.scan_all(path)[0]),
                    _payload(repo.search("q")),
                )


class ObjectKeyOrderTest(unittest.TestCase):
    def test_key_order_is_code_point(self) -> None:
        doc = {"o": {"b": 1, "a": 2, "B": 3, "é": 4, "1": 5}}
        hits = evaluate(doc, [Key("o"), ObjectWildcard()])
        self.assertEqual([h.path[1] for h in hits], ["1", "B", "a", "b", "é"])
        with temp_db() as db, Repository(db) as repo:
            repo.replace("d", doc)
            repo.register_query("q", [Key("o"), ObjectWildcard()])
            self.assertEqual(
                [h.path[1] for h in repo.search("q")[0].hits],
                ["1", "B", "a", "b", "é"],
            )


class EvidenceMatchTest(unittest.TestCase):
    """Index-assisted search equals direct engine runs on full evidence."""

    def test_wide_corpus(self) -> None:
        queries_map = {
            "eq": [Key("items"), Filter(eq([Key("kind")], "alpha"))],
            "and": [Key("items"), Filter(and_(eq([Key("kind")], "alpha"),
                                              eq([Key("grade")], 2)))],
            "or": [Key("items"), Filter(or_(eq([Key("kind")], "alpha"),
                                            eq([Key("kind")], "gamma")))],
            "not": [Key("items"), Filter(not_(eq([Key("kind")], "alpha")))],
            "exists": [Key("items"), Filter(exists([Key("maybe")]))],
            "null": [Key("items"), Filter(eq([Key("maybe")], None))],
            "wildcard": [Key("items"), ArrayWildcard(), Key("kind")],
            "object": [Key("labels"), ObjectWildcard()],
        }
        with temp_db() as db, Repository(db) as repo:
            for name, path in queries_map.items():
                repo.register_query(name, path)
            repo.ensure_indexes()
            repo.bulk_load((f"d{i:03d}", make_document(i)) for i in range(150))
            for name, path in queries_map.items():
                with self.subTest(query=name):
                    indexed = repo.search(name)
                    ref = repo.scan_all(path)[0]
                    self.assertEqual(_payload(indexed), _payload(ref))
                    # Also compare against the standalone engine on payloads.
                    direct = []
                    for doc_id, revision, document in repo.iter_documents():
                        hits = evaluate(document, path)
                        if hits:
                            direct.append((doc_id, revision,
                                           [list(h.path) for h in hits],
                                           [h.value for h in hits]))
                    self.assertEqual(
                        [(r.doc_id, r.revision, [list(h.path) for h in r.hits],
                          [h.value for h in r.hits]) for r in indexed],
                        direct,
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
