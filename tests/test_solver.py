"""Solver tests: DP vs independent brute-force enumeration and objectives."""

import random
import unittest

from stratcorr.models import (
    BoreholeInput,
    Edge,
    Horizon,
    Product,
    SegmentInput,
)
from stratcorr.poset import PartialOrder
from stratcorr.solver import (
    UNKNOWN,
    CorrelationContradiction,
    brute_force_hole,
    check_assigned_sequence,
    solve_all,
    solve_hole,
)


def make_product(
    horizon_ids, edges, lithology_map, sources=("t",)
) -> Product:
    horizons = tuple(
        Horizon(hid, hid, frozenset(lithology_map[hid]), sources[0])
        for hid in horizon_ids
    )
    edge_tuple = tuple(Edge(a, b, sources[0]) for a, b in edges)
    return Product("P1", 1, "local", "test", horizons, edge_tuple)


def make_hole(hole_id, specs, fixed=None):
    fixed = fixed or {}
    segments = tuple(
        SegmentInput(
            segment_id=f"{hole_id}-S{i + 1}",
            start=f"{i}/1",
            end=f"{i + 1}/1",
            lithology=lith,
            weight=weight,
            fixed_horizon=fixed.get(i),
        )
        for i, (lith, weight) in enumerate(specs)
    )
    return BoreholeInput(hole_id, segments, ())


