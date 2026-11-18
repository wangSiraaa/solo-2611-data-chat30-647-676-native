"""Range algebra tests: canonical form, parsing, and brute-force set checks."""

from __future__ import annotations

import itertools
import random
import unittest

from multi_range_reservation.ranges import (
    LIMIT,
    RangeError,
    RangeService,
    RangeValue,
    canonicalize,
    contains,
    difference,
    intersection,
    intersects,
    is_empty,
    normalize,
    union,
)

UNIVERSE = range(-8, 9)


def points(value: object) -> frozenset[int]:
    """Independent set semantics obtained by enumeration, not by the algebra."""

    result = RangeValue(value)
    known = {p for p in UNIVERSE if result.contains(p)}
    # Anything outside the universe but inside the represented boundary is
    # still checked by iterating the canonical pairs directly.
    outside = {p for p in result.iter_points() if p not in UNIVERSE}
    return frozenset(known | outside)


def random_segment(rng: random.Random, low: int = -4, high: int = 5) -> dict:
    return {
        "lower": rng.randint(low, high),
        "upper": rng.randint(low, high),
        "lower_inclusive": rng.random() < 0.5,
        "upper_inclusive": rng.random() < 0.5,
    }


def random_value(rng: random.Random, max_segments: int = 4) -> list[dict]:
    return [random_segment(rng) for _ in range(rng.randint(0, max_segments))]


