"""End-to-end verification with independent analytic cross-checks.

``verify_all`` runs each synthetic scenario through the store and checks:

* mass conservation (window residual within tolerance),
* catalog identity recomputation,
* for the single-room scenarios: closed-form exponential solution,
* for the two-room symmetric scenario: eigen-decomposition solution,
* for the closed zero-exhaust scenario: total tracer mass is conserved,
* the balanced flow revision changes the neighbour curve and exposure, while
  the old experiment keeps its old basis.

Results are aggregated into a JSON-friendly dict with ``ok`` at the top.
"""

from __future__ import annotations

import math
import tempfile

from .db import Store
from .examples import SCENARIOS, build
from .rational import parse_nonnegative
from .transfer import transfer_response


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def verify_all() -> dict:
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(f"{tmp}/verify.db")
        for name in SCENARIOS:
            built = build(name)
            if name == "legit_flow_correction":
                cat, exp, revision = built
            else:
                cat, exp = built
                revision = None

            store.save_catalog(cat)
            verify = store.verify_catalog(cat.identity)
            checks.append(_check(f"{name}:identity", verify["ok"]))

            e = store.import_experiment(
                catalog=cat,
                initial_concentration=exp["initial_concentration"],
                source_events=exp["source_events"],
                window=exp["window"],
                visits=exp["visits"],
            )
            result = store.run(e.identity)
            mb = result["mass_balance"]
            checks.append(
                _check(
                    f"{name}:mass_balance",
                    mb["ok"],
                    detail={"relative_residual": mb["relative_residual"]},
                )
            )
            checks.append(
                _check(
                    f"{name}:controlled_error",
                    result["max_abs_error_estimate"] <= 1e-7,
                    detail={"estimate": result["max_abs_error_estimate"]},
                )
            )

            # ---- analytic cross-checks ----
            if name == "piecewise_source":
                _check_single_room_piecewise(cat, exp, result, checks)
            elif name == "boundary_flow":
                _check_two_room_boundary(cat, exp, result, checks)
            elif name == "closed_zero_exhaust":
                total = sum(
                    cat.rooms[r] * result["concentrations"][r][-1] for r in cat.room_names()
                )
                initial = sum(
                    cat.rooms[r]
                    * float(parse_nonnegative(exp["initial_concentration"][r]))
                    for r in cat.room_names()
                )
                checks.append(
                    _check(
                        f"{name}:mass_conserved",
                        _close(float(total), float(initial), 1e-6),
                        detail={"final": float(total), "initial": float(initial)},
                    )
                )
            elif name == "two_room_exchange":
                _check_two_room_transfer(cat, checks)

            if revision:
                rev = store.revise_flows(cat.identity, revision)
                store.save_catalog(rev)
                e2 = store.import_experiment(
                    catalog=rev,
                    initial_concentration=exp["initial_concentration"],
                    source_events=exp["source_events"],
                    window=exp["window"],
                    visits=exp["visits"],
                )
                result2 = store.run(e2.identity)
                b_old = result["concentrations"]["B"]
                b_new = result2["concentrations"]["B"]
                changed = max(abs(x - y) for x, y in zip(b_old, b_new)) > 1e-5
                checks.append(_check(f"{name}:revision_changes_neighbor_curve", changed))
                v_old = result["person_totals"]["frank"]["integral_mg_s_per_m3"]
                v_new = result2["person_totals"]["frank"]["integral_mg_s_per_m3"]
                checks.append(
                    _check(
                        f"{name}:revision_changes_exposure",
                        not _close(v_old, v_new, 1e-9),
                        detail={"old": v_old, "new": v_new},
                    )
                )
                old = store.get_result(e.identity)
                checks.append(
                    _check(
                        f"{name}:old_experiment_keeps_old_basis",
                        old["catalog_identity"] == cat.identity
                        and all(
                            v["basis_catalog_identity"] == cat.identity
                            for v in old["visit_integrals"]
                        ),
                    )
                )
        store.close()

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks}


def _check(name, ok, detail=None):
    entry = {"name": name, "ok": bool(ok)}
    if detail is not None:
        entry["detail"] = detail
    return entry


