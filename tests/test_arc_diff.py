"""End-to-end tests for arc service + differential warehouse.

Covers, per the brief:
* same-wavelength multi-solution ambiguity,
* dual-wavelength uniqueness,
* lock loss -> INCOMPLETE,
* interval gap (within G continues, beyond G cuts),
* closed displacement bound (endpoints kept),
* cycle-slip correction changing the answer while old tasks keep old products,
* brute-force cross-check of the exact lattice solver on small examples,
* huge solution sets: exact count + per-epoch extremes beyond the 1000 cap.
"""

from __future__ import annotations

import itertools
from fractions import Fraction

import pytest

from gnss_arc import (
    ArcService,
    DifferentialWarehouse,
    DiffTask,
    Store,
    Q,
    solve_differential,
    OUTPUT_CAP,
)
from gnss_arc.synthetic import build_same_wavelength, build_dual_wavelength

F = Fraction


def solve_scenario(scn, task, gap=1, **kw):
    svc = ArcService(scn.store)
    prods = {}
    for st in scn.stations:
        for ch in scn.channels:
            prods[(st, ch)] = svc.build(st, ch, gap=gap, persist=True)
    return solve_differential(task, prods, **kw)


# --------------------------------------------------------------------------- #
# 1. same wavelength -> ambiguity
# --------------------------------------------------------------------------- #
def test_same_wavelength_is_ambiguous_but_exact():
    scn = build_same_wavelength(
        epochs=10, lam=F(1, 100), displacement=0, n1=3, n2=3,
        dlo=0, dhi=F(1, 10),
    )
    task = DiffTask.make(
        "ROV", "BASE", 1, 2, F(1, 100), F(1, 100),
        win_lo=1, win_hi=10, dlo=0, dhi=F(1, 10),
    )
    res = solve_scenario(scn, task)
    assert res.status == "AMBIGUOUS"
    # d = lam*(z-n) = (3-n)/100 in [0, 1/10]  =>  n in [-7, 3]
    assert res.n1_range == (-7, 3)
    assert res.n2_range == (-7, 3)
    # cross-channel forces n1 == n2 (same lambda, z1 == z2): 11 pairs
    assert res.solution_count == 11
    assert [(s.n1, s.n2) for s in res.solutions] == [(n, n) for n in range(-7, 4)]
    # the authored truth (n=3) is among them and reproduces displacement 0
    truth = [(s.n1, s.n2) for s in res.solutions if s.displacement_dict()[1] == 0]
    assert truth == [(3, 3)]


# --------------------------------------------------------------------------- #
# 2. dual wavelength -> unique
# --------------------------------------------------------------------------- #
def test_dual_wavelength_is_unique():
    scn = build_dual_wavelength(
        epochs=6, lam1=F(1, 5), lam2=F(1, 7), displacement=F(1, 10),
        n1=2, n2=1, dlo=0, dhi=F(1, 2),
    )
    task = DiffTask.make(
        "ROV", "BASE", 1, 2, F(1, 5), F(1, 7),
        win_lo=1, win_hi=6, dlo=0, dhi=F(1, 2),
    )
    res = solve_scenario(scn, task)
    assert res.status == "UNIQUE"
    assert res.solution_count == 1
    assert (res.solutions[0].n1, res.solutions[0].n2) == (2, 1)
    assert all(d == F(1, 10) for _e, d in res.solutions[0].displacements)


