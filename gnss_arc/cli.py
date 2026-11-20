"""Command-line entry points: demo, solve, and an independent verifier.

Run as ``python -m gnss_arc.cli <command> [options]``.

Commands
--------
``demo``    Build synthetic scenarios, print arc products and differential answers.
``verify``  Re-derive every small differential answer by brute-force integer
            enumeration and compare against the exact lattice solver.  Prints
            PASS/FAIL and exits non-zero on any mismatch.

Everything uses exact rationals; no network, ephemeris or real GNSS protocol.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from fractions import Fraction

from . import (
    ArcService,
    DiffTask,
    DifferentialWarehouse,
    Store,
    solve_differential,
    Q,
    OUTPUT_CAP,
)
from .synthetic import build_same_wavelength, build_dual_wavelength

F = Fraction


def _products(scn, gap=1):
    svc = ArcService(scn.store)
    return {
        (st, ch): svc.build(st, ch, gap=gap, persist=True)
        for st in scn.stations
        for ch in scn.channels
    }


def cmd_demo(_args) -> int:
    print("=" * 70)
    print("SAME-WAVELENGTH  (expect AMBIGUOUS, many integer pairs)")
    print("=" * 70)
    scn = build_same_wavelength(
        epochs=10, lam=Fraction(1, 100), displacement=0, n1=3, n2=3,
        dlo=0, dhi=Fraction(1, 10),
    )
    task = DiffTask.make("ROV", "BASE", 1, 2, Fraction(1, 100), Fraction(1, 100),
                         win_lo=1, win_hi=10, dlo=0, dhi=Fraction(1, 10))
    res = solve_differential(task, _products(scn))
    print(f"status={res.status} count={res.solution_count} "
          f"n1_range={res.n1_range} n2_range={res.n2_range}")
    for s in res.solutions[:8]:
        print(f"  n1={s.n1:>3} n2={s.n2:>3} d(1)={s.displacement_dict()[1]}")

    print()
    print("=" * 70)
    print("DUAL-WAVELENGTH  (expect UNIQUE)")
    print("=" * 70)
    scn = build_dual_wavelength(
        epochs=6, lam1=Fraction(1, 5), lam2=Fraction(1, 7),
        displacement=Fraction(1, 10), n1=2, n2=1, dlo=0, dhi=Fraction(1, 2),
    )
    task = DiffTask.make("ROV", "BASE", 1, 2, Fraction(1, 5), Fraction(1, 7),
                         win_lo=1, win_hi=6, dlo=0, dhi=Fraction(1, 2))
    res = solve_differential(task, _products(scn))
    print(f"status={res.status} count={res.solution_count}")
    s = res.solutions[0]
    print(f"  n1={s.n1} n2={s.n2} d={[str(d) for _e, d in s.displacements]}")

    print()
    print("=" * 70)
    print("LOCK LOSS  (expect INCOMPLETE)")
    print("=" * 70)
    scn = build_dual_wavelength(
        epochs=6, displacement=Fraction(1, 10), lock_drop=4,
        dlo=0, dhi=Fraction(1, 2),
    )
    task = DiffTask.make("ROV", "BASE", 1, 2, Fraction(1, 5), Fraction(1, 7),
                         win_lo=1, win_hi=6, dlo=0, dhi=Fraction(1, 2))
    res = solve_differential(task, _products(scn))
    print(f"status={res.status} reason={res.reason}")

    print()
    print("=" * 70)
    print("HUGH SOLUTION SET  (exact count + per-epoch extremes beyond cap)")
    print("=" * 70)
    scn = build_same_wavelength(
        epochs=2, lam=1, displacement=0, n1=100000, n2=100000, dlo=0, dhi=200000,
    )
    task = DiffTask.make("ROV", "BASE", 1, 2, 1, 1, win_lo=1, win_hi=2,
                         dlo=0, dhi=200000)
    res = solve_differential(task, _products(scn))
    print(f"status={res.status} count={res.solution_count} "
          f"listed={len(res.solutions)} truncated={res.truncated} "
          f"(cap={OUTPUT_CAP})")
    print(f"per-epoch d extremes: { {e: (str(a), str(b)) for e,(a,b) in res.per_epoch_extremes.items()} }")
    return 0


def _bruteforce(task, z1, z2, nlim):
    A, B = task.lambda1, task.lambda2
    out = []
    for n1 in range(-nlim, nlim + 1):
        for n2 in range(-nlim, nlim + 1):
            ok = True
            for e in task.epochs:
                d = A * (z1[e] - n1)
                if d != B * (z2[e] - n2) or not (task.dlo <= d <= task.dhi):
                    ok = False
                    break
            if ok:
                out.append((n1, n2))
    return sorted(out)


def cmd_verify(_args) -> int:
    """Independent brute-force check of the exact solver on small instances."""
    import random

    cases = [
        # (lam1, lam2, n1, n2, dval, dlo, dhi)
        (F(1, 5), F(1, 7), 2, 1, F(1, 10), 0, F(1, 2)),
        (F(1, 100), F(1, 100), 3, 3, 0, 0, F(1, 10)),
        (F(1), F(1), 5, 5, 0, 0, 1),
        (F(2, 3), F(4, 5), 1, 2, F(3, 10), -1, 1),
        (F(1, 2), F(1, 2), 4, 4, F(1, 4), 0, F(1, 2)),
        (F(3), F(5), 2, 1, F(7, 2), 0, F(10)),
    ]
    rng = random.Random(2024)
    for _ in range(120):
        cases.append((
            Fraction(rng.randint(1, 7), rng.randint(1, 6)),
            Fraction(rng.randint(1, 7), rng.randint(1, 6)),
            rng.randint(0, 6), rng.randint(0, 6),
            Fraction(rng.randint(0, 12), 6),
            Fraction(rng.randint(-6, 3), 6),
            Fraction(rng.randint(4, 18), 6),
        ))

    failures = 0
    for idx, (l1, l2, n1, n2, dval, dlo, dhi) in enumerate(cases):
        if dlo > dhi:
            dlo, dhi = dhi, dlo
        epochs = list(range(1, 4))
        z1 = {e: dval / l1 + n1 for e in epochs}
        z2 = {e: dval / l2 + n2 for e in epochs}
        task = DiffTask.make("R", "B", 1, 2, l1, l2, 1, 3, dlo, dhi)

        # build products directly from required z (rover z, base 0)
        from .arc import ArcProduct, ArcSegment, EpochState

        def prod(st, ch, phases):
            eps = {
                e: EpochState(e, p, True, 0, p, True, 1,
                              "FEED_START" if e == 1 else None, None)
                for e, p in phases.items()
            }
            return ArcProduct(st, ch, 1, 0, "verify", eps,
                              (ArcSegment(1, 1, 3, 3, "FEED_START", "END_OF_DATA", None),))

        prods = {
            ("R", 1): prod("R", 1, z1),
            ("B", 1): prod("B", 1, {e: Fraction(0) for e in epochs}),
            ("R", 2): prod("R", 2, z2),
            ("B", 2): prod("B", 2, {e: Fraction(0) for e in epochs}),
        }
        res = solve_differential(task, prods)
        bf = _bruteforce(task, z1, z2, nlim=40)
        if res.is_solved:
            got = sorted((s.n1, s.n2) for s in res.solutions)
            ok = got == bf and res.solution_count == len(bf)
        else:
            ok = res.status == "INCONSISTENT" and not bf
        if not ok:
            failures += 1
            print(f"FAIL case {idx}: solver={res.status} "
                  f"got={[(s.n1, s.n2) for s in res.solutions][:6]} bf={bf[:6]}")
    print(f"verified {len(cases)} cases, {failures} failures")
    return 0 if failures == 0 else 1


def cmd_solve(args) -> int:
    """Solve a differential task against a persisted database.

    Expects a pre-populated DB (see README / tests) and prints exact JSON.
    """
    with Store(args.db) as store:
        wh = DifferentialWarehouse(store)
        task = DiffTask.make(
            args.rover, args.base, args.channel1, args.channel2,
            Q(args.lambda1), Q(args.lambda2),
            args.win_lo, args.win_hi, Q(args.dlo), Q(args.dhi),
        )
        # Resolve against the latest persisted product for each series.  With
        # --rebuild, re-derive current products at the requested gap first.
        if getattr(args, "rebuild", False):
            svc = ArcService(store)
            for key in task.series():
                svc.build(key[0], key[1], gap=args.gap, persist=True)
        prods = wh.resolve_products(task)
        res = wh.solve(task, products=prods)
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gnss_arc")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="run built-in synthetic scenarios")
    d.set_defaults(func=cmd_demo)

    v = sub.add_parser("verify", help="brute-force cross-check the solver")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("solve", help="solve a task against a SQLite database")
    s.add_argument("--db", required=True)
    s.add_argument("--rover", required=True)
    s.add_argument("--base", required=True)
    s.add_argument("--channel1", type=int, default=1)
    s.add_argument("--channel2", type=int, default=2)
    s.add_argument("--lambda1", required=True)
    s.add_argument("--lambda2", required=True)
    s.add_argument("--win-lo", type=int, required=True)
    s.add_argument("--win-hi", type=int, required=True)
    s.add_argument("--dlo", required=True)
    s.add_argument("--dhi", required=True)
    s.add_argument("--gap", type=int, default=1,
                   help="arc gap G used when rebuilding/loading products")
    s.add_argument("--rebuild", action="store_true",
                   help="rebuild current arc products at --gap before solving")
    s.set_defaults(func=cmd_solve)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
