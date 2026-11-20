"""Deterministic synthetic data generation.

Produces

* ``data/catalog.json``    - 5 port tide models (each up to 4 harmonics),
  with one port carrying a corrected second revision.
* ``data/scenarios.json``  - channels (<= 8 segments) + 30 passage requests,
  including the mandatory feature scenarios:
    - a single-sine hand-checkable channel,
    - an interior local valley,
    - a pure tangency (isolated feasible instant),
    - a channel whose segments belong to *different* ports with real sailing
      offsets (so intersecting raw port windows would be wrong),
    - constant-level models (all-zero harmonics; one exactly at threshold).

Everything is rational.  All times are hours; all levels are synthetic and
must not be used for real navigation.
"""
from __future__ import annotations

import json
import os
from fractions import Fraction

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")


def S(x):
    """rational -> canonical string used in the JSON files."""
    f = Fraction(x)
    return str(f.numerator) if f.denominator == 1 else f"{f.numerator}/{f.denominator}"


def build_catalog():
    """5 ports; P2 has two revisions (a corrected product)."""
    models = [
        # P1 single sine, hand-checkable: level = sin(pi t/12)
        #   0 at t=0,12;  +1 at t=6;  -1 at t=18.
        ("P1", 1, "0", ["0", "0", "0", "0"], ["1", "0", "0", "0"],
         "single sine sin(pi t/12)"),
        # P2 rich 4-harmonic tide with interior valleys.
        ("P2", 1, "1/2",
         ["3/2", "-1/2", "1/3", "1/4"],
         ["0", "3/4", "-1/5", "1/6"],
         "multi-harmonic lively tide"),
        # P3 cos tide for tangency scenarios: level = cos(pi t/12).
        ("P3", 1, "0", ["1", "0", "0", "0"], ["0", "0", "0", "0"],
         "cos tide cos(pi t/12)"),
        # P4 constant level 2/3 - all harmonics zero (allowed).
        ("P4", 1, "2/3", ["0", "0", "0", "0"], ["0", "0", "0", "0"],
         "constant 2/3 (all harmonics zero)"),
        # P5 constant exactly at a threshold used by a request (level = 1).
        ("P5", 1, "1", ["0", "0", "0", "0"], ["0", "0", "0", "0"],
         "constant exactly 1"),
        # P2 revision 2: corrected product.  b 1/2 -> 3/5 shifts the windows;
        # revision 1 stays queryable and unchanged.
        ("P2", 2, "3/5",
         ["3/2", "-1/2", "1/3", "1/4"],
         ["0", "3/4", "-1/5", "1/6"],
         "corrected product: b 1/2 -> 3/5"),
    ]
    return {"models": [
        {"port": p, "revision": r, "b": b, "a": a, "c": c, "note": note}
        for (p, r, b, a, c, note) in models
    ]}


