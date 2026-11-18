"""Tests for the standalone structured path engine."""

from __future__ import annotations

import unittest

from jsonpath_service.codec import path_from_json, path_to_json
from jsonpath_service.engine import (
    UNKNOWN,
    ArrayWildcard,
    Filter,
    Hit,
    Index,
    Key,
    ObjectWildcard,
    PathError,
    and_,
    eq,
    evaluate,
    evaluate_filter,
    exists,
    not_,
    or_,
)
from tests.helpers import hit_paths


class StepSemanticsTest(unittest.TestCase):
    def test_key_only_matches_objects(self) -> None:
        self.assertEqual(evaluate({"a": 1}, [Key("a")]), [Hit(("a",), 1)])
        self.assertEqual(evaluate([1, 2], [Key("a")], strict=False), [])
        with self.assertRaises(PathError) as ctx:
            evaluate([1, 2], [Key("a")], strict=True)
        self.assertEqual(ctx.exception.reason, "type")
        self.assertEqual(ctx.exception.step_index, 0)

    def test_missing_key_distinct_from_null_and_empty_array(self) -> None:
        missing = evaluate({}, [Key("a")])
        explicit_null = evaluate({"a": None}, [Key("a")])
        empty = evaluate({"a": []}, [Key("a"), ArrayWildcard()])
        self.assertEqual(missing, [])
        self.assertEqual(explicit_null, [Hit(("a",), None)])
        self.assertEqual(empty, [])
        # The null hit is distinguishable from "nothing", and the empty array
        # is distinguishable because the key itself is still a hit.
        self.assertEqual(evaluate({"a": []}, [Key("a")]), [Hit(("a",), [])])

    def test_negative_index(self) -> None:
        doc = {"a": [10, 20, 30]}
        self.assertEqual(evaluate(doc, [Key("a"), Index(-1)]), [Hit(("a", 2), 30)])
        self.assertEqual(evaluate(doc, [Key("a"), Index(-3)]), [Hit(("a", 0), 10)])
        self.assertEqual(evaluate(doc, [Key("a"), Index(-4)]), [])
        with self.assertRaises(PathError) as ctx:
            evaluate(doc, [Key("a"), Index(-4)], strict=True)
        self.assertEqual(ctx.exception.reason, "index_out_of_range")
        self.assertEqual(ctx.exception.step_index, 1)
        self.assertEqual(ctx.exception.hit_path, ("a",))

    def test_array_wildcard_preserves_order_and_duplicates(self) -> None:
        doc = {"a": [3, 1, 3]}
        hits = evaluate(doc, [Key("a"), ArrayWildcard()])
        self.assertEqual(
            hits, [Hit(("a", 0), 3), Hit(("a", 1), 1), Hit(("a", 2), 3)]
        )

    def test_object_wildcard_code_point_order(self) -> None:
        doc = {"o": {"b": 1, "A": 2, "a": 3, "é": 4}}
        hits = evaluate(doc, [Key("o"), ObjectWildcard()])
        self.assertEqual(
            hit_paths(hits),
            [("o", "A"), ("o", "a"), ("o", "b"), ("o", "é")],
        )

    def test_lax_skips_branch_not_wraps_scalars(self) -> None:
        doc = {"a": [{"b": 1}, 5, {"b": 2}], "c": 7}
        self.assertEqual(
            evaluate(doc, [Key("a"), ArrayWildcard(), Key("b")]),
            [Hit(("a", 0, "b"), 1), Hit(("a", 2, "b"), 2)],
        )
        # A scalar is never implicitly wrapped into an array.
        self.assertEqual(evaluate(doc, [Key("c"), ArrayWildcard()]), [])
        self.assertEqual(evaluate(doc, [Key("c"), Index(0)]), [])

    def test_lax_never_splits_arrays(self) -> None:
        # There is no implicit flattening across array boundaries.
        doc = {"a": [[1, 2], [3]]}
        self.assertEqual(
            evaluate(doc, [Key("a"), ArrayWildcard(), ArrayWildcard()]),
            [
                Hit(("a", 0, 0), 1),
                Hit(("a", 0, 1), 2),
                Hit(("a", 1, 0), 3),
            ],
        )
        self.assertEqual(evaluate(doc, [Key("a"), Key("b")]), [])

    def test_strict_reports_first_local_error(self) -> None:
        doc = {"a": [{"b": 1}, {"c": 2}]}
        with self.assertRaises(PathError) as ctx:
            evaluate(doc, [Key("a"), ArrayWildcard(), Key("b")], strict=True)
        err = ctx.exception
        self.assertEqual(err.reason, "missing_key")
        self.assertEqual(err.step_index, 2)
        self.assertEqual(err.hit_path, ("a", 1))
        self.assertEqual(err.hit_value_type, "object")

    def test_strict_type_error_location(self) -> None:
        with self.assertRaises(PathError) as ctx:
            evaluate({"a": 1}, [Key("a"), ArrayWildcard()], strict=True)
        err = ctx.exception
        self.assertEqual((err.reason, err.step_index, err.hit_path), ("type", 1, ("a",)))

    def test_empty_path_returns_root(self) -> None:
        doc = {"a": 1}
        self.assertEqual(evaluate(doc, []), [Hit((), doc)])