class CanonicalFormTests(unittest.TestCase):
    def assert_canonical(self, value: RangeValue) -> None:
        pairs = value.pairs
        for lo, hi in pairs:
            self.assertLess(lo, hi, f"empty segment in {pairs}")
        for (_, prev_hi), (next_lo, _) in zip(pairs, pairs[1:]):
            self.assertLess(prev_hi, next_lo, f"adjacent/overlapping pairs in {pairs}")

    def test_open_closed_normalization(self):
        self.assertEqual(normalize("(1,3]"), normalize("[2,4)"))
        self.assertEqual(normalize("(1,3]"), RangeValue({"half_open": [[2, 4]]}))
        self.assertEqual(normalize("[1,3)"), normalize((1, 3, True, False)))
        self.assertEqual(normalize("[1,3)"), normalize((1, 2, True, True)))

    def test_hole_between_segments_is_preserved(self):
        value = normalize("[1,2) [3,4)")
        self.assertEqual(value.as_pairs(), [[1, 2], [3, 4]])
        self.assertFalse(value.contains(2))
        self.assertTrue(value.contains(1))
        self.assertTrue(value.contains(3))
        self.assertNotEqual(value, normalize("[1,4)"))

    def test_adjacent_segments_merge(self):
        self.assertEqual(normalize("[1,2) [2,3)").as_pairs(), [[1, 3]])
        self.assertEqual(normalize("[1,2] (2,3]").as_pairs(), [[1, 4]])
        self.assertEqual(normalize("[1,2] [3,4]").as_pairs(), [[1, 5]])
        # Point 3 is missing, so these stay apart.
        self.assertEqual(normalize("[1,2] [4,5]").as_pairs(), [[1, 3], [4, 6]])

    def test_empty_segments_are_legal_and_ignored(self):
        for text in ("[4,4)", "(2,3)", "[5,1]", "[3,2)", "∅"):
            self.assertTrue(normalize(text).is_empty, text)
        # A closed singleton is a non-empty set containing that integer.
        self.assertEqual(normalize("[2,2]").as_pairs(), [[2, 3]])
        mixed = normalize("[4,4) (2,3) [1,2) [9,1]")
        self.assertEqual(mixed.as_pairs(), [[1, 2]])

    def test_input_order_does_not_matter(self):
        a = normalize("[10,12) [1,2) [5,7)")
        b = normalize([{"lower": 5, "upper": 6}, (1, 1), (10, 12, True, False)])
        self.assertEqual(a, b)
        self.assert_canonical(a)

    def test_same_set_different_representation_is_equal(self):
        variants = [
            "[1,5)",
            "[1,2) [2,4) [4,5)",
            "(0,5)",
            (1, 4, True, True),
            {"lower": 0, "upper": 5, "lower_inclusive": False, "upper_inclusive": False},
            {"half_open": [[1, 2], [2, 3], [3, 5]]},
        ]
        canon = normalize(variants[0])
        for variant in variants[1:]:
            with self.subTest(variant=variant):
                self.assertEqual(normalize(variant), canon)
                self.assertEqual(normalize(variant).as_pairs(), [[1, 5]])

    def test_idempotent(self):
        value = normalize("[1,2) [4,9) (9,10]")
        self.assertEqual(normalize(value), value)
        self.assertEqual(value.union(), value)
        self.assertEqual(value.intersection(value), value)
        self.assertTrue(value.difference(value).is_empty)

    def test_text_round_trip(self):
        for value in (
            normalize("∅"),
            normalize("[1,5)"),
            normalize("[-3,-1) [0,1) [4,9]"),
            normalize("[1000000,1000001)"),
        ):
            self.assertEqual(normalize(value.to_text()), value)
            self.assertEqual(
                RangeValue.from_half_open(value.as_pairs()), value
            )
            self.assertEqual(normalize({"half_open": value.as_pairs()}), value)
            self.assertEqual(normalize(value.as_segments()), value)

    def test_endpoint_limits(self):
        self.assertEqual(normalize(f"[{LIMIT},{LIMIT}]").as_pairs(), [[LIMIT, LIMIT + 1]])
        self.assertEqual(
            normalize(f"[-{LIMIT},-{LIMIT}]").as_pairs(), [[-LIMIT, -LIMIT + 1]]
        )
        with self.assertRaises(RangeError):
            normalize(f"[{LIMIT + 1},{LIMIT + 1}]")
        with self.assertRaises(RangeError):
            normalize(f"[-{LIMIT + 1},0]")
        with self.assertRaises(RangeError):
            normalize("[-10000001,5]")
        with self.assertRaises(RangeError):
            RangeValue({"half_open": [[0, LIMIT + 2]]})

    def test_bad_input_rejected(self):
        for bad in (
            {"lower": 1},
            {"lower": "1", "upper": 3},
            {"lower": 1, "upper": 3, "upper_inclusive": 1},
            "not a range",
            "[1,2) garbage",
            (1, 2, 3),
            [object()],
            12,
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(RangeError):
                    normalize(bad)


class BruteForceAlgebraTests(unittest.TestCase):
    def check(self, a: object, b: object) -> None:
        left = normalize(a)
        right = normalize(b)
        set_a = points(left)
        set_b = points(right)

        self.assertEqual(points(left.union(right)), set_a | set_b)
        self.assertEqual(points(intersection(left, right)), set_a & set_b)
        self.assertEqual(points(difference(left, right)), set_a - set_b)
        self.assertEqual(intersects(left, right), bool(set_a & set_b))
        self.assertEqual(is_empty(left), not set_a)
        for point in range(-6, 7):
            self.assertEqual(contains(left, point), point in set_a)

    def test_deterministic_pairs(self):
        cases = [
            ("[1,2) [3,4)", "[2,3)"),
            ("(1,3] [5,7]", "[2,6)"),
            ("[-5,-2] (-2,0)", "[-3,1)"),
            ("[1,10)", "[2,3) [4,5) [9,12)"),
            ("[]", "[1,2)"),
            ("[1,2)", "[]"),
        ]
        for a, b in cases:
            with self.subTest(a=a, b=b):
                self.check(a, b)

    def test_random_enumeration(self):
        rng = random.Random(20261118)
        for _ in range(300):
            a = random_value(rng)
            b = random_value(rng)
            self.check(a, b)
            self.assert_canonical_pair(normalize(a))

    def assert_canonical_pair(self, value: RangeValue) -> None:
        prev_hi = None
        for lo, hi in value.pairs:
            self.assertLess(lo, hi)
            if prev_hi is not None:
                self.assertLess(prev_hi, lo)
            prev_hi = hi

    def test_all_single_point_sets_over_small_domain(self):
        domain = list(range(-3, 4))
        for size in range(0, 4):
            for subset in itertools.combinations(domain, size):
                segments = [
                    {"lower": p, "upper": p, "lower_inclusive": True, "upper_inclusive": True}
                    for p in subset
                ]
                value = normalize(segments)
                self.assertEqual(points(value), frozenset(subset))
                for p in domain:
                    self.assertEqual(value.contains(p), p in subset)


class ServiceWrapperTests(unittest.TestCase):
    def test_range_service_wrapper(self):
        service = RangeService()
        value = service.normalize("[1,2) [3,4)")
        self.assertEqual(service.union(value, "[2,3)"), service.normalize("[1,4)"))
        self.assertTrue(service.intersects(value, "[3,9)"))
        self.assertEqual(service.intersection(value, "[3,9)").as_pairs(), [[3, 4]])
        self.assertEqual(service.difference(value, "[3,9)").as_pairs(), [[1, 2]])
        self.assertTrue(service.contains(value, 3))
        self.assertFalse(service.contains(value, 2))
        self.assertFalse(service.is_empty(value))
        self.assertTrue(service.is_empty("∅"))

    def test_canonicalize_alias(self):
        self.assertEqual(canonicalize("[1,2) [2,3)"), normalize("[1,3)"))


if __name__ == "__main__":
    unittest.main()
