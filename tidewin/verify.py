"""Independent verification suite.

Two kinds of checks:

1. **Hand-computed exact cases.**  Expected feasible sets are derived by hand
   from the closed-form levels.  The solver's boundaries must match to 1e-7 h,
   isolated points must be preserved, and touching intervals must be merged.

2. **Independent falsifier (dense grid).**  For every reported feasible window
   we *confirm* its interior sample points are feasible, and for every reported
   unsafe gap we sample it densely to look for *any* feasible point.  A dense
   grid can only *refute* a result; it never *proves* a boundary.  The proof is
   the exact root/eversion algorithm in :mod:`tidewin.vault`.  We also compare
   against a *naive* raw-port-window intersection for the different-port channel
   to demonstrate that the sailing offset genuinely changes the answer.

Run with ``python -m tidewin.cli verify`` (or ``python -m tidewin.verify``).
"""
from __future__ import annotations

import math
import sys
from fractions import Fraction

from .catalog import TideCatalog
from .rational import F
from .vault import (Channel, Segment, Vessel, solve_request,
                    check_window_point, safe_set, erode_by_duration)

TOL = 1e-7
fails = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        fails.append(f"{name}: {detail}")
        print(f"  FAIL {name}: {detail}")
    else:
        print(f"  ok   {name}")


def approx(a, b, tol=TOL):
    return abs(a - b) <= tol


def intervals_match(windows, expected, tol=TOL):
    """Compare solved windows (list of start_boundary dicts' windows) to
    expected list of (a,b) with isolated points as a==b."""
    got = [(w["start"], w["end"]) for w in windows]
    exp = sorted(expected)
    if len(got) != len(exp):
        return False, f"count {len(got)} != {len(exp)}: got={got} exp={exp}"
    for (ga, gb), (ea, eb) in zip(sorted(got), exp):
        if not (approx(ga, ea, tol) and approx(gb, eb, tol)):
            return False, f"got {got} != exp {exp}"
    return True, ""


def make_catalog():
    cat = TideCatalog()
    cat.publish("P1", 0, [0, 0, 0, 0], [1, 0, 0, 0])       # sin(th)
    cat.publish("P2", F(1, 2), ["3/2", "-1/2", "1/3", "1/4"],
                ["0", "3/4", "-1/5", "1/6"])
    cat.publish("P3", 0, [1, 0, 0, 0], [0, 0, 0, 0])       # cos(th)
    cat.publish("P4", F(2, 3), [0, 0, 0, 0], [0, 0, 0, 0])
    cat.publish("P5", 1, [0, 0, 0, 0], [0, 0, 0, 0])
    return cat


# --------------------------------------------------------------------------- #
# 1. Hand-computed single-sine case
# --------------------------------------------------------------------------- #
def verify_single_sine():
    print("\n[1] single sine  level = sin(pi t/12), depth=1/2 draft=1/2 -> req=0")
    cat = make_catalog()
    # safe set sin>=0 over [0,48]: [0,12] u [24,36] (period 24)
    seg = Segment("P1", None, F(1, 2), F(6))
    ch = Channel("sine6", [seg])
    v = Vessel(F(1, 2), F(0))
    res = solve_request(cat, ch, v, 0, 48)
    # window [s,s+6] must lie in sin>=0.  Over [0,48] the safe set is
    # [0,12]u[24,36]u[48,60]; hence starts [0,6], [24,30] and (clipped by the
    # request end) the isolated s=48.
    expected = [(0.0, 6.0), (24.0, 30.0), (48.0, 48.0)]
    ok, d = intervals_match(res["windows"], expected)
    check("single-sine windows", ok, d)
    for s in (0.0, 3.0, 6.0, 24.0, 30.0):
        c = check_window_point(cat, ch, v, s)
        check(f"single-sine s={s} feasible", c["feasible"])
    for s in (7.0, 12.0, 13.0, 23.0, 31.0):
        c = check_window_point(cat, ch, v, s)
        check(f"single-sine s={s} infeasible", not c["feasible"])


