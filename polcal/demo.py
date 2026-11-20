"""Demonstration of the limited synthetic model.

Run:  .venv/bin/python -m polcal.demo

This script:

1. builds a 16-channel, 100-reference calibration set from HAND-COMPUTED
   (I,Q,U) and hand-generated readings;
2. publishes a product and independently back-substitutes every channel;
3. runs the required scenarios: orthogonal solvable design, all-angles-repeated
   rank deficiency, gain error, non-positive gain fit, zero intensity,
   legal boundary, model-inconsistent solution;
4. ingests 1000 exposures and inverts them;
5. truly retracts one reference, republishes, and shows the parameter and
   exposure-run state change while the old product/run stay bound.

All numbers are outputs of s=(I+Q*cos(2theta)+U*sin(2theta))/2, y=g*s+b, g>0.
They are NOT medical, astronomical, or real-instrument measurements.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

import numpy as np

from . import model
from .calibration import CalibrationStore
from .warehouse import ObservationWarehouse

ART = Path(__file__).resolve().parent.parent / "artifacts"


def out(*args):
    print(*args)


def hdr(title):
    out("\n" + "=" * 78)
    out(title)
    out("=" * 78)


def hand_s(I, Q, U, theta):
    """Reference hand computation, independent of polcal.model."""
    import math as _m
    return (I + Q * _m.cos(_m.radians(2 * theta))
            + U * _m.sin(_m.radians(2 * theta))) / 2.0


def hand_y(g, b, s):
    return g * s + b


def make_design(n=16, seed=7):
    rng = np.random.default_rng(seed)
    # distinct, well-spread angles in [0,180)
    angles = {}
    for i in range(n):
        angles[f"ch{i:02d}"] = float((i * 11.25) % 180.0)
    return angles


def make_truth(n, seed=11):
    rng = np.random.default_rng(seed)
    gains = {f"ch{i:02d}": float(rng.uniform(0.5, 2.5)) for i in range(n)}
    offs = {f"ch{i:02d}": float(rng.uniform(-0.5, 0.5)) for i in range(n)}
    return gains, offs


def make_refs(n_refs, seed=23):
    rng = np.random.default_rng(seed)
    refs = []
    for k in range(n_refs):
        I = float(rng.uniform(0.3, 2.0))
        # keep inside the legal polarization cone for references
        frac = float(rng.uniform(0.0, 0.95))
        ang = float(rng.uniform(0.0, 2 * math.pi))
        Q = I * frac * math.cos(ang)
        U = I * frac * math.sin(ang)
        refs.append((f"R{k:03d}", I, Q, U))
    return refs


def main():
    ART.mkdir(parents=True, exist_ok=True)
    dbdir = tempfile.mkdtemp(prefix="polcal_demo_")
    cal_db = os.path.join(dbdir, "calibration.sqlite")
    wh_db = os.path.join(dbdir, "warehouse.sqlite")
    for p in (cal_db, wh_db):
        if os.path.exists(p):
            os.unlink(p)

    cal = CalibrationStore(cal_db)
    cal.init_db()
    wh = ObservationWarehouse(wh_db)
    wh.init_db()

    out("MODEL:", model.MODEL_STATEMENT)
    out("DISCLAIMER:", model.DISCLAIMER)

    N = 16
    angles = make_design(N)
    gains, offs = make_truth(N)
    design_id = cal.register_channels(angles)

    # ------------------------------------------------------------------
    hdr("1. HAND-COMPUTED INDEPENDENT BACK-SUBSTITUTION (one reference, all 16)")
    I, Q, U = 1.0, 0.3, -0.4
    out(f"reference truth: I={I} Q={Q} U={U}")
    out("channel theta     hand s      hand y     recomputed s   |err|")
    max_err = 0.0
    for ch, th in angles.items():
        s_h = hand_s(I, Q, U, th)
        y_h = hand_y(gains[ch], offs[ch], s_h)
        s_recomputed = (y_h - offs[ch]) / gains[ch]
        err = abs(s_recomputed - s_h)
        max_err = max(max_err, err)
        out(f"{ch:6s} {th:7.3f}  {s_h:10.6f}  {y_h:10.6f}  {s_recomputed:12.6f}   {err:.2e}")
    out(f"max |err| over all 16 channels: {max_err:.2e}")

    # ------------------------------------------------------------------
    hdr("2. CALIBRATE 100 REFERENCES / 16 CHANNELS -> PRODUCT")
    refs = make_refs(100)
    for rid, I, Q, U in refs:
        y = {ch: hand_y(gains[ch], offs[ch], hand_s(I, Q, U, th))
             for ch, th in angles.items()}
        cal.record_measurement(rid, I, Q, U, y)

    prod1 = cal.publish(design_id)
    pl = prod1["payload"]
    out(f"product {prod1['product_id']} v{prod1['product_version']} "
        f"kind={prod1['kind']}")
    out("status summary:", pl["status_summary"])
    gerr = max(abs(pl["channels"][ch]["gain"] - gains[ch]) for ch in angles)
    berr = max(abs(pl["channels"][ch]["offset"] - offs[ch]) for ch in angles)
    out(f"max |recovered_gain - true_gain|   = {gerr:.3e}")
    out(f"max |recovered_offset - true_offset| = {berr:.3e}")
    out(f"max residual_ss over channels       = "
        f"{max(pl['channels'][ch]['residual_ss'] for ch in angles):.3e}")

    # Independent back-substitution using ONLY the published product parameters:
    # take the hand-computed raw reading and correct it with (y-b)/g from the
    # product, then compare to the hand s.
    prod_max_err = 0.0
    for ch, th in angles.items():
        s_h = hand_s(I, Q, U, th)
        y_h = hand_y(gains[ch], offs[ch], s_h)
        s_from_prod = ((y_h - pl["channels"][ch]["offset"])
                       / pl["channels"][ch]["gain"])
        prod_max_err = max(prod_max_err, abs(s_from_prod - s_h))
    out(f"max |s via product params - hand s| over 16 ch = {prod_max_err:.3e}")

    # ------------------------------------------------------------------
    hdr("3. REQUIRED SCENARIOS")

    # 3a orthogonal solvable design
    des_o = cal.register_channels({"o0": 0.0, "o45": 45.0, "o90": 90.0})
    for name, (ri, rq, ru) in (("or1", (1.0, 0.3, -0.4)),
                               ("or2", (1.2, -0.2, 0.5))):
        y = {c: hand_y(1.0, 0.0, hand_s(ri, rq, ru, th))
             for c, th in (("o0", 0.0), ("o45", 45.0), ("o90", 90.0))}
        cal.record_measurement(name, ri, rq, ru, y)
    prod_o = cal.publish(des_o)
    wh.add_exposure("E_ortho", [
        {"channel": "o0", "weight": 1.0,
         "y_raw": hand_y(1.0, 0.0, hand_s(0.8, 0.3, -0.2, 0.0))},
        {"channel": "o45", "weight": 1.0,
         "y_raw": hand_y(1.0, 0.0, hand_s(0.8, 0.3, -0.2, 45.0))},
        {"channel": "o90", "weight": 1.0,
         "y_raw": hand_y(1.0, 0.0, hand_s(0.8, 0.3, -0.2, 90.0))},
    ])
    r = wh.invert("E_ortho", prod_o["payload"])
    out(f"  [orthogonal] run_status={r['solution']['run_status']} "
        f"status={r['solution']['status']}")
    out(f"    I={r['solution']['I']:.6f} Q={r['solution']['Q']:.6f} "
        f"U={r['solution']['U']:.6f}  (truth 0.8, 0.3, -0.2)")
    out(f"    DOLP={r['solution']['polarization']['dolp']:.6f} "
        f"angle={r['solution']['polarization']['angle_deg']:.4f} deg")

    # 3b all angles repeated -> rank deficient
    des_rep = cal.register_channels(
        {f"rep{i}": 30.0 for i in range(4)})
    for name, (ri, rq, ru) in (("rep_r1", (1.0, 0.2, 0.1)),
                               ("rep_r2", (0.7, -0.1, 0.3))):
        y = {f"rep{i}": hand_y(1.0, 0.0, hand_s(ri, rq, ru, 30.0))
             for i in range(4)}
        cal.record_measurement(name, ri, rq, ru, y)
    prod_rep = cal.publish(des_rep)
    wh.add_exposure("E_rep", [
        {"channel": f"rep{i}", "weight": 1.0,
         "y_raw": hand_y(1.0, 0.0, hand_s(0.5, 0.1, 0.1, 30.0))}
        for i in range(4)])
    r = wh.invert("E_rep", prod_rep["payload"])
    out(f"  [rank-deficient] run_status={r['solution']['run_status']} "
        f"rank={r['solution']['rank_diagnostics']['rank']} "
        f"(I is {r['solution']['I']})")

    # 3c gain error
    bad = json.loads(json.dumps(prod1["payload"]))
    bad["channels"]["ch00"]["gain"] *= 3.0
    wh.add_exposure("E_gainerr", [
        {"channel": ch, "weight": 1.0,
         "y_raw": hand_y(gains[ch], offs[ch], hand_s(0.8, 0.2, -0.1, th))}
        for ch, th in angles.items()])
    r = wh.invert("E_gainerr", bad)
    sol = r["solution"]
    out(f"  [gain error on ch00] run_status={sol['run_status']} "
        f"status={sol['status']} ssr={sol['sum_squared_weighted_residual']:.4e}")
    out(f"    solved I={sol['I']:.4f} Q={sol['Q']:.4f} U={sol['U']:.4f} "
        f"(truth 0.8, 0.2, -0.1) -> residuals expose the wrong gain")

    # 3d non-positive fitted gain
    des_neg = cal.register_channels({"neg": 0.0, "neg45": 45.0, "neg90": 90.0})
    for name, (ri, rq, ru) in (("neg_r1", (1.0, 0.2, 0.1)),
                               ("neg_r2", (0.6, -0.3, 0.2))):
        y = {}
        for c, th in (("neg", 0.0), ("neg45", 45.0), ("neg90", 90.0)):
            y[c] = -2.0 * hand_s(ri, rq, ru, th) + 0.1  # negative synthetic gain
        cal.record_measurement(name, ri, rq, ru, y)
    prod_neg = cal.publish(des_neg)
    out("  [non-positive gain] per-channel status/reason and attempted gain:")
    for c in ("neg", "neg45", "neg90"):
        p = prod_neg["payload"]["channels"][c]
        out(f"    {c}: {p['status']} reason={p['reason']} "
            f"attempted_gain={p['attempted_gain']:.4f}")

    # 3e zero intensity, legal boundary, model inconsistency, angle undefined
    def single_case(eid, I, Q, U, note):
        wh.add_exposure(eid, [
            {"channel": ch, "weight": 1.0,
             "y_raw": hand_y(gains[ch], offs[ch], hand_s(I, Q, U, th))}
            for ch, th in angles.items()])
        r = wh.invert(eid, prod1["payload"])["solution"]
        pol = r["polarization"]
        out(f"  [{note}] status={r['status']} I={r['I']:.4f} Q={r['Q']:.4f} "
            f"U={r['U']:.4f}")
        out(f"      DOLP={pol['dolp']} (defined={pol['dolp_defined']}) "
            f"angle={pol['angle_deg']} (defined={pol['angle_defined']}) "
            f"reasons={pol['undefined_reasons']}")

    single_case("E_zero", 0.0, 0.0, 0.0, "zero intensity")
    Ib = 0.7
    single_case("E_boundary", Ib, Ib * math.cos(0.3), Ib * math.sin(0.3),
                "legal boundary")
    single_case("E_inconsistent", 0.5, 0.5, 0.5, "model inconsistent")
    single_case("E_unpol", 1.0, 0.0, 0.0, "unpolarized (Q=U=0)")

    # 3f explicit routing destinations for missing / uncalibratable channels
    wh.add_exposure("E_routing", [
        {"channel": ch, "weight": 1.0,
         "y_raw": hand_y(gains[ch], offs[ch], hand_s(0.9, 0.2, 0.1, angles[ch]))}
        for ch in list(angles)[:10]])  # only 10 of 16 present
    r = wh.invert("E_routing", prod1["payload"])["solution"]
    out(f"  [routing] 10 of 16 channels present -> run_status={r['run_status']}")
    out(f"      missing (calibrated, no reading): {r['qc']['missing']}")
    out(f"      not_in_product: {r['qc']['not_in_product']}")
    out(f"      insufficient_design: {r['qc']['insufficient_design']['used_channels']}"
        f" used; none are zero-filled")

    # uncalibratable routing: make exactly u_c uncalibratable while u_a/u_b
    # stay calibratable.  u_c is theta=90, so s=(I-Q)/2; keep I-Q constant
    # (here 0.8) across references while varying U.  Then u_a s=(I+Q)/2 and
    # u_b s=(I+U)/2 both vary, but u_c is constant -> uncalibratable.
    des_uncal = cal.register_channels({"u_a": 0.0, "u_b": 45.0, "u_c": 90.0})
    for name, (ri, rq, ru) in (("u_r1", (1.0, 0.2, 0.1)),
                               ("u_r2", (1.1, 0.3, 0.4)),
                               ("u_r3", (1.2, 0.4, 0.7))):
        y = {c: hand_y(1.0, 0.0, hand_s(ri, rq, ru, th))
             for c, th in (("u_a", 0.0), ("u_b", 45.0), ("u_c", 90.0))}
        cal.record_measurement(name, ri, rq, ru, y)
    prod_uncal = cal.publish(des_uncal)
    wh.add_exposure("E_uncal", [
        {"channel": c, "weight": 1.0,
         "y_raw": hand_y(1.0, 0.0, hand_s(0.5, 0.1, 0.1, th))}
        for c, th in (("u_a", 0.0), ("u_b", 45.0), ("u_c", 90.0))])
    r = wh.invert("E_uncal", prod_uncal["payload"])["solution"]
    st = {c: prod_uncal["payload"]["channels"][c]["status"]
          for c in ("u_a", "u_b", "u_c")}
    out(f"  [uncalibratable] product statuses={st}; "
        f"u_c reason={prod_uncal['payload']['channels']['u_c']['reason']}")
    out(f"      run_status={r['run_status']} qc.uncalibratable="
        f"{list(r['qc']['uncalibratable'])} (not zero-filled); only the 2 "
        f"calibrated channels (u_a,u_b) are candidates, below the configured "
        f"minimum of 3, so no solution is fabricated")

    # ------------------------------------------------------------------
    hdr("4. 1000 EXPOSURES INGEST + INVERT")
    rng = np.random.default_rng(99)
    n_exp = 1000
    truths = {}
    for k in range(n_exp):
        I = float(rng.uniform(0.2, 2.0))
        frac = float(rng.uniform(0.0, 0.99))
        ang = float(rng.uniform(0.0, 2 * math.pi))
        Q, U = I * frac * math.cos(ang), I * frac * math.sin(ang)
        truths[f"EXP{k:04d}"] = (I, Q, U)
        n_ch = int(rng.integers(12, N + 1))
        chosen = list(angles)[:n_ch]
        readings = []
        for ch in chosen:
            y = hand_y(gains[ch], offs[ch], hand_s(I, Q, U, angles[ch]))
            # independent tiny measurement perturbation (not model noise claim)
            y += float(rng.normal(0.0, 1e-6))
            readings.append({"channel": ch, "y_raw": y,
                             "weight": float(rng.uniform(0.5, 2.0))})
        wh.add_exposure(f"EXP{k:04d}", readings)

    max_dI = max_dQ = max_dU = 0.0
    n_consistent = n_inconsistent = n_rankdef = n_insuf = 0
    for k in range(n_exp):
        eid = f"EXP{k:04d}"
        r = wh.invert(eid, prod1["payload"])["solution"]
        st = r["run_status"]
        if st == "solved":
            if r["status"] == "consistent":
                n_consistent += 1
            else:
                n_inconsistent += 1
            I0, Q0, U0 = truths[eid]
            max_dI = max(max_dI, abs(r["I"] - I0))
            max_dQ = max(max_dQ, abs(r["Q"] - Q0))
            max_dU = max(max_dU, abs(r["U"] - U0))
        elif st == "rank_deficient":
            n_rankdef += 1
        else:
            n_insuf += 1
    out(f"exposures: {n_exp}")
    out(f"  consistent={n_consistent}  model_inconsistent={n_inconsistent}  "
        f"rank_deficient={n_rankdef}  insufficient_channels={n_insuf}")
    out(f"  max |I-recovered - I_truth| = {max_dI:.3e}")
    out(f"  max |Q-recovered - Q_truth| = {max_dQ:.3e}")
    out(f"  max |U-recovered - U_truth| = {max_dU:.3e}")

    # ------------------------------------------------------------------
    hdr("5. TRUE REFERENCE RETRACTION -> PARAMETER + EXPOSURE STATE CHANGE")
    # First inject one deliberately-inconsistent reference (stated (I,Q,U)=
    # (1,0,0) but readings +5%).  This biases the fit, so retracting it gives a
    # visible parameter change.  (Sections 3-4 used the clean product.)
    # Replace R042's original clean measurement with the bad one.
    for m in cal.list_measurements():
        if m["reference_id"] == "R042" and m["status"] == "active":
            cal.retract_measurement(m["measurement_id"],
                                    "replaced by corrupted-reference test")
    cal.register_reference("R042", 1.0, 0.0, 0.0, correction_of="R042")
    bad_y = {ch: hand_y(gains[ch], offs[ch], hand_s(1.0, 0.0, 0.0, th)) * 1.05
             for ch, th in angles.items()}
    cal.record_measurement("R042", 1.0, 0.0, 0.0, bad_y)
    before_product = cal.publish(design_id)  # now biased by R042
    target_ref = "R042"
    target_exp = "EXP0000"
    run_before = wh.invert(target_exp, before_product["payload"])
    ref_before = [x for x in cal.list_references()
                  if x["reference_id"] == target_ref][0]
    ch_probe = "ch00"
    before_params = before_product["payload"]["channels"][ch_probe]
    out(f"BEFORE: reference {target_ref} version={ref_before['reference_version']}, "
        f"{ch_probe} n_points={before_params['n_points']}, "
        f"gain={before_params['gain']:.6f}, offset={before_params['offset']:.6f}")
    out(f"        exposure {target_exp} run {run_before['run_id']} "
        f"product {run_before['product_id']} v{run_before['product_version']}")
    out(f"        run I={run_before['solution']['I']:.6f} "
        f"Q={run_before['solution']['Q']:.6f} "
        f"U={run_before['solution']['U']:.6f}")

    retract = cal.retract_reference(target_ref, "reference withdrawn by operator")
    out(f"RETRACT: {retract}")

    prod_after = cal.publish(design_id)
    after_params = prod_after["payload"]["channels"][ch_probe]
    out(f"AFTER:  new product {prod_after['product_id']} "
        f"v{prod_after['product_version']}; {ch_probe} "
        f"n_points={after_params['n_points']}, gain={after_params['gain']:.6f}, "
        f"offset={after_params['offset']:.6f}")
    out(f"        gain changed by {after_params['gain'] - before_params['gain']:.3e}, "
        f"offset changed by {after_params['offset'] - before_params['offset']:.3e}")

    run_after = wh.rerun(run_before["run_id"], prod_after["payload"])
    out(f"RERUN:  exposure {target_exp} new run {run_after['run_id']} "
        f"product {run_after['product_id']} v{run_after['product_version']} "
        f"(reof_of={run_after['reof_of']})")
    out(f"        new run I={run_after['solution']['I']:.6f} "
        f"Q={run_after['solution']['Q']:.6f} "
        f"U={run_after['solution']['U']:.6f}")
    old = wh.get_run(run_before["run_id"])
    out(f"        OLD run still bound to product {old['product_id']} "
        f"v{old['product_version']} with I={old['solution']['I']:.6f} "
        f"(unchanged={abs(old['solution']['I'] - run_before['solution']['I']) < 1e-15})")
    old_product = cal.get_product(before_product["product_id"])
    out(f"        OLD product v{old_product['product_version']} still has "
        f"{ch_probe} n_points={old_product['payload']['channels'][ch_probe]['n_points']}")

    # ------------------------------------------------------------------
    hdr("6. TRACE")
    tr = cal.trace_reference(target_ref)
    inc = [x for x in tr["product_versions"] if x["included"]]
    out(f"reference {target_ref}: {len(tr['measurements'])} measurement(s), "
        f"events={[e['action'] for e in tr['events']]}")
    out(f"  included in {len(inc)} product(s); last by version: "
        f"{inc[-1]['product_version'] if inc else None}; the post-retraction "
        f"product v{prod_after['product_version']} included="
        f"{[x['included'] for x in tr['product_versions'] if x['product_id']==prod_after['product_id']]}")
    trp = cal.trace_product(prod_after["product_id"])
    out(f"new product {prod_after['product_id']} trace: design={trp['design']['design_id']}, "
        f"fit_points for {ch_probe} = {len(trp['fit_points'].get(ch_probe, []))}")
    trx = wh.trace_exposure(target_exp)
    out(f"exposure {target_exp} has {len(trx['runs'])} run(s):")
    for x in trx["runs"]:
        out(f"    run={x['run_id']} product={x['product_id']} "
            f"v{x['product_version']} status={x['run_status']}")

    # ------------------------------------------------------------------
    hdr("7. PERSIST ARTEFACTS")
    summary = {
        "model_statement": model.MODEL_STATEMENT,
        "disclaimer": model.DISCLAIMER,
        "n_channels": N,
        "n_references": len(refs),
        "n_exposures": n_exp,
        "product_v1_clean": {
            "product_id": prod1["product_id"],
            "product_version": prod1["product_version"],
            "sha256": prod1["payload_sha256"],
            "status_summary": prod1["payload"]["status_summary"],
            "note": "100 clean references; used for sections 3-4.",
        },
        "product_biased_with_R042": {
            "product_id": before_product["product_id"],
            "product_version": before_product["product_version"],
            "sha256": before_product["payload_sha256"],
            "status_summary": before_product["payload"]["status_summary"],
            "note": "contains the deliberately inconsistent reference R042.",
        },
        "product_after_retraction": {
            "product_id": prod_after["product_id"],
            "product_version": prod_after["product_version"],
            "sha256": prod_after["payload_sha256"],
            "status_summary": prod_after["payload"]["status_summary"],
        },
        "inversion_counts": {
            "consistent": n_consistent,
            "model_inconsistent": n_inconsistent,
            "rank_deficient": n_rankdef,
            "insufficient_channels": n_insuf,
        },
        "recovery_max_abs_error": {"I": max_dI, "Q": max_dQ, "U": max_dU},
        "retraction": {
            "reference_id": target_ref,
            "old_product": before_product["product_id"],
            "new_product": prod_after["product_id"],
            "old_run": run_before["run_id"],
            "new_run": run_after["run_id"],
            "gain_before": before_params["gain"],
            "gain_after": after_params["gain"],
            "offset_before": before_params["offset"],
            "offset_after": after_params["offset"],
        },
        "config": model.DEFAULT_CONFIG,
    }
    (ART / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n")
    cal_export = cal.list_products()
    wh_export = {
        "exposures": len(wh.list_exposures()),
        "runs_for_target": wh.list_runs(target_exp),
    }
    (ART / "products.json").write_text(json.dumps(cal_export, indent=2) + "\n")
    (ART / "runs.json").write_text(json.dumps(wh_export, indent=2) + "\n")
    out(f"wrote {ART/'summary.json'}")
    out(f"wrote {ART/'products.json'}")
    out(f"wrote {ART/'runs.json'}")
    out(f"\n(scratch sqlite databases under {dbdir})")


if __name__ == "__main__":
    main()