# --------------------------------------------------------------------------- #
# 3. lock loss -> INCOMPLETE (no window shrinking)
# --------------------------------------------------------------------------- #
def test_lock_loss_cuts_arc_and_incompletes_task():
    scn = build_dual_wavelength(
        epochs=6, displacement=F(1, 10), lock_drop=4, dlo=0, dhi=F(1, 2),
    )
    svc = ArcService(scn.store)
    prod = svc.build("ROV", 1, gap=1)
    # epoch 4 unusable, arcs split 1..3 and 5..6
    assert prod.arc_id_at(3) is not None
    assert not prod.usable_at(4)
    assert prod.arc_id_at(3) != prod.arc_id_at(5)
    seg = [s for s in prod.segments if s.start_epoch == 5][0]
    assert seg.start_reason == "NO_LOCK"

    task = DiffTask.make(
        "ROV", "BASE", 1, 2, F(1, 5), F(1, 7),
        win_lo=1, win_hi=6, dlo=0, dhi=F(1, 2),
    )
    res = solve_scenario(scn, task)
    assert res.status == "INCOMPLETE"
    assert res.solution_count == 0


def test_arc_change_inside_window_incompletes_even_if_epochs_usable():
    scn = build_dual_wavelength(epochs=6, displacement=F(1, 10), dlo=0, dhi=F(1, 2))
    # push a jump at epoch 4 but also "correct" it so phase continuity returns
    scn.store.add_jump("ROV", 1, 4, 0)  # a zero-magnitude event still cuts the arc
    task = DiffTask.make(
        "ROV", "BASE", 1, 2, F(1, 5), F(1, 7),
        win_lo=1, win_hi=6, dlo=0, dhi=F(1, 2),
    )
    res = solve_scenario(scn, task)
    assert res.status == "INCOMPLETE"
    assert "arc id" in res.reason


# --------------------------------------------------------------------------- #
# 4. interval gap behaviour around G
# --------------------------------------------------------------------------- #
def test_gap_within_G_continues_beyond_G_cuts():
    s2 = build_dual_wavelength(epochs=6, displacement=F(1, 10), dlo=0, dhi=F(1, 2))
    s2.store.conn.execute(
        "DELETE FROM observations WHERE station='ROV' AND channel=1 AND epoch=3"
    )
    s2.store.conn.commit()
    prod = ArcService(s2.store).build("ROV", 1, gap=2)
    # spacing 2 -> 4 is <= G=2 -> no cut, still one arc across the hole
    assert prod.arc_id_at(2) == prod.arc_id_at(4)
    prod_cut = ArcService(s2.store).build("ROV", 1, gap=1)
    assert prod_cut.arc_id_at(2) != prod_cut.arc_id_at(4)
    seg = [s for s in prod_cut.segments if s.start_epoch == 4][0]
    assert seg.start_reason == "GAP"


# --------------------------------------------------------------------------- #
# 5. closed bound: endpoints kept
# --------------------------------------------------------------------------- #
def test_closed_bound_keeps_endpoints():
    # lam = 1, z = 5, n in {4,5} for d in [0,1]; both endpoints d=0 and d=1 included
    scn = build_same_wavelength(
        epochs=4, lam=F(1), displacement=0, n1=5, n2=5, dlo=0, dhi=1,
    )
    task = DiffTask.make(
        "ROV", "BASE", 1, 2, F(1), F(1), win_lo=1, win_hi=4, dlo=0, dhi=1,
    )
    res = solve_scenario(scn, task)
    assert res.status == "AMBIGUOUS"
    assert res.n1_range == (4, 5)
    got = {(s.n1, s.n2): s.displacement_dict()[1] for s in res.solutions}
    assert got == {(4, 4): F(1), (5, 5): F(0)}


def test_bound_inclusive_equality_allowed():
    # z1 = 3, lam=1/10 -> d = (3-n)/10. bound [0, 1/5] -> n in {1,2,3}
    scn = build_same_wavelength(
        epochs=3, lam=F(1, 10), displacement=0, n1=3, n2=3, dlo=0, dhi=F(1, 5),
    )
    task = DiffTask.make(
        "ROV", "BASE", 1, 2, F(1, 10), F(1, 10), win_lo=1, win_hi=3, dlo=0, dhi=F(1, 5),
    )
    res = solve_scenario(scn, task)
    assert res.n1_range == (1, 3)
    assert res.solution_count == 3
    ds = sorted(s.displacement_dict()[1] for s in res.solutions)
    assert ds == [F(0), F(1, 10), F(1, 5)]  # both endpoints present


