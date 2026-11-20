"""Synthetic, deterministic input data for demos and the test suite.

The generated dataset deliberately contains every awkward case named in the
specification:

* **missing horizons** -- boreholes skip catalogue horizons; line-number
  alignment between boreholes would be wrong;
* **incomparable horizons** -- ``H3`` and ``H4`` share no
  ``shallower_than`` path, so their relative order is undetermined;
* **duplicate lithology** -- ``H2`` and ``H3`` both accept ``SAND``; the
  solver must not merge them just because the lithology code is identical;
* **boundary samples** -- samples exactly at segment endpoints verify the
  half-open ``[start, end)`` rule (the deepest end stays a miss);
* **same-horizon and cross-horizon gaps** -- only same-horizon gaps are
  attributed; cross-horizon gaps remain undetermined;
* **hard fixations and a contradiction** -- one borehole is fixed in an
  order the partial order forbids.

Everything is synthetic bookkeeping data; no engineering interpretation is
implied.
"""

from __future__ import annotations

from .models import (
    BoreholeInput,
    Edge,
    GapInput,
    Horizon,
    Product,
    SampleInput,
    SegmentInput,
)


def build_product() -> Product:
    horizons = (
        Horizon("H1", "horizon-1", frozenset({"CLAY"}), "synthetic:doc-1"),
        Horizon("H2", "horizon-2", frozenset({"SAND", "SILT"}), "synthetic:doc-1"),
        Horizon("H3", "horizon-3", frozenset({"SAND"}), "synthetic:doc-2"),
        Horizon("H4", "horizon-4", frozenset({"GRAVEL", "SILT"}), "synthetic:doc-2"),
        Horizon("H5", "horizon-5", frozenset({"MARL"}), "synthetic:doc-3"),
        Horizon("H6", "horizon-6", frozenset({"LIMESTONE"}), "synthetic:doc-3"),
    )
    # H1 < H2; H2 branches into incomparable H3 and H4; both precede H5 < H6.
    edges = (
        Edge("H1", "H2", "synthetic:log-A"),
        Edge("H2", "H3", "synthetic:log-A"),
        Edge("H2", "H4", "synthetic:log-B"),
        Edge("H3", "H5", "synthetic:log-A"),
        Edge("H4", "H5", "synthetic:log-B"),
        Edge("H5", "H6", "synthetic:log-A"),
    )
    return Product(
        product_id="SYN-0001",
        version=1,
        kind="local",
        note="synthetic partial order with an incomparable branch",
        horizons=horizons,
        edges=edges,
    )


def build_boreholes() -> list[BoreholeInput]:
    # B1: H1, split H2 (gap inside same horizon), H4, H6 -- skips H3/H5.
    b1 = BoreholeInput(
        borehole_id="B1",
        segments=(
            SegmentInput("B1-S1", "0/1", "2/1", "CLAY", 1),
            SegmentInput("B1-S2", "3/1", "5/1", "SAND", 1),
            SegmentInput("B1-S3", "7/1", "8/1", "SILT", 1),
            SegmentInput("B1-S4", "10/1", "12/1", "GRAVEL", 2),
            SegmentInput("B1-S5", "12/1", "14/1", "LIMESTONE", 3),
        ),
        gaps=(
            # bounded by B1-S2 and B1-S3: same-horizon gap once both -> H2
            GapInput("B1-G1", "5/1", "7/1"),
            # bounded by H4 segment and H6 segment: cross-horizon gap
            GapInput("B1-G2", "8/1", "10/1"),
        ),
    )

    # B2: unknown top, missing H1, repeated SAND over incomparable H2/H3.
    b2 = BoreholeInput(
        borehole_id="B2",
        segments=(
            # SILTSTONE is accepted by no horizon -> UNKNOWN on every product.
            SegmentInput("B2-S1", "1/1", "2/1", "SILTSTONE", 5),
            SegmentInput("B2-S2", "2/1", "4/1", "SAND", 2),
            SegmentInput("B2-S3", "4/1", "6/1", "SAND", 2),    # same lithology
            SegmentInput("B2-S4", "6/1", "8/1", "MARL", 1),
        ),
        gaps=(GapInput("B2-G1", "0/1", "1/1"),),  # above first cored segment
    )

    # B3: hard fixations pinning H1 then H4 (valid via H1->H2->H4).
    b3 = BoreholeInput(
        borehole_id="B3",
        segments=(
            SegmentInput("B3-S1", "0/1", "1/1", "CLAY", 1, fixed_horizon="H1"),
            SegmentInput("B3-S2", "2/1", "3/1", "SILT", 1, fixed_horizon="H4"),
            SegmentInput("B3-S3", "3/1", "5/1", "LIMESTONE", 1),
        ),
        gaps=(GapInput("B3-G1", "1/1", "2/1"),),
    )

    # B4: contradictory fixations -- H4 fixed shallower than H1, but the
    # catalogue only knows H1 -> ... -> H4, and H1/H4 are not equal.
    b4 = BoreholeInput(
        borehole_id="B4",
        segments=(
            SegmentInput("B4-S1", "0/1", "1/1", "GRAVEL", 1, fixed_horizon="H4"),
            SegmentInput("B4-S2", "1/1", "2/1", "CLAY", 1, fixed_horizon="H1"),
        ),
    )
    return [b1, b2, b3, b4]


def build_samples() -> list[SampleInput]:
    return [
        # B1 boundary and interior samples.
        SampleInput("SM1", "0/1", "B1"),       # first segment start -> HIT
        SampleInput("SM2", "2/1", "B1"),       # B1-S1 end == B1-S2 start -> S2 HIT
        SampleInput("SM3", "6/1", "B1"),       # inside same-horizon gap -> GAP
        SampleInput("SM4", "12/1", "B1"),      # S4 end / S5 start -> HIT in S5
        SampleInput("SM5", "14/1", "B1"),      # deepest end -> MISS
        SampleInput("SM6", "11/1", "B1"),      # interior H4 segment -> HIT
        # B2 samples, including one inside the UNKNOWN top segment.
        SampleInput("SM7", "3/2", "B2"),       # 1.5 inside UNKNOWN B2-S1
        SampleInput("SM8", "3/1", "B2"),       # interior B2-S2
        # Unboreholed sample and a global (no borehole) sample.
        SampleInput("SM9", "100/1", "B1"),     # far below everything -> MISS
        SampleInput("SM10", "3/1", None),      # searched across all boreholes
    ]