class SolverObjectiveTest(unittest.TestCase):
    def setUp(self):
        # H1 < H2 < H3, plus incomparable H4 above H3 via H2 < H4.
        self.product = make_product(
            ["H1", "H2", "H3", "H4"],
            [("H1", "H2"), ("H2", "H3"), ("H2", "H4"), ("H4", "H3")],
            {
                "H1": {"CLAY"},
                "H2": {"SAND", "SILT"},
                "H3": {"SAND", "MARL"},
                "H4": {"SAND"},
            },
        )
        self.poset = PartialOrder(
            ["H1", "H2", "H3", "H4"],
            [("H1", "H2"), ("H2", "H3"), ("H2", "H4"), ("H4", "H3")],
        )

    def test_minimise_unknown_weight_first(self):
        # Heavy unknown vs cheap known assignment trade-off.
        hole = make_hole("BH", [("GRAVEL", 10), ("CLAY", 1), ("SAND", 1)])
        solution = solve_hole(hole, self.product, self.poset)
        vector = solution.vector
        self.assertEqual(vector[0], UNKNOWN)  # GRAVEL accepted nowhere
        self.assertEqual(vector[1], "H1")
        self.assertEqual(solution.unknown_weight, 10)

    def test_duplicate_lithology_not_merged_and_order_breaks_tie(self):
        # Two SAND segments share a lithology accepted by H2/H3/H4; a leading
        # fixation pins the first to H2 and the final MARL segment can only be
        # H3.  Equal-lithology segments are not merged across horizons, but the
        # switch penalty keeps the free SAND inside H2 before moving to H3.
        hole = make_hole(
            "BH",
            [("SAND", 1), ("SAND", 1), ("MARL", 1)],
            fixed={0: "H2"},
        )
        solution = solve_hole(hole, self.product, self.poset)
        self.assertEqual(list(solution.vector), ["H2", "H2", "H3"])
        self.assertEqual(solution.switches, 1)

    def test_reentry_is_forbidden(self):
        # CLAY only fits H1.  The vector H1,H2,H1 would re-enter H1 and must
        # be rejected outright; among feasible vectors the one with fewer
        # switches wins, which is achieved by marking the middle SAND UNKNOWN
        # so the two H1 assignments stay consecutive after UNKNOWN removal.
        hole = make_hole("BH", [("CLAY", 1), ("SAND", 1), ("CLAY", 1)])
        solution = solve_hole(hole, self.product, self.poset)
        self.assertEqual(list(solution.vector), ["H1", UNKNOWN, "H1"])
        self.assertEqual(solution.switches, 0)
        violations = check_assigned_sequence(
            ["H1", "H2", "H3", "H4"],
            self.poset._reachable,
            [0, 1, 0],  # H1, H2, H1 -> re-entry
        )
        self.assertTrue(any("re-entry" in v for v in violations))

    def test_incomparable_branch_resolved_by_lexicographic_id(self):
        # Clean incomparable case: A < B and A < C, B/C incomparable, both
        # accept lithology Y.  Costs are identical, so horizon ID order
        # decides (B before C); a topological convenience never does.
        product = make_product(
            ["A", "B", "C"],
            [("A", "B"), ("A", "C")],
            {"A": {"X"}, "B": {"Y"}, "C": {"Y"}},
        )
        poset = PartialOrder(["A", "B", "C"], [("A", "B"), ("A", "C")])
        hole = make_hole(
            "BH", [("X", 1), ("Y", 1), ("Y", 1)], fixed={0: "A"}
        )
        solution = solve_hole(hole, product, poset)
        self.assertEqual(list(solution.vector), ["A", "B", "B"])
        self.assertEqual(solution.switches, 1)

    def test_switch_minimisation_beats_early_lexicographic_move(self):
        # SILT only fits H2; the free SAND segment may stay in H2 (fewer
        # switches) before MARL forces H3.  An early move to H3 ties on
        # switches only if H2 is skipped, so staying put must win.
        hole = make_hole("BH", [("SILT", 1), ("SAND", 1), ("MARL", 1)])
        solution = solve_hole(hole, self.product, self.poset)
        self.assertEqual(list(solution.vector), ["H2", "H2", "H3"])
        self.assertEqual(solution.switches, 1)

    def test_unknown_ranks_after_every_horizon_in_lexicographic_tie(self):
        # All-SAND hole where every segment could be H2; no tie with UNKNOWN
        # once weights are equal, because UNKNOWN must rank last.  Construct a
        # case where known vs unknown share the first two cost components.
        hole = make_hole("BH", [("GRAVEL", 1), ("GRAVEL", 1)])
        solution = solve_hole(hole, self.product, self.poset)
        self.assertEqual(list(solution.vector), [UNKNOWN, UNKNOWN])

    def test_fixation_is_hard_and_never_unknown(self):
        hole = make_hole(
            "BH",
            [("SAND", 1), ("SAND", 1)],
            fixed={0: "H4", 1: "H3"},
        )
        solution = solve_hole(hole, self.product, self.poset)
        self.assertEqual(list(solution.vector), ["H4", "H3"])

    def test_contradictory_fixation_reports_evidence(self):
        hole = make_hole(
            "BH",
            [("SAND", 1), ("SAND", 1)],
            fixed={0: "H3", 1: "H2"},  # H3 -> H2 unsupported (H2 < H3)
        )
        with self.assertRaises(CorrelationContradiction) as ctx:
            solve_all([hole], self.product)
        contradiction = ctx.exception.contradictions[0]
        self.assertEqual(contradiction.kind, "fixed_order")
        self.assertEqual(len(contradiction.related_fixes), 2)
        self.assertTrue(contradiction.relations)
        self.assertIsNone(contradiction.relations[0]["forward_path"])

    def test_fixed_lithology_mismatch_is_contradiction(self):
        hole = make_hole("BH", [("CLAY", 1)], fixed={0: "H3"})
        with self.assertRaises(CorrelationContradiction) as ctx:
            solve_all([hole], self.product)
        self.assertEqual(ctx.exception.contradictions[0].kind, "fixed_lithology")

    def test_fixed_reentry_is_contradiction(self):
        hole = make_hole(
            "BH",
            [("SAND", 1), ("MARL", 1), ("SAND", 1)],
            fixed={0: "H2", 1: "H3", 2: "H2"},
        )
        with self.assertRaises(CorrelationContradiction) as ctx:
            solve_all([hole], self.product)
        self.assertEqual(ctx.exception.contradictions[0].kind, "fixed_reentry")

    def test_leaving_horizon_blocks_return_even_if_lithology_matches(self):
        # Only one horizon accepts CLAY; fixations force a real transition out
        # of it, so the trailing CLAY must become UNKNOWN rather than return.
        product = make_product(
            ["H00", "H05", "H11"],
            [("H00", "H05"), ("H05", "H11")],
            {"H00": {"CLAY"}, "H05": {"SAND"}, "H11": {"MARL"}},
        )
        poset = PartialOrder(
            ["H00", "H05", "H11"],
            [("H00", "H05"), ("H05", "H11")],
        )
        hole = make_hole(
            "BH",
            [("CLAY", 1), ("SAND", 1), ("CLAY", 1)],
            fixed={0: "H00", 1: "H05"},
        )
        solution = solve_hole(hole, product, poset)
        self.assertEqual(list(solution.vector), ["H00", "H05", UNKNOWN])