# --------------------------------------------------------------------------- #
# 6. cycle-slip correction -> new product, old task keeps old product
# --------------------------------------------------------------------------- #
def test_jump_correction_changes_answer_and_old_result_is_stable():
    scn = build_dual_wavelength(
        epochs=6, displacement=F(1, 10), n1=2, n2=1, dlo=0, dhi=F(1, 2),
    )
    store = scn.store
    svc = ArcService(store)
    prod_before = svc.build("ROV", 1, gap=1, persist=True)
    pid_before = prod_before.product_id

    # Correct the data by declaring a spurious jump event mid-window: this cuts
    # the arc, so a fresh product becomes INCOMPLETE for the same window.
    ev = store.add_jump("ROV", 1, 4, 1)
    prod_after = svc.build("ROV", 1, gap=1, persist=True)
    assert prod_after.product_id != pid_before
    assert prod_after.arc_id_at(3) != prod_after.arc_id_at(4)
    assert prod_after.state(4).source_event == ev

    task = DiffTask.make(
        "ROV", "BASE", 1, 2, F(1, 5), F(1, 7),
        win_lo=1, win_hi=6, dlo=0, dhi=F(1, 2),
    )
    wh = DifferentialWarehouse(store)
    res_after = wh.solve(task)
    assert res_after.status == "INCOMPLETE"

    # Old product snapshot still yields the old, valid UNIQUE answer.
    old_prods = {
        ("ROV", 1): svc.load("ROV", 1, product_id=pid_before),
        ("ROV", 2): svc.load("ROV", 2),
        ("BASE", 1): svc.load("BASE", 1),
        ("BASE", 2): svc.load("BASE", 2),
    }
    res_old = solve_differential(task, old_prods)
    assert res_old.status == "UNIQUE"
    assert (res_old.solutions[0].n1, res_old.solutions[0].n2) == (2, 1)


def test_supersede_jump_restores_answer():
    scn = build_dual_wavelength(epochs=6, displacement=F(1, 10), n1=2, n2=1,
                                dlo=0, dhi=F(1, 2))
    store = scn.store
    svc = ArcService(store)
    ev = store.add_jump("ROV", 1, 4, 1)
    assert svc.build("ROV", 1, gap=1, persist=False).arc_id_at(3) != \
        svc.build("ROV", 1, gap=1, persist=False).arc_id_at(4)
    # Correcting the event away restores continuity (and the unique pair).
    store.supersede_jump(ev)
    prod = svc.build("ROV", 1, gap=1, persist=True)
    assert prod.arc_id_at(3) == prod.arc_id_at(4)
    prods = {
        ("ROV", 1): prod,
        ("ROV", 2): svc.build("ROV", 2, gap=1),
        ("BASE", 1): svc.build("BASE", 1, gap=1),
        ("BASE", 2): svc.build("BASE", 2, gap=1),
    }
    task = DiffTask.make("ROV", "BASE", 1, 2, F(1, 5), F(1, 7),
                         win_lo=1, win_hi=6, dlo=0, dhi=F(1, 2))
    res = solve_differential(task, prods)
    assert res.status == "UNIQUE"
    assert (res.solutions[0].n1, res.solutions[0].n2) == (2, 1)


# --------------------------------------------------------------------------- #
# 7. brute-force cross-check of lattice solver
# --------------------------------------------------------------------------- #
def _brute(task, z1, z2):
    A, B = task.lambda1, task.lambda2
    feasible = []
    for n1 in range(-50, 51):
        for n2 in range(-50, 51):
            ok = True
            for e in task.epochs:
                d = A * (z1[e] - n1)
                if d != B * (z2[e] - n2) or not (task.dlo <= d <= task.dhi):
                    ok = False
                    break
            if ok:
                feasible.append((n1, n2))
    return sorted(feasible)