# --------------------------------------------------------------------------- #
# 2. Pure tangency (isolated feasible instant)
# --------------------------------------------------------------------------- #
def verify_tangency():
    print("\n[2] tangency: cos>=0 safe width exactly 12h, 12h window")
    cat = make_catalog()
    seg = Segment("P3", None, F(1, 2), F(12))
    ch = Channel("cos12", [seg])
    v = Vessel(F(1, 2), F(0))       # req 0, cos>=0 safe [18,30] within [0,48]...
    res = solve_request(cat, ch, v, 0, 24)
    # cos(th)>=0 for th in [-pi/2,pi/2] -> t in [-6,6] mod 24 = [18,24]u[0,6].
    # A 12h window needs [u,u+12] fully inside cos>=0; only u=18 works
    # (window [18,30]) but 30>24... within [0,24]: window start s in [0,24],
    # need [s,s+12] inside cos>=0. cos>=0 on [18,30] (i.e. [-6,6] mod 24).
    # [s,s+12] inside [18,30]? s>=18,s+12<=30 => s=18 exactly. Also s+12<=6
    # impossible for s>=0 except s=-12. So isolated s=18.
    expected = [(18.0, 18.0)]
    ok, d = intervals_match(res["windows"], expected)
    check("tangency isolated point", ok, d)
    check("tangency flagged isolated", all(w["isolated"] for w in res["windows"]))
    c = check_window_point(cat, ch, v, 18.0)
    check("tangency s=18 feasible (equality)", c["feasible"],
          f"minres={min(x['residual'] for x in c['constraints'])}")
    c = check_window_point(cat, ch, v, 18.0 + 1e-4)
    check("tangency s=18+eps infeasible", not c["feasible"])
    c = check_window_point(cat, ch, v, 18.0 - 1e-4)
    check("tangency s=18-eps infeasible", not c["feasible"])

    # A genuine tangency off the coordinate axes: level = -1/2 + 2cos - cos(2th),
    # which touches req=1 at t=4 and t=20 (double / tangent roots) and lies below
    # everywhere else.  The safe set must be exactly the two isolated points.
    cat.publish("T", F(-1, 2), [F(2), F(-1), 0, 0], [0, 0, 0, 0])
    mt = cat.get_model("T")
    ss = safe_set(mt, F(1), 0.0, 24.0)
    check("off-axis tangency -> isolated safe points",
          all(a == b for a, b in ss) and len(ss) == 2, str(ss))
    roots = mt.level_roots(F(1), 0.0, 24.0)
    check("off-axis tangency roots at 4,20",
          len(roots) == 2 and approx(roots[0], 4.0) and approx(roots[1], 20.0),
          str(roots))
    # any positive-duration window cannot fit inside an isolated point
    chT = Channel("tang", [Segment("T", None, F(0), F(1, 2))])
    vT = Vessel(F(1, 2), F(1, 2))   # req = 1/2+1/2-0 = 1
    resT = solve_request(cat, chT, vT, 0, 24)
    check("off-axis tangency positive-duration -> INFEASIBLE",
          resT["status"] == "INFEASIBLE", str(resT["windows"]))


# --------------------------------------------------------------------------- #
# 3. Interior valley (rich tide): windows split, and grid finds no leak
# --------------------------------------------------------------------------- #
def verify_interior_valley():
    print("\n[3] interior valley on a rich tide (P2) + dense falsifier")
    cat = make_catalog()
    ch = Channel("valley", [Segment("P2", 1, F(3, 4), F(8))])
    v = Vessel(F(3, 2), F(1, 5))    # req = 3/2+1/5-3/4 = 19/20
    res = solve_request(cat, ch, v, 0, 48)
    check("valley has windows", res["n_windows"] >= 1,
          f"n={res['n_windows']}")
    check("valley splits into >1 window", res["n_windows"] >= 2,
          f"n={res['n_windows']}")
    # falsifier: sample interior of every window must be feasible; sample gaps
    # must not contain feasible points (dense grid only refutes).
    ok_gap, bad = _falsify_grid(cat, ch, v, 0.0, 48.0, res["windows"], step=0.002)
    check("valley dense grid: no feasible point in unsafe gaps", ok_gap, bad)


