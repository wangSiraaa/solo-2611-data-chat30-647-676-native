"""Tests for the finite partial order: cycles, closure, evidence, sequences."""

import unittest

from stratcorr.poset import (
    CycleError,
    PartialOrder,
    SelfLoopError,
    UnknownHorizonError,
)


class PartialOrderTest(unittest.TestCase):
    def setUp(self):
        # H1 < H2; H2 branches into incomparable H3, H4; both < H5 < H6.
        self.ids = ["H1", "H2", "H3", "H4", "H5", "H6"]
        self.edges = [
            ("H1", "H2"),
            ("H2", "H3"),
            ("H2", "H4"),
            ("H3", "H5"),
            ("H4", "H5"),
            ("H5", "H6"),
        ]
        self.poset = PartialOrder(self.ids, self.edges)

    def test_self_loop_rejected(self):
        with self.assertRaises(SelfLoopError):
            PartialOrder(["A"], [("A", "A")])

    def test_directed_cycle_rejected_with_witness(self):
        with self.assertRaises(CycleError) as ctx:
            PartialOrder(
                ["A", "B", "C"],
                [("A", "B"), ("B", "C"), ("C", "A")],
            )
        self.assertEqual(ctx.exception.cycle[0], ctx.exception.cycle[-1])
        self.assertGreaterEqual(len(ctx.exception.cycle), 3)

    def test_unknown_horizon_edge(self):
        with self.assertRaises(UnknownHorizonError):
            PartialOrder(["A", "B"], [("A", "X")])

    def test_transitive_relation_and_witness_path(self):
        self.assertTrue(self.poset.is_shallower("H1", "H6"))
        path = self.poset.path("H1", "H6")
        self.assertIsNotNone(path)
        self.assertEqual(path[0], "H1")
        self.assertEqual(path[-1], "H6")
        # Every adjacent pair in the witness must be a direct edge.
        direct = set(self.edges)
        for a, b in zip(path, path[1:]):
            self.assertIn((a, b), direct)

    def test_incomparable_branch_is_undetermined(self):
        evidence = self.poset.evidence("H3", "H4")
        self.assertTrue(evidence.is_incomparable)
        self.assertIsNone(evidence.forward_path)
        self.assertIsNone(evidence.backward_path)
        self.assertIn("incomparable", evidence.explain())
        self.assertFalse(self.poset.is_shallower("H3", "H4"))
        self.assertFalse(self.poset.is_shallower("H4", "H3"))

    def test_reverse_direction_evidence(self):
        evidence = self.poset.evidence("H5", "H2")
        self.assertFalse(evidence.is_incomparable)
        self.assertIsNone(evidence.forward_path)
        self.assertEqual(evidence.backward_path[0], "H2")
        self.assertEqual(evidence.backward_path[-1], "H5")

    def test_validate_sequence_accepts_repeats_and_skips(self):
        self.assertEqual(
            self.poset.validate_sequence(["H1", "H2", "H2", "H4", "H6"]),
            [],
        )

    def test_validate_sequence_flags_reentry(self):
        violations = self.poset.validate_sequence(["H1", "H2", "H3", "H2"])
        self.assertTrue(any("re-entry" in v for v in violations))

    def test_validate_sequence_flags_incomparable_transition(self):
        violations = self.poset.validate_sequence(["H3", "H4"])
        self.assertEqual(len(violations), 1)
        self.assertIn("order violation", violations[0])

    def test_topo_sort_is_linear_extension_not_a_claim(self):
        order = self.poset.topo_sort()
        position = {h: i for i, h in enumerate(order)}
        for parent, child in self.edges:
            self.assertLess(position[parent], position[child])
        # Incomparable nodes get an arbitrary deterministic tie break.
        self.assertEqual(order, self.poset.topo_sort())


if __name__ == "__main__":
    unittest.main()