def test_lattice_matches_bruteforce_random_small():
    import random
    rng = random.Random(1234)
    for trial in range(200):
        lam1 = Fraction(rng.randint(1, 6), rng.randint(1, 6))
        lam2 = Fraction(rng.randint(1, 6), rng.randint(1, 6))
        n1, n2 = rng.randint(0, 6), rng.randint(0, 6)
        dval = Fraction(rng.randint(0, 10), 10)
        epochs = list(range(1, rng.randint(1, 5) + 1))
        # build consistent z from the truth
        z1 = {e: dval / lam1 + n1 for e in epochs}
        z2 = {e: dval / lam2 + n2 for e in epochs}
        dlo = Fraction(rng.randint(-5, 5), 10)
        dhi = dlo + Fraction(rng.randint(0, 20), 10)
        task = DiffTask.make("R", "B", 1, 2, lam1, lam2, epochs[0], epochs[-1], dlo, dhi)

        prods = {
            ("R", 1): _prod("R", 1, {e: z1[e] for e in epochs}),
            ("B", 1): _prod("B", 1, {e: Fraction(0) for e in epochs}),
            ("R", 2): _prod("R", 2, {e: z2[e] for e in epochs}),
            ("B", 2): _prod("B", 2, {e: Fraction(0) for e in epochs}),
        }
        res = solve_differential(task, prods)
        bf = _brute(task, z1, z2)
        if res.is_solved:
            got = sorted((s.n1, s.n2) for s in res.solutions)
            assert got == bf, (trial, got, bf)
            assert res.solution_count == len(bf)
        else:
            assert res.status == "INCONSISTENT"
            assert bf == []


def _prod(station, channel, phases):
    from gnss_arc.arc import ArcProduct, ArcSegment, EpochState
    eps = {
        e: EpochState(
            epoch=e, raw_phase=p, lock=True, cumulative_jump=0,
            corrected_phase=p, usable=True, arc_id=1,
            start_reason=("FEED_START" if e == min(phases) else None),
            source_event=None,
        )
        for e, p in phases.items()
    }
    seg = ArcSegment(
        arc_id=1, start_epoch=min(phases), end_epoch=max(phases),
        n_epochs=len(phases), start_reason="FEED_START", end_reason="END_OF_DATA",
        source_event=None,
    )
    return ArcProduct(
        station=station, channel=channel, gap=1, event_level=0,
        obs_stamp="test", epochs=eps, segments=(seg,),
    )


# --------------------------------------------------------------------------- #
# 8. huge solution sets: exact count + per-epoch extremes beyond cap
# --------------------------------------------------------------------------- #
def test_large_solution_set_count_and_extremes():
    # lam=1/1000, z=500, bound [0,1] -> n in [0, 500]? d=(500-n)/1000 -> n in [499,500]
    # want a big range: lam=1, z=500, bound [0,1000] -> n in [0,500] -> ~501
    scn = build_same_wavelength(
        epochs=3, lam=F(1), displacement=0, n1=500, n2=500, dlo=0, dhi=1000,
    )
    task = DiffTask.make("ROV", "BASE", 1, 2, F(1), F(1),
                         win_lo=1, win_hi=3, dlo=0, dhi=1000)
    res = solve_scenario(scn, task)
    # n1 == n2 in [0,1000] -> 1001 solutions, just over the cap
    assert res.solution_count == 1001
    assert res.truncated is True
    assert len(res.solutions) == OUTPUT_CAP
    # per-epoch extremes: d in [0,1000]
    assert res.per_epoch_extremes[1] == (F(0), F(1000))