def build_scenarios():
    """Channels and 30 requests covering all mandatory features."""
    channels: list = []
    requests: list = []

    def ch(name, segs):
        channels.append({"name": name, "segments": [
            {"port": p, "revision": ("current" if r is None else r),
             "depth": S(d), "duration": S(dur)}
            for (p, r, d, dur) in segs]})

    def req(rid, channel, draft, margin, S0, E0, note=""):
        requests.append({"id": rid, "channel": channel,
                         "draft": S(draft), "margin": S(margin),
                         "S": S(S0), "E": S(E0), "note": note})

    # C1 - single sine, hand-checkable.  depth 1/2, draft 1/2 => req 0.
    #   sin>=0 on [0,12]u[24,...]; 6h window fits s in [0,6]; the period end
    #   gives the isolated s=24 (window [24,30] only touches sin=0 at 24, then
    #   goes negative? no: sin>=0 on [24,36], so [24,30] is safe -> not isolated)
    #   -- see verify.py for the exact expected set.
    ch("C1_sine_hand", [("P1", 1, Fraction(1, 2), Fraction(6))])
    req("R01", "C1_sine_hand", Fraction(1, 2), 0, 0, 24,
        "hand-checkable single sine, sin>=0, 6h window")

    # C2 - pure tangency: cos>=0 safe width is exactly 12h; a 12h window only
    #   fits at the single instant s where window==[18,30]-style span.  After
    #   clipping to [0,24] -> isolated s=18.
    ch("C2_cos_tangent", [("P3", 1, Fraction(1, 2), Fraction(12))])
    req("R02", "C2_cos_tangent", Fraction(1, 2), 0, 0, 24,
        "cos>=0 safe width == 12h window -> isolated tangency")
    req("R03", "C2_cos_tangent", Fraction(1, 2), 0, 12, 36,
        "same tangency inside range [12,36]")

    # C3 - interior valley: rich tide splits the request into several windows.
    ch("C3_valley", [("P2", 1, Fraction(3, 4), Fraction(8))])
    req("R04", "C3_valley", Fraction(3, 2), Fraction(1, 5), 0, 48,
        "rich tide: interior valleys split into windows")
    req("R05", "C3_valley", Fraction(3, 2), Fraction(1, 5), 5, 29,
        "same channel, narrower range")
    req("R29", "C3_valley", Fraction(1, 2), 0, 0, 48,
        "shallow draft on rich tide -> wide windows")

    # C4 - different ports + real offsets.  seg1 sin [s,s+2], seg2 cos
    #   [s+2,s+4].  Correct feasible set differs from raw-window intersection.
    ch("C4_mixed_ports", [
        ("P1", 1, Fraction(1, 2), Fraction(2)),    # offset 0
        ("P3", 1, Fraction(1, 2), Fraction(2)),    # offset 2
    ])
    req("R06", "C4_mixed_ports", Fraction(1, 2), 0, 0, 24,
        "different ports + 2h offset; naive raw intersection is wrong")
    req("R07", "C4_mixed_ports", Fraction(1, 2), 0, 0, 48,
        "same mixed-offset channel over two periods")
    req("R26", "C4_mixed_ports", Fraction(1, 2), 0, 24, 48,
        "mixed-offset channel, second period")

    # C5 - constant model comfortably above threshold -> whole range feasible.
    ch("C5_const_ok", [("P4", 1, Fraction(0), Fraction(6))])   # req=0, level 2/3
    req("R08", "C5_const_ok", Fraction(1, 2), 0, 0, 24,
        "constant 2/3 >= req 1/2 -> whole [0,24] feasible")
    req("R30", "C5_const_ok", Fraction(1, 2), 0, 24, 48,
        "constant model over [24,48]")

    # C6 - constant exactly at threshold -> whole range feasible (equality ok).
    ch("C6_const_exact", [("P5", 1, Fraction(1, 2), Fraction(6))])  # req=1/2, lvl 1
    req("R09", "C6_const_exact", Fraction(1, 2), 0, 0, 24,
        "constant 1 > req 1/2 -> whole range feasible")
    req("R10", "C6_const_exact", Fraction(3, 2), 0, 0, 24,
        "constant exactly at threshold req=1 (equality) -> feasible")

    # C7 - constant below threshold -> INFEASIBLE.
    ch("C7_const_low", [("P4", 1, Fraction(0), Fraction(6))])   # req=1, level 2/3
    req("R11", "C7_const_low", Fraction(1), 0, 0, 24,
        "constant 2/3 < req 1 -> INFEASIBLE")
    req("R12", "C7_const_low", Fraction(1), 0, 24, 48,
        "same, second period -> INFEASIBLE")

    # C8 - length-8 channel, mixed ports, offsets accumulate over two periods.
    ch("C8_long", [
        ("P1", 1, Fraction(1, 2), Fraction(3)),
        ("P2", 1, Fraction(1), Fraction(4)),
        ("P3", 1, Fraction(1, 2), Fraction(2)),
        ("P1", 1, Fraction(1, 2), Fraction(3)),
        ("P2", 1, Fraction(1), Fraction(3)),
        ("P3", 1, Fraction(1, 2), Fraction(2)),
        ("P1", 1, Fraction(1, 2), Fraction(3)),
        ("P2", 1, Fraction(1), Fraction(2)),
    ])
    req("R13", "C8_long", Fraction(1, 2), 0, 0, 48,
        "8-segment channel, offsets accumulate over two periods")
    req("R28", "C8_long", Fraction(3, 2), 0, 12, 36,
        "8-segment channel, higher draft, shifted range")

    # C9/C10 - corrected-product consumption vs pinned old revision.
    ch("C9_p2_current", [("P2", None, Fraction(1), Fraction(6))])
    req("R14", "C9_p2_current", Fraction(3, 2), 0, 0, 48,
        "consumes CURRENT P2 (corrected product)")
    ch("C10_p2_rev1", [("P2", 1, Fraction(1), Fraction(6))])
    req("R15", "C10_p2_rev1", Fraction(3, 2), 0, 0, 48,
        "pinned to P2 rev 1; unaffected by correction")

    # C11 - short 1h windows, margin effect.
    ch("C11_short", [("P1", 1, Fraction(1, 2), Fraction(1))])
    req("R16", "C11_short", Fraction(1, 2), 0, 0, 24, "1h sine window")
    req("R17", "C11_short", Fraction(1, 2), Fraction(1, 10), 6, 30,
        "margin raises the required level")

    # C12 - two same-port segments, offsets 0 and 6.
    ch("C12_two_sine", [
        ("P1", 1, Fraction(1, 2), Fraction(6)),
        ("P1", 1, Fraction(1, 2), Fraction(6)),
    ])
    req("R18", "C12_two_sine", Fraction(1, 2), 0, 0, 24,
        "two sine segments, offsets 0 and 6")
    req("R19", "C12_two_sine", Fraction(1, 2), 0, 0, 48,
        "same over two periods")

    # C13 - deep drafts on the rich tide.
    ch("C13_p2_deep", [("P2", 1, Fraction(1), Fraction(4))])
    req("R20", "C13_p2_deep", Fraction(2), 0, 0, 48,
        "high draft on rich tide")
    req("R21", "C13_p2_deep", Fraction(3, 2), 0, 0, 48,
        "moderate draft on rich tide")
    req("R22", "C13_p2_deep", Fraction(3, 2), Fraction(1, 4), 0, 24,
        "margin tightens the windows")

    # C14 - cos windows.
    ch("C14_cos", [("P3", 1, Fraction(1, 2), Fraction(6))])
    req("R23", "C14_cos", Fraction(1, 2), 0, 0, 24,
        "cos>=0 with a 6h window")
    req("R24", "C14_cos", Fraction(1), 0, 0, 24,
        "cos>=1/2 -> windows near crests t=0,24")

    # C15 - mixed sine/cos/constant, tight offsets.
    ch("C15_mixed_tight", [
        ("P1", 1, Fraction(1, 2), Fraction(2)),
        ("P3", 1, Fraction(1, 2), Fraction(2)),
        ("P5", 1, Fraction(1, 2), Fraction(2)),
    ])
    req("R25", "C15_mixed_tight", Fraction(1, 2), 0, 0, 48,
        "mixed sine/cos/constant, tight offsets")
    req("R27", "C1_sine_hand", Fraction(1, 2), Fraction(1, 10), 12, 36,
        "sine with margin, second period")

    return {"channels": channels, "requests": requests}


def main():
    os.makedirs(DATA, exist_ok=True)
    cat = build_catalog()
    sc = build_scenarios()
    # sanity: unique channel names and 30 requests
    names = [c["name"] for c in sc["channels"]]
    assert len(names) == len(set(names)), f"duplicate channel names: {names}"
    assert len(sc["requests"]) == 30, len(sc["requests"])
    with open(os.path.join(DATA, "catalog.json"), "w") as f:
        json.dump(cat, f, indent=2)
    with open(os.path.join(DATA, "scenarios.json"), "w") as f:
        json.dump(sc, f, indent=2)
    print(f"wrote {len(cat['models'])} model revisions, "
          f"{len(sc['channels'])} channels, {len(sc['requests'])} requests")


if __name__ == "__main__":
    main()