# --------------------------------------------------------------------------- #
# 4. Different ports with offset: must not intersect raw port windows
# --------------------------------------------------------------------------- #
def verify_mixed_ports():
    print("\n[4] different ports + 2h sailing offset (P1 then P3)")
    cat = make_catalog()
    seg1 = Segment("P1", None, F(1, 2), F(2))     # sin [s,s+2], req 0
    seg2 = Segment("P3", None, F(1, 2), F(2))     # cos [s+2,s+4], req 0
    ch = Channel("mixed", [seg1, seg2])
    v = Vessel(F(1, 2), F(0))
    res = solve_request(cat, ch, v, 0, 24)
    # Correct: sin-safe for [s,s+2] and cos-safe for [s+2,s+4].
    # sin>=0 over [0,12] (and 24): [s,s+2] inside => s in [0,10] (plus s=24).
    # cos>=0 over [18,24]u[0,6]: [s+2,s+4] inside => s in [16,22] or [0,2].
    # Intersect with the sin-starts: [0,2] (and s=24, clipped to a point).
    #   ([16,22] is excluded because seg1 sin is negative there.)
    expected = [(0.0, 2.0), (24.0, 24.0)]
    ok, d = intervals_match(res["windows"], expected)
    check("mixed-offset windows", ok, d)
    # Under a naive "intersect raw port windows ignoring offsets" the answer
    # would be the intersection of {s: sin-safe window} and {s: cos-safe}, which
    # is a *different* set.  Show the solver differs from that naive set.
    naive = _naive_raw_intersection(cat, ch, v, 0.0, 24.0)
    got = [(w["start"], w["end"]) for w in res["windows"]]
    check("solver differs from naive raw-window intersection",
          _sets_differ(got, naive), f"got={got} naive={naive}")
    # falsifier across the range
    ok_gap, bad = _falsify_grid(cat, ch, v, 0.0, 24.0, res["windows"], step=0.001)
    check("mixed-offset dense grid: no feasible point in gaps", ok_gap, bad)


def _naive_raw_intersection(cat, ch, v, S, E):
    """Deliberately-wrong reference: intersect each segment's *raw* safe
    departure set (ignoring per-segment offset), to show the solver is correct
    to differ."""
    from .vault import intersect_sets, merge_intervals
    naive = [(S, E)]
    for seg in ch.segments:
        req = v.draft + v.margin - seg.depth
        model = cat.get_model(seg.port, seg.revision)
        safe = safe_set(model, req, S, E + float(seg.duration))
        starts = merge_intervals(erode_by_duration(safe, float(seg.duration)))
        naive = intersect_sets(naive, starts)
    return merge_intervals(naive)


def _sets_differ(A, B):
    if len(A) != len(B):
        return True
    for (a1, a2), (b1, b2) in zip(sorted(A), sorted(B)):
        if abs(a1 - b1) > 1e-6 or abs(a2 - b2) > 1e-6:
            return True
    return False


# --------------------------------------------------------------------------- #
# 5. Constant models (all-zero harmonics, exactly at threshold)
# --------------------------------------------------------------------------- #
def verify_constants():
    print("\n[5] constant models (all-zero harmonics; exactly at threshold)")
    cat = make_catalog()
    # P4 level 2/3, req 0 -> whole range feasible
    ch = Channel("const", [Segment("P4", None, F(0), F(6))])
    v = Vessel(F(1, 2), F(0))
    res = solve_request(cat, ch, v, 0, 24)
    ok, d = intervals_match(res["windows"], [(0.0, 24.0)])
    check("constant feasible whole range", ok, d)
    # P5 level 1, req exactly 1 -> equality feasible
    ch2 = Channel("exact", [Segment("P5", None, F(1, 2), F(6))])
    v2 = Vessel(F(3, 2), F(0))      # req = 3/2 - 1/2 = 1 == level
    res2 = solve_request(cat, ch2, v2, 0, 24)
    ok, d = intervals_match(res2["windows"], [(0.0, 24.0)])
    check("constant exactly at threshold feasible", ok, d)
    # P4 level 2/3, req 1 -> infeasible
    v3 = Vessel(F(1), F(0))
    ch3 = Channel("low", [Segment("P4", None, F(0), F(6))])
    res3 = solve_request(cat, ch3, v3, 0, 24)
    check("constant below threshold INFEASIBLE", res3["status"] == "INFEASIBLE",
          f"windows={res3['windows']}")
    # constant exactly-touch with all-zero tension: P5 level 1, req 1 first
    # segment and a nonconstant second -> still feasible whole range on seg1
    check("constant zero-depth zero-harmonics extrema empty",
          cat.get_model("P4").base_critical_times() == [],
          str(cat.get_model("P4").base_critical_times()))