def test_over_cap_still_exact():
    # build with a genuinely huge count
    scn = build_same_wavelength(
        epochs=2, lam=F(1), displacement=0, n1=100000, n2=100000, dlo=0, dhi=200000,
    )
    task = DiffTask.make("ROV", "BASE", 1, 2, F(1), F(1),
                         win_lo=1, win_hi=2, dlo=0, dhi=200000)
    res = solve_scenario(scn, task)
    assert res.solution_count == 200001
    assert res.truncated is True
    assert len(res.solutions) == OUTPUT_CAP
    # listed solutions sorted by (n1,n2)
    ks = [(s.n1, s.n2) for s in res.solutions]
    assert ks == sorted(ks)
    # extremes span full bound even though only 1000 are listed
    assert res.per_epoch_extremes[1] == (F(0), F(200000))


# --------------------------------------------------------------------------- #
# 9. range query independence & cut reasons/source events
# --------------------------------------------------------------------------- #
def test_range_query_and_sources():
    scn = build_dual_wavelength(epochs=8, displacement=F(1, 10), dlo=0, dhi=F(1, 2))
    store = scn.store
    ev = store.add_jump("ROV", 1, 5, 2)
    svc = ArcService(store)
    prod = svc.build("ROV", 1, gap=1, persist=True)
    q = svc.query("ROV", 1, 4, 6)
    assert q.arc_ids == [prod.arc_id_at(4), prod.arc_id_at(5)]
    seg = [s for s in q.segments if s.start_epoch == 5][0]
    assert seg.start_reason == "JUMP"
    assert seg.source_event == ev
    # A declared jump is subtracted from raw phase from that epoch onward.
    assert prod.state(5).cumulative_jump == 2
    assert prod.state(5).corrected_phase == prod.state(5).raw_phase - 2
    assert prod.state(4).cumulative_jump == 0


# --------------------------------------------------------------------------- #
# 11. varying displacement per epoch is allowed (no smoothing)
# --------------------------------------------------------------------------- #
def test_varying_displacement_not_smoothed():
    dvals = [Fraction(0), Fraction(1, 10), Fraction(1, 5), Fraction(3, 20),
             Fraction(0), Fraction(1, 10)]
    scn = build_dual_wavelength(
        epochs=6, lam1=F(1, 5), lam2=F(1, 7), displacement=dvals,
        n1=2, n2=1, dlo=0, dhi=F(1, 2),
    )
    task = DiffTask.make("ROV", "BASE", 1, 2, F(1, 5), F(1, 7),
                         win_lo=1, win_hi=6, dlo=0, dhi=F(1, 2))
    res = solve_scenario(scn, task)
    assert res.status == "UNIQUE"
    got = res.solutions[0].displacement_dict()
    assert [got[e] for e in range(1, 7)] == dvals
    # per-epoch extremes equal the single solution's displacement
    for e in range(1, 7):
        assert res.per_epoch_extremes[e] == (got[e], got[e])


# --------------------------------------------------------------------------- #
# 12. cross-channel inconsistency -> INCONSISTENT
# --------------------------------------------------------------------------- #
def test_cross_channel_inconsistency():
    scn = build_dual_wavelength(epochs=4, displacement=F(1, 10), n1=2, n2=1,
                                dlo=0, dhi=F(1, 2))
    # Perturb channel 2 at one epoch by exactly one wavelength-worth of z (1/7).
    # Each channel stays independently feasible, but A*z1 - B*z2 now varies, so
    # no single (n1,n2) with one shared displacement can reconcile the channels.
    cur, lock = scn.store.get_observation("ROV", 2, 3)
    scn.store.put_observation("ROV", 2, 3, cur + F(1, 7), lock)
    task = DiffTask.make("ROV", "BASE", 1, 2, F(1, 5), F(1, 7),
                         win_lo=1, win_hi=4, dlo=0, dhi=F(1, 2))
    res = solve_scenario(scn, task)
    assert res.status == "INCONSISTENT"
    assert "cross-channel" in res.reason
    assert res.solution_count == 0


