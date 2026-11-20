"""Geometry statistics and sample membership tests, including boundaries."""

import unittest

from stratcorr.geometry import (
    SAMPLE_GAP,
    SAMPLE_HIT,
    SAMPLE_MISS,
    SAMPLE_UNKNOWN,
    GeometryError,
    assign_samples,
    build_statistics,
    validate_borehole,
)
from stratcorr.models import (
    BoreholeInput,
    GapInput,
    SampleInput,
    SegmentInput,
)
from stratcorr.solver import Assignment, HoleSolution


def hole_with_gaps() -> BoreholeInput:
    return BoreholeInput(
        "BH",
        segments=(
            SegmentInput("S1", "0/1", "2/1", "CLAY", 1),
            SegmentInput("S2", "5/1", "6/1", "SAND", 1),
            SegmentInput("S3", "8/1", "9/1", "SAND", 1),
            SegmentInput("S4", "11/1", "12/1", "MARL", 1),
        ),
        gaps=(
            GapInput("G-same", "6/1", "8/1"),    # between S2 and S3
            GapInput("G-cross", "9/1", "11/1"),  # between S3 and S4
            GapInput("G-top", "0/1", "0/1"),     # zero thickness -> invalid
        ),
    )


class ValidationTest(unittest.TestCase):
    def test_zero_thickness_rejected(self):
        with self.assertRaises(GeometryError):
            validate_borehole(hole_with_gaps())

    def test_overlap_rejected(self):
        hole = BoreholeInput(
            "BH",
            (
                SegmentInput("S1", "0/1", "3/1", "X", 1),
                SegmentInput("S2", "2/1", "4/1", "Y", 1),
            ),
        )
        with self.assertRaises(GeometryError):
            validate_borehole(hole)

    def test_non_positive_weight_rejected(self):
        hole = BoreholeInput(
            "BH", (SegmentInput("S1", "0/1", "1/1", "X", 0),)
        )
        with self.assertRaises(GeometryError):
            validate_borehole(hole)

    def test_touching_endpoints_are_allowed(self):
        hole = BoreholeInput(
            "BH",
            (
                SegmentInput("S1", "1/2", "3/2", "X", 1),
                SegmentInput("S2", "3/2", "5/2", "Y", 1),
            ),
        )
        validate_borehole(hole)  # no exception

    def test_segments_must_be_shallow_to_deep(self):
        hole = BoreholeInput(
            "BH",
            (
                SegmentInput("S1", "2/1", "3/1", "X", 1),
                SegmentInput("S2", "0/1", "1/1", "Y", 1),
            ),
        )
        with self.assertRaises(GeometryError):
            validate_borehole(hole)


class StatisticsTest(unittest.TestCase):
    def setUp(self):
        segments = (
            SegmentInput("S1", "0/1", "2/1", "CLAY", 1),
            SegmentInput("S2", "5/1", "6/1", "SAND", 1),
            SegmentInput("S3", "8/1", "9/1", "SAND", 1),
            SegmentInput("S4", "11/1", "12/1", "MARL", 1),
        )
        gaps = (
            GapInput("G-same", "6/1", "8/1"),
            GapInput("G-cross", "9/1", "11/1"),
        )
        self.hole = BoreholeInput("BH", segments, gaps)
        # Assignments: S1 -> H1(unknown here, use labels), S2/S3 -> H2, S4 -> H3.
        self.solution = HoleSolution(
            "BH",
            (
                Assignment("S1", "BH", None),       # UNKNOWN
                Assignment("S2", "BH", "H2"),
                Assignment("S3", "BH", "H2"),
                Assignment("S4", "BH", "H3"),
            ),
            unknown_weight=1,
            switches=2,
        )

    def test_same_horizon_gap_attributed_others_undetermined(self):
        stats, gap_records = build_statistics(self.hole, self.solution)
        by_id = {g.gap_id: g for g in gap_records}
        self.assertEqual(by_id["G-same"].horizon_id, "H2")
        self.assertEqual(by_id["G-same"].thickness, "2/1")
        self.assertIsNone(by_id["G-cross"].horizon_id)
        self.assertIn("different horizons", by_id["G-cross"].reason)

        h2 = next(s for s in stats if s.horizon_id == "H2")
        self.assertEqual(h2.top_depth, "5/1")
        self.assertEqual(h2.bottom_depth, "9/1")
        self.assertEqual(h2.cored_thickness, "2/1")  # 1 + 1
        self.assertEqual(h2.gap_thickness, "2/1")    # only G-same
        self.assertEqual(h2.segment_ids, ("S2", "S3"))
        self.assertEqual(h2.gap_ids, ("G-same",))

        h3 = next(s for s in stats if s.horizon_id == "H3")
        self.assertEqual(h3.gap_thickness, "0/1")    # cross gap not apportioned
        self.assertEqual(h3.cored_thickness, "1/1")

        # UNKNOWN segment never appears in known-horizon statistics.
        self.assertFalse(any(s.horizon_id == "UNKNOWN" for s in stats))
        self.assertTrue(all("S1" not in s.segment_ids for s in stats))

    def test_half_open_sample_boundaries(self):
        samples = [
            SampleInput("at-start", "0/1", "BH"),
            SampleInput("at-s1-end", "2/1", "BH"),
            SampleInput("inside-gap", "7/1", "BH"),
            SampleInput("at-deepest-end", "12/1", "BH"),
            SampleInput("inside-unknown", "1/1", "BH"),
            SampleInput("cross-gap", "10/1", "BH"),
        ]
        memberships = {
            m.sample_id: m
            for m in assign_samples(samples, {"BH": self.hole},
                                    {"BH": self.solution})
        }
        self.assertEqual(memberships["at-start"].status, SAMPLE_UNKNOWN)
        # 2/1 is outside S1 [0,2): lands in the open interval before S2.
        self.assertEqual(memberships["at-s1-end"].status, SAMPLE_MISS)
        self.assertEqual(memberships["inside-gap"].status, SAMPLE_GAP)
        self.assertEqual(memberships["cross-gap"].status, SAMPLE_GAP)
        self.assertEqual(memberships["at-deepest-end"].status, SAMPLE_MISS)
        unknown_hit = memberships["inside-unknown"]
        self.assertEqual(unknown_hit.status, SAMPLE_UNKNOWN)
        self.assertIsNone(unknown_hit.horizon_id)

    def test_hit_member_uses_start_end_interval(self):
        samples = [SampleInput("ok", "5/1", "BH")]
        memberships = assign_samples(
            samples, {"BH": self.hole}, {"BH": self.solution}
        )
        self.assertEqual(memberships[0].status, SAMPLE_HIT)
        self.assertEqual(memberships[0].horizon_id, "H2")
        self.assertEqual(memberships[0].segment_id, "S2")


class RationalArithmeticTest(unittest.TestCase):
    def test_fractional_depth_geometry(self):
        hole = BoreholeInput(
            "BH",
            (
                SegmentInput("S1", "1/3", "2/3", "X", 1),
                SegmentInput("S2", "2/3", "5/6", "Y", 1),
            ),
        )
        validate_borehole(hole)
        solution = HoleSolution(
            "BH",
            (Assignment("S1", "BH", "HA"), Assignment("S2", "BH", "HB")),
            0,
            1,
        )
        stats, _ = build_statistics(hole, solution)
        ha = next(s for s in stats if s.horizon_id == "HA")
        self.assertEqual(ha.cored_thickness, "1/3")
        hb = next(s for s in stats if s.horizon_id == "HB")
        self.assertEqual(hb.cored_thickness, "1/6")  # 5/6 - 2/3


if __name__ == "__main__":
    unittest.main()