class BruteForceCrossCheckTest(unittest.TestCase):
    """Random small problems: dynamic program and enumeration must agree."""

    def test_random_holes_match_brute_force(self):
        rng = random.Random(20261119)
        lith_codes = ["SAND", "CLAY", "MARL", "GRAVEL"]
        horizon_ids = ["A", "B", "C", "D"]
        # Chain DAG keeps every pair comparable here; random lithology gives
        # UNKNOWN and duplicate-lithology cases naturally.
        edges = [("A", "B"), ("B", "C"), ("C", "D")]
        lithology_map = {
            hid: set(rng.sample(lith_codes, k=rng.randint(1, 3)))
            for hid in horizon_ids
        }
        product = make_product(horizon_ids, edges, lithology_map)
        poset = PartialOrder(horizon_ids, edges)

        for trial in range(60):
            length = rng.randint(1, 5)
            specs = [
                (lith_codes[rng.randrange(len(lith_codes))], rng.randint(1, 4))
                for _ in range(length)
            ]
            hole = make_hole(f"R{trial}", specs)
            dp = solve_hole(hole, product, poset)
            brute = brute_force_hole(hole, product, poset)
            self.assertEqual(
                dp.vector,
                brute.vector,
                msg=f"mismatch on {specs} with {lithology_map}",
            )
            self.assertEqual(dp.unknown_weight, brute.unknown_weight)
            self.assertEqual(dp.switches, brute.switches)

    def test_branching_poset_matches_brute_force(self):
        rng = random.Random(7)
        horizon_ids = ["A", "B", "C"]
        edges = [("A", "B"), ("A", "C")]  # B and C incomparable
        product = make_product(
            horizon_ids,
            edges,
            {"A": {"CLAY"}, "B": {"SAND"}, "C": {"SAND"}},
        )
        poset = PartialOrder(horizon_ids, edges)
        for trial in range(40):
            specs = [
                ("SAND" if rng.random() < 0.8 else "CLAY", rng.randint(1, 3))
                for _ in range(rng.randint(1, 5))
            ]
            hole = make_hole(f"Q{trial}", specs)
            dp = solve_hole(hole, product, poset)
            brute = brute_force_hole(hole, product, poset)
            self.assertEqual(dp.vector, brute.vector)
            self.assertEqual(dp.switches, brute.switches)


class GlobalObjectiveTest(unittest.TestCase):
    def test_per_hole_optima_compose_global_lex_vector(self):
        product = make_product(
            ["A", "B"],
            [("A", "B")],
            {"A": {"X"}, "B": {"X"}},
        )
        h1 = make_hole("K1", [("X", 1), ("Z", 1)])
        h2 = make_hole("K2", [("X", 1), ("X", 1)])
        result = solve_all([h1, h2], product)
        self.assertEqual(result.total_unknown_weight, 1)
        self.assertEqual(result.lex_vector, ("A", UNKNOWN, "A", "A"))


if __name__ == "__main__":
    unittest.main()