def test_no_feasible_pair_is_inconsistent():
    # Truth d = 1/10 but the bound is [1/5, 1/2]; no integer pair fits.
    scn = build_dual_wavelength(epochs=4, displacement=F(1, 10), n1=2, n2=1,
                                dlo=F(1, 5), dhi=F(1, 2))
    task = DiffTask.make("ROV", "BASE", 1, 2, F(1, 5), F(1, 7),
                         win_lo=1, win_hi=4, dlo=F(1, 5), dhi=F(1, 2))
    res = solve_scenario(scn, task)
    assert res.status == "INCONSISTENT"
    assert res.solution_count == 0


# --------------------------------------------------------------------------- #
# 13. external products: warehouse consumes supplied products
# --------------------------------------------------------------------------- #
def test_external_products_consumed():
    scn = build_dual_wavelength(epochs=4, displacement=F(1, 10), n1=2, n2=1,
                                dlo=0, dhi=F(1, 2))
    svc = ArcService(scn.store)
    # Build products in memory (not persisted) and hand them over as external.
    external = {
        (st, ch): svc.build(st, ch, gap=1, persist=False)
        for st in scn.stations for ch in scn.channels
    }
    # Mark them external so provenance records the distinction.
    external = {
        k: type(p)(p.station, p.channel, p.gap, p.event_level, p.obs_stamp,
                   p.epochs, p.segments, jump_stamp=p.jump_stamp,
                   product_id=None, external=True)
        for k, p in external.items()
    }
    task = DiffTask.make("ROV", "BASE", 1, 2, F(1, 5), F(1, 7),
                         win_lo=1, win_hi=4, dlo=0, dhi=F(1, 2))
    wh = DifferentialWarehouse(store=None)  # no DB needed
    res = wh.solve(task, external=external)
    assert res.status == "UNIQUE"
    assert (res.solutions[0].n1, res.solutions[0].n2) == (2, 1)
    for prov in res.provenance["consumed"].values():
        assert prov["external"] is True


# --------------------------------------------------------------------------- #
# 14. store persistence of a differential result keeps provenance
# --------------------------------------------------------------------------- #
def test_persisted_result_records_provenance():
    scn = build_dual_wavelength(epochs=4, displacement=F(1, 10), n1=2, n2=1,
                                dlo=0, dhi=F(1, 2))
    svc = ArcService(scn.store)
    prods = {
        (st, ch): svc.build(st, ch, gap=1, persist=True)
        for st in scn.stations for ch in scn.channels
    }
    task = DiffTask.make("ROV", "BASE", 1, 2, F(1, 5), F(1, 7),
                         win_lo=1, win_hi=4, dlo=0, dhi=F(1, 2))
    wh = DifferentialWarehouse(scn.store)
    res = wh.solve(task, products=prods, persist=True)
    assert res.result_id is not None
    rows = scn.store.task_results(1)
    assert rows[-1]["status"] == "UNIQUE"
    assert '"external": false' in rows[-1]["provenance"]


# --------------------------------------------------------------------------- #
# 10. persistence round-trip
# --------------------------------------------------------------------------- #
def test_store_roundtrip_independent_range_query(tmp_path):
    db = str(tmp_path / "t.db")
    with Store(db) as store:
        scn_store = store
        scn_store.put_observations([
            ("A", 1, 1, F(1, 3), True),
            ("A", 1, 2, F(2, 3), True),
        ])
        svc = ArcService(scn_store)
        p = svc.build("A", 1, gap=1)
        pid = p.product_id
    with Store(db) as store2:
        svc2 = ArcService(store2)
        loaded = svc2.load("A", 1, product_id=pid)
        assert loaded.corrected_at(1) == F(1, 3)
        assert loaded.arc_id_at(1) == loaded.arc_id_at(2)
        q = svc2.query("A", 1, 1, 2, product_id=pid)
        assert q.is_single_arc
