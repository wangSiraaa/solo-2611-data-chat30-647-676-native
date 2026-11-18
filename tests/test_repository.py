"""Tests for the updateable document repository."""

from __future__ import annotations

import random
import unittest

from docrepo import (
    DocNotFound,
    QueryConflict,
    Repository,
    StaleRevision,
)
from jsonpath_service.engine import (
    ArrayWildcard,
    Filter,
    Index,
    Key,
    ObjectWildcard,
    and_,
    eq,
    exists,
    not_,
    or_,
)
from tests.helpers import make_document, result_payload, search_signature, temp_db


class RevisionTest(unittest.TestCase):
    def test_replace_increments_revision(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            self.assertEqual(repo.replace("a", {"v": 1}), 1)
            self.assertEqual(repo.replace("a", {"v": 2}), 2)
            self.assertEqual(repo.get("a"), (2, {"v": 2}))
            self.assertEqual(repo.count(), 1)

    def test_stale_write_rejected(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("a", {"v": 1})
            repo.replace("a", {"v": 2}, expected_revision=1)
            with self.assertRaises(StaleRevision):
                repo.replace("a", {"v": 3}, expected_revision=1)
            self.assertEqual(repo.get("a"), (2, {"v": 2}))
            # History records only successful writes.
            self.assertEqual([h["revision"] for h in repo.history("a")], [1, 2])

    def test_expected_zero_means_create_only(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("a", {"v": 1}, expected_revision=0)
            with self.assertRaises(StaleRevision):
                repo.replace("a", {"v": 2}, expected_revision=0)

    def test_delete_and_stale_delete(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("a", {"v": 1})
            with self.assertRaises(StaleRevision):
                repo.delete("a", expected_revision=5)
            self.assertEqual(repo.delete("a", expected_revision=1), 1)
            self.assertIsNone(repo.get("a"))
            with self.assertRaises(DocNotFound):
                repo.delete("a")

    def test_failed_replace_does_not_touch_index(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("a", {"items": [{"k": "r"}]})
            repo.register_query("q", [Key("items"), Filter(eq([Key("k")], "r"))])
            repo.ensure_indexes()
            with self.assertRaises(StaleRevision):
                repo.replace("a", {"items": [{"k": "nope"}]}, expected_revision=99)
            self.assertEqual([r.doc_id for r in repo.search("q")], ["a"])


class QueryRegistryTest(unittest.TestCase):
    def test_register_idempotent_and_conflict(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            p = [Key("a")]
            repo.register_query("q", p)
            repo.register_query("q", [Key("a")])
            with self.assertRaises(QueryConflict):
                repo.register_query("q", [Key("b")])
            with self.assertRaises(QueryConflict):
                repo.get_query("missing")

    def test_persisted_across_reopen(self) -> None:
        with temp_db() as path:
            with Repository(path) as repo:
                repo.replace("a", {"items": [{"k": "r"}, {"k": "s"}]})
                repo.register_query("q", [Key("items"), Filter(eq([Key("k")], "r"))])
                repo.register_schema("s", {"required": ["items"]})
                repo.ensure_indexes()
            with Repository(path) as repo:
                self.assertEqual(repo.list_queries(), ["q"])
                self.assertEqual(repo.list_schemas(), ["s"])
                self.assertEqual(repo.get_schema("s"), ({"required": ["items"]}, 1))
                results = repo.search("q")
                self.assertEqual([r.doc_id for r in results], ["a"])
                self.assertEqual([h.path for h in results[0].hits], [("items", 0)])

    def test_schema_conflict(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.register_schema("s", {"a": 1})
            with self.assertRaises(QueryConflict):
                repo.register_schema("s", {"a": 2})


class UpdateSemanticsTest(unittest.TestCase):
    def test_removed_field_leaves_no_index_hit(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("d", {"items": [{"k": "r"}, {"k": "s"}]})
            repo.register_query("q", [Key("items"), Filter(eq([Key("k")], "r"))])
            repo.ensure_indexes()
            self.assertEqual([r.doc_id for r in repo.search("q")], ["d"])
            # Remove the field that the index matched.
            repo.replace("d", {"items": [{"k": "s"}]})
            self.assertEqual(repo.search("q"), [])
            self.assertEqual(repo.scan_all([Key("items"), Filter(eq([Key("k")], "r"))])[0], [])

    def test_delete_removes_hits(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("d", {"items": [{"k": "r"}]})
            repo.register_query("q", [Key("items"), Filter(eq([Key("k")], "r"))])
            repo.ensure_indexes()
            repo.delete("d")
            self.assertEqual(repo.search("q"), [])

    def test_array_move_changes_evidence_path(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("d", {"items": [{"k": "r", "v": 1}, {"k": "s"}]})
            query = [Key("items"), Filter(eq([Key("k")], "r"))]
            repo.register_query("q", query)
            repo.ensure_indexes()
            first = repo.search("q")
            self.assertEqual([h.path for h in first[0].hits], [("items", 0)])
            # Move the matching element; the same value must not keep index 0.
            repo.replace("d", {"items": [{"k": "s"}, {"k": "r", "v": 1}]})
            moved = repo.search("q")
            self.assertEqual([h.path for h in moved[0].hits], [("items", 1)])
            scan = repo.scan_all(query)[0]
            self.assertEqual(search_signature(moved), search_signature(scan))

    def test_duplicate_equal_values_are_all_reported(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("d", {"items": [{"k": "r"}, {"k": "r"}, {"k": "s"}]})
            query = [Key("items"), Filter(eq([Key("k")], "r"))]
            repo.register_query("q", query)
            repo.ensure_indexes()
            self.assertEqual(
                [h.path for h in repo.search("q")[0].hits], [("items", 0), ("items", 1)]
            )

    def test_missing_vs_null_vs_empty(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("missing", {"items": [{}]})
            repo.replace("null", {"items": [{"x": None}]})
            repo.replace("empty", {"items": [{"x": []}]})

            eq_null = repo.scan_all(
                [Key("items"), Filter(eq([Key("x")], None))]
            )[0]
            self.assertEqual([r.doc_id for r in eq_null], ["null"])

            exists_x = repo.scan_all(
                [Key("items"), Filter(exists([Key("x")]))]
            )[0]
            # exists counts explicit null and empty array as present.
            self.assertEqual([r.doc_id for r in exists_x], ["empty", "null"])

            # The three cases also stay distinct as direct path hits.
            missing_hits = repo.scan_all([Key("items"), ArrayWildcard(), Key("x")])[0]
            self.assertEqual([r.doc_id for r in missing_hits], ["empty", "null"])
            empty_array = repo.scan_all(
                [Key("items"), ArrayWildcard(), Key("x"), ArrayWildcard()]
            )[0]
            self.assertEqual(empty_array, [])

    def test_strict_mode_errors_match_scan(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("ok", {"items": [{"k": "r"}]})
            repo.replace("bad", {"items": [{"k": "r"}, {"v": 1}]})
            path_steps = [Key("items"), ArrayWildcard(), Key("k")]
            repo.register_query("q", path_steps, mode="strict")
            repo.ensure_indexes()
            results, errors = repo.search_with_errors("q")
            scan_results, scan_errors = repo.scan_all(path_steps, mode="strict")
            self.assertEqual(search_signature(results), search_signature(scan_results))
            self.assertEqual(errors, scan_errors)
            self.assertEqual([d for d, _ in errors], ["bad"])

    def test_bulk_load_and_reopen(self) -> None:
        with temp_db() as path:
            with Repository(path) as repo:
                docs = [(f"d{i}", make_document(i)) for i in range(20)]
                self.assertEqual(repo.bulk_load(docs), 20)
                repo.register_query("q", [Key("items"), Filter(eq([Key("kind")], "alpha"))])
                repo.ensure_indexes()
                before = search_signature(repo.search("q"))
            with Repository(path) as repo:
                self.assertEqual(repo.count(), 20)
                self.assertEqual(search_signature(repo.search("q")), before)


class IndexDifferentialTest(unittest.TestCase):
    """Index-assisted search must equal the index-free reference exactly."""

    def test_random_documents_and_queries(self) -> None:
        queries = {
            "eq": [Key("items"), Filter(eq([Key("kind")], "alpha"))],
            "and": [
                Key("items"),
                Filter(and_(eq([Key("kind")], "alpha"), eq([Key("score")], 5))),
            ],
            "or": [
                Key("items"),
                Filter(or_(eq([Key("kind")], "alpha"), eq([Key("kind")], "beta"))),
            ],
            "not": [Key("items"), Filter(not_(eq([Key("kind")], "alpha")))],
            "exists": [Key("items"), Filter(exists([Key("note")]))],
            "null": [Key("items"), Filter(eq([Key("score")], None))],
            "wild": [Key("items"), ArrayWildcard(), Key("score")],
            "neg": [Key("items"), Index(-1)],
            "obj": [Key("customer"), ObjectWildcard()],
        }
        with temp_db() as path, Repository(path) as repo:
            for name, path_steps in queries.items():
                repo.register_query(name, path_steps)
            repo.ensure_indexes()
            rng = random.Random(11)
            docs = []
            for i in range(200):
                doc = make_document(i, items=rng.randint(0, 6))
                if i % 5 == 0:
                    del doc["items"]
                docs.append((f"d{i:04d}", doc))
            repo.bulk_load(docs, chunk_size=32)

            for name, path_steps in queries.items():
                with self.subTest(query=name):
                    indexed = repo.search(name)
                    reference, _ = repo.scan_all(path_steps)
                    self.assertEqual(
                        result_payload(indexed), result_payload(reference)
                    )

    def test_index_reduces_candidates(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            for i in range(50):
                repo.replace(
                    f"d{i}",
                    {"items": [{"kind": "alpha" if i == 7 else "beta", "score": i}]},
                )
            repo.register_query("q", [Key("items"), Filter(eq([Key("kind")], "alpha"))])
            repo.ensure_indexes()
            explanation = repo.explain("q")
            self.assertTrue(explanation.used_index)
            self.assertLess(explanation.candidate_documents, explanation.total_documents)
            self.assertEqual(explanation.matched_documents, 1)

    def test_pending_index_falls_back_to_full_scan_with_same_answer(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace("a", {"items": [{"k": "r"}]})
            repo.register_query("q", [Key("items"), Filter(eq([Key("k")], "r"))])
            # No ensure_indexes: must still return the exact answer.
            before = repo.search("q")
            self.assertEqual([r.doc_id for r in before], ["a"])
            self.assertFalse(repo.explain("q").used_index)
            repo.ensure_indexes()
            after = repo.search("q")
            self.assertEqual(search_signature(before), search_signature(after))

    def test_repeated_value_in_document(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.replace(
                "d",
                {"items": [{"k": "r", "v": 1}, {"k": "r", "v": 2}, {"k": "s"}]},
            )
            query = [Key("items"), Filter(eq([Key("k")], "r"))]
            repo.register_query("q", query)
            repo.ensure_indexes()
            self.assertEqual(
                [h.path for h in repo.search("q")[0].hits],
                [("items", 0), ("items", 1)],
            )
            self.assertEqual(
                result_payload(repo.search("q")), result_payload(repo.scan_all(query)[0])
            )

    def test_rebuild_indexes_preserves_answers(self) -> None:
        with temp_db() as path, Repository(path) as repo:
            repo.bulk_load((f"d{i}", make_document(i)) for i in range(30))
            query = [Key("items"), Filter(eq([Key("kind")], "alpha"))]
            repo.register_query("q", query)
            repo.ensure_indexes()
            before = result_payload(repo.search("q"))
            repo.rebuild_indexes()
            after = result_payload(repo.search("q"))
            self.assertEqual(before, after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