class FilterTest(unittest.TestCase):
    def test_exists_counts_null_as_present(self) -> None:
        doc = {"a": [{"x": None}, {}, {"x": 0}]}
        hits = evaluate(doc, [Key("a"), Filter(exists([Key("x")]))])
        self.assertEqual(hit_paths(hits), [("a", 0), ("a", 2)])

    def test_eq_requires_single_scalar(self) -> None:
        doc = {
            "a": [
                {"x": 1},          # match
                {"x": "1"},        # different type -> false
                {"x": True},       # bool != int -> false
                {"x": [1]},        # one non-scalar -> unknown
                {"x": [1, 2]},     # multiple hits -> unknown
                {},                # no hit -> unknown
                {"x": None},       # null vs 1 -> false
            ]
        }
        hits = evaluate(doc, [Key("a"), Filter(eq([Key("x")], 1))])
        self.assertEqual(hit_paths(hits), [("a", 0)])

    def test_eq_null(self) -> None:
        doc = {"a": [{"x": None}, {"x": 0}, {"x": False}, {}]}
        self.assertEqual(
            hit_paths(evaluate(doc, [Key("a"), Filter(eq([Key("x")], None))])),
            [("a", 0)],
        )

    def test_eq_unknown_on_multiple_hits(self) -> None:
        doc = {"a": [{"x": [1, 2]}, {"x": [3, 1]}]}
        self.assertEqual(
            evaluate(doc, [Key("a"), Filter(eq([Key("x"), ArrayWildcard()], 1))]), []
        )
        self.assertEqual(
            hit_paths(
                evaluate(doc, [Key("a"), Filter(eq([Key("x"), Index(-1)], 1))])
            ),
            [("a", 1)],
        )

    def test_three_valued_logic(self) -> None:
        missing = {}
        one = {"x": 1}
        two = {"x": 2}
        expr = or_(eq([Key("x")], 1), not_(eq([Key("x")], 1)))
        # UNKNOWN OR UNKNOWN is UNKNOWN, so the element is not kept.
        self.assertEqual(evaluate_filter(expr, missing), UNKNOWN)
        self.assertEqual(evaluate({"a": [missing]}, [Key("a"), Filter(expr)]), [])
        self.assertIs(evaluate_filter(expr, one), True)
        self.assertIs(evaluate_filter(expr, two), True)

        and_expr = and_(eq([Key("x")], 1), eq([Key("x")], 2))
        self.assertIs(evaluate_filter(and_expr, missing), UNKNOWN)
        self.assertIs(evaluate_filter(and_expr, one), False)

        not_missing = not_(eq([Key("x")], 1))
        self.assertIs(evaluate_filter(not_missing, missing), UNKNOWN)

    def test_only_true_is_kept(self) -> None:
        doc = {"a": [{}, {"x": 2}, {"x": 1}]}
        expr = or_(eq([Key("x")], 1), eq([Key("x")], 2))
        self.assertEqual(
            hit_paths(evaluate(doc, [Key("a"), Filter(expr)])), [("a", 1), ("a", 2)]
        )

    def test_conditions_do_not_cross_elements(self) -> None:
        # x is in element 0 and y is in element 1: the conjunction must not
        # combine them.
        doc = {"a": [{"x": 1}, {"y": 1}]}
        expr = and_(exists([Key("x")]), eq([Key("y")], 1))
        self.assertEqual(evaluate(doc, [Key("a"), Filter(expr)]), [])

    def test_filter_requires_array(self) -> None:
        self.assertEqual(
            evaluate({"a": {"x": 1}}, [Key("a"), Filter(exists([Key("x")]))]), []
        )
        with self.assertRaises(PathError) as ctx:
            evaluate(
                {"a": {"x": 1}}, [Key("a"), Filter(exists([Key("x")]))], strict=True
            )
        self.assertEqual(ctx.exception.reason, "type")

    def test_relative_path_cannot_escape(self) -> None:
        # A relative path only walks downward from the element; there is no
        # parent/root syntax at all.
        expression = Filter(eq([Key("x")], 1))
        self.assertIs(evaluate_filter(expression.expr, {"x": 1}), True)

    def test_nested_filter_paths_and_wildcards(self) -> None:
        doc = {"groups": [{"members": [{"on": True}, {"on": False}]}, {"members": []}]}
        path = [
            Key("groups"),
            Filter(exists([Key("members"), ArrayWildcard(), Key("on")])),
        ]
        self.assertEqual(hit_paths(evaluate(doc, path)), [("groups", 0)])


class CodecTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        path = [
            Key("items"),
            Filter(
                and_(
                    eq([Key("kind")], "alpha"),
                    or_(exists([Key("note")]), not_(eq([Key("score")], 3))),
                )
            ),
            Index(-1),
            ObjectWildcard(),
        ]
        raw = path_to_json(path)
        self.assertEqual(path_from_json(raw), path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