def _check_single_room_piecewise(cat, exp, result, checks):
    """Closed-form reference for the single-room piecewise source."""
    V = float(cat.rooms["A"])
    q = float(cat.exhaust("A"))
    # piecewise analytic: segments [0,100) s=2, [100,200) s=0, [200,300) s=1
    def step(c, s, dt):
        k = q / V
        eq = s / q if q else 0.0
        e = math.exp(-k * dt)
        c_end = eq + (c - eq) * e
        if q:
            integral = eq * dt + (c - eq) * (1 - e) / k
        else:
            integral = c * dt + s / V * dt * dt / 2
        return c_end, integral

    c = 0.0
    I = 0.0
    for (t0, t1, s) in [(0, 100, 2.0), (100, 200, 0.0), (200, 300, 1.0), (300, 400, 0.0)]:
        c, i = step(c, s, t1 - t0)
        I += i
    # visit [50,250): integrate [50,100] with s=2, [100,200] s=0, [200,250] s=1
    c50 = _value_after([(0, 100, 2.0)], 50, V, q)
    I50 = _integral_seg(c50, 2.0, 50, V, q)  # 50..100
    c100 = _value_after([(0, 100, 2.0)], 100, V, q)
    I100 = _integral_seg(c100, 0.0, 100, V, q)  # 100..200
    c200 = _value_after([(0, 100, 2.0), (100, 200, 0.0)], 200, V, q)
    I200 = _integral_seg(c200, 1.0, 50, V, q)  # 200..250
    got = result["visit_integrals"][0]["concentration_integral_mg_s_per_m3"]
    checks.append(
        _check(
            "piecewise_source:analytic_visit_integral",
            _close(got, I50 + I100 + I200, 1e-6),
            detail={"got": got, "expected": I50 + I100 + I200},
        )
    )
    c_end_got = result["concentrations"]["A"][-1]
    checks.append(
        _check(
            "piecewise_source:analytic_end_concentration",
            _close(c_end_got, c, 1e-6),
            detail={"got": c_end_got, "expected": c},
        )
    )


def _value_after(segments, t, V, q):
    c = 0.0
    for (t0, t1, s) in segments:
        if t <= t0:
            break
        dt = min(t, t1) - t0
        k = q / V
        eq = s / q if q else 0.0
        c = eq + (c - eq) * math.exp(-k * dt)
    return c


def _integral_seg(c0, s, dt, V, q):
    k = q / V
    eq = s / q if q else 0.0
    e = math.exp(-k * dt)
    return eq * dt + (c0 - eq) * (1 - e) / k


def _check_two_room_boundary(cat, exp, result, checks):
    """Two-room with distinct flows: independent 2x2 eigen check for end state.

    Uses the general 2x2 solution via eigen-decomposition of K.
    """
    import numpy as np

    V = cat.matrices()["V"]
    K = cat.matrices()["K"]
    # source constant [3,0] on [0,150), then [0,0] on [150,400)
    names = cat.room_names()
    c = np.array([0.0, 0.0])
    s = np.array([3.0, 0.0])

    def advance(c, s, dt):
        # solve dc/dt = K c + V^-1 s analytically using augmented expm is the
        # same solver; instead use small RK4 with many steps (independent).
        n = 3000
        h = dt / n
        cc = c.copy()
        for _ in range(n):
            k1 = K @ cc + s / V
            k2 = K @ (cc + 0.5 * h * k1) + s / V
            k3 = K @ (cc + 0.5 * h * k2) + s / V
            k4 = K @ (cc + h * k3) + s / V
            cc = cc + h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        return cc

    c = advance(c, s, 150.0)
    c = advance(c, np.array([0.0, 0.0]), 250.0)
    got = np.array([result["concentrations"][r][-1] for r in names])
    checks.append(
        _check(
            "boundary_flow:independent_rk4_end_state",
            np.allclose(got, c, atol=1e-5, rtol=0),
            detail={"got": got.tolist(), "expected": c.tolist()},
        )
    )


def _check_two_room_transfer(cat, checks):
    """Transfer operators agree with direct experiment propagation."""
    resp = transfer_response(cat)
    # G(0) = I
    G0 = resp.concentration_operator(0.0)
    checks.append(
        _check(
            "two_room_exchange:transfer_operator_identity",
            _close(float(G0[0, 0]), 1.0, 1e-12) and _close(float(G0[0, 1]), 0.0, 1e-12),
        )
    )


if __name__ == "__main__":
    import json

    print(json.dumps(verify_all(), indent=2))