# --------------------------------------------------------------------------- #
# 6. Interval merging at a single touching point
# --------------------------------------------------------------------------- #
def verify_merge_touch():
    print("\n[6] touching intervals merge; genuine positive gaps preserved")
    from .vault import merge_intervals, intersect_sets
    # [0,5] and [5,10] touch at 5 -> must merge into [0,10]
    merged = merge_intervals([(0.0, 5.0), (5.0, 10.0)])
    check("touching intervals merge", merged == [(0.0, 10.0)], str(merged))
    # a genuine unsafe gap of 1e-3 must NOT be erased
    gapped = merge_intervals([(0.0, 5.0), (5.0 + 1e-3, 10.0)])
    check("positive-width gap preserved", gapped == [(0.0, 5.0),
          (5.0 + 1e-3, 10.0)], str(gapped))
    # a gap at the touching tolerance (~1e-9) is treated as touching
    merged2 = merge_intervals([(0.0, 5.0), (5.0 + 1e-10, 10.0)])
    check("sub-tolerance gap treated as touching",
          merged2 == [(0.0, 10.0)], str(merged2))
    # intersection that yields two pieces meeting at a point merges
    A = [(0.0, 5.0), (5.0, 10.0)]
    B = [(2.0, 8.0)]
    inter = intersect_sets(A, B)
    # [0,5]∩[2,8]=[2,5] and [5,10]∩[2,8]=[5,8]; they touch at 5 -> [2,8]
    check("intersection merges at touch", inter == [(2.0, 8.0)], str(inter))

    cat = make_catalog()
    # sin>=-1 everywhere -> single merged interval over [0,48]
    ch = Channel("all", [Segment("P1", None, F(3, 2), F(6))])
    v = Vessel(F(1, 2), F(0))       # req = 1/2+0-3/2 = -1, sin>=-1 always
    res = solve_request(cat, ch, v, 0, 48)
    ok, d = intervals_match(res["windows"], [(0.0, 48.0)])
    check("sin>=-1 everywhere -> single merged interval", ok, d)


# --------------------------------------------------------------------------- #
# dense grid falsifier
# --------------------------------------------------------------------------- #
def _falsify_grid(cat, ch, v, S, E, windows, step):
    """Use exact per-point feasibility to scan a dense grid.  Returns
    (no_leak, detail): every grid point inside a reported window must be
    feasible; every grid point outside all windows must be infeasible."""
    n = max(1, int((E - S) / step))
    for k in range(n + 1):
        s = S + k * (E - S) / n
        c = check_window_point(cat, ch, v, s)
        inside = any(w["start"] - 1e-9 <= s <= w["end"] + 1e-9
                     for w in windows)
        if inside and not c["feasible"]:
            return False, f"window says s={s:.6f} feasible but check failed"
        if not inside and c["feasible"]:
            # allow a tiny neighborhood of a true boundary (grid may land on it)
            return False, f"gap contains feasible s={s:.6f}"
    return True, ""


# --------------------------------------------------------------------------- #
# 7. Revision publishing: old analysis unchanged, new product shifts windows
# --------------------------------------------------------------------------- #
def verify_revisions():
    print("\n[7] revision publish: old analysis unchanged, corrected product shifts")
    cat = make_catalog()
    # P4 rev 1 exists; publish rev 2 with b 1/2 (change from 2/3)
    cat.publish("P4", F(1, 2), [0, 0, 0, 0], [0, 0, 0, 0])
    ch_rev1 = Channel("r1", [Segment("P4", 1, F(0), F(6))])
    ch_cur = Channel("cur", [Segment("P4", None, F(0), F(6))])
    v = Vessel(F(1, 2), F(0))       # req 1/2
    r_before = solve_request(cat, ch_rev1, v, 0, 24)
    r_cur = solve_request(cat, ch_cur, v, 0, 24)
    check("rev1 pinned still feasible (level 2/3>=1/2)",
          r_before["status"] == "FEASIBLE")
    # current rev2 level 1/2 == req -> equality feasible (whole range)
    check("current rev2 at equality feasible",
          r_cur["status"] == "FEASIBLE")
    # Now a *corrected* product that makes it unsafe for this vessel:
    cat.publish("P4", F(1, 4), [0, 0, 0, 0], [0, 0, 0, 0])
    r_new = solve_request(cat, ch_cur, v, 0, 24)
    check("corrected product (level 1/4) -> INFEASIBLE",
          r_new["status"] == "INFEASIBLE")
    # old pinned rev1 result must be reproduced identically
    r_after = solve_request(cat, ch_rev1, v, 0, 24)
    check("pinned rev1 analysis unchanged after publish",
          [(w["start"], w["end"]) for w in r_after["windows"]]
          == [(w["start"], w["end"]) for w in r_before["windows"]])


# --------------------------------------------------------------------------- #
# 8. Full dataset: boundaries' residuals are ~0, feasibility consistent
# --------------------------------------------------------------------------- #
def verify_dataset():
    print("\n[8] full 30-request dataset: boundary residuals + internal consistency")
    from .run import run_all
    results = run_all()
    for r in results:
        for w in r["windows"]:
            for lbl in ("start_boundary", "end_boundary"):
                b = w[lbl]
                if b and b["kind"] == "tide":
                    check(f"{r['id']} {lbl} tide residual small",
                          abs(b["residual"]) <= 1e-7,
                          f"residual={b['residual']}")
            # every window must be feasible at its midpoint and boundaries
            sc = _scenario_ctx(r["id"])
            for s in (w["start"], 0.5 * (w["start"] + w["end"]), w["end"]):
                c = check_window_point(sc["cat"], sc["ch"], sc["v"], s)
                check(f"{r['id']} window point s={s:.5f} feasible",
                      c["feasible"])
    check("dataset has 30 requests", len(results) == 30, str(len(results)))


_CTX_CACHE = {}


def _scenario_ctx(rid):
    if rid in _CTX_CACHE:
        return _CTX_CACHE[rid]
    from .io import channel_from_dict, request_from_dict
    from .run import load_catalog, load_scenarios
    cat = load_catalog()
    sc = load_scenarios()
    rd = next(x for x in sc["requests"] if x["id"] == rid)
    req = request_from_dict(rd)
    ch = next(channel_from_dict(c) for c in sc["channels"]
              if c["name"] == req["channel"])
    ctx = {"cat": cat, "ch": ch, "v": req["vessel"]}
    _CTX_CACHE[rid] = ctx
    return ctx


# --------------------------------------------------------------------------- #
# 9. Whole-dataset dense falsifier (opt-in via --falsify; can only refute)
# --------------------------------------------------------------------------- #
def verify_dense_falsifier(step=0.01):
    print(f"\n[9] whole-dataset dense falsifier (grid step {step}h, refutation only)")
    from .io import channel_from_dict, request_from_dict
    from .run import load_catalog, load_scenarios
    from .vault import check_window_point_fast, solve_request
    cat = load_catalog()
    sc = load_scenarios()
    channels = {c["name"]: channel_from_dict(c) for c in sc["channels"]}
    total = 0
    mism = 0
    for rd in sc["requests"]:
        req = request_from_dict(rd)
        ch = channels[req["channel"]]
        v = req["vessel"]
        res = solve_request(cat, ch, v, req["S"], req["E"])
        S, E = float(req["S"]), float(req["E"])
        wins = [(w["start"], w["end"]) for w in res["windows"]]
        n = max(1, int((E - S) / step))
        for k in range(n + 1):
            s = S + k * (E - S) / n
            total += 1
            feas = check_window_point_fast(cat, ch, v, s)
            inside = any(a - 1e-9 <= s <= b + 1e-9 for a, b in wins)
            if feas != inside and not any(abs(s - x) < 2e-6
                                          for a, b in wins for x in (a, b)):
                mism += 1
                if mism <= 5:
                    print(f"    mismatch {rd['id']} s={s:.6f} "
                          f"solver_inside={inside} grid_feasible={feas}")
    check(f"dense falsifier over {total} grid points: no leak", mism == 0,
          f"{mism} mismatches")


# --------------------------------------------------------------------------- #
def main(argv=None):
    import sys
    falsify = "--falsify" in (argv if argv is not None else sys.argv[1:])
    falsify_step = 0.01
    for a in (argv if argv is not None else sys.argv[1:]):
        if a.startswith("--falsify-step="):
            falsify_step = float(a.split("=", 1)[1])
    print("=" * 72)
    print("tidewin verification suite (exact roots + independent grid falsifier)")
    print("=" * 72)
    verify_single_sine()
    verify_tangency()
    verify_interior_valley()
    verify_mixed_ports()
    verify_constants()
    verify_merge_touch()
    verify_revisions()
    verify_dataset()
    if falsify:
        verify_dense_falsifier(falsify_step)
    print("\n" + "=" * 72)
    if fails:
        print(f"RESULT: {len(fails)} FAILURES / {checks} checks")
        for f in fails:
            print("  -", f)
        return 1
    print(f"RESULT: ALL {checks} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
