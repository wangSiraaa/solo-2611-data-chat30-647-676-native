"""Generate the worked examples required by the task and print a full audit.

Scenarios:
  1. narrow window crossing calibration kinks, constant-scale hand check
  2. background measured on a different date, common reference subtraction
  3. lambda = 0 exact limit
  4. zero-count spectrum
  5. calibration gap (energy-domain and unmeasured-bin incompleteness)
  6. re-calibration changing fractions while old results keep identity

Run:  python examples/run_examples.py
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from fractions import Fraction as F

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spectrum_svc.catalog import CalibrationCatalog
from spectrum_svc.decay import expected_counts, invert_activity, \
    to_reference_time
from spectrum_svc.repository import CountRepository

LINE = "=" * 72


def heading(title: str) -> None:
    print("\n" + LINE + f"\n{title}\n" + LINE)


def main() -> None:
    work = tempfile.mkdtemp(prefix="spectrum_examples_")
    cat_path = os.path.join(work, "catalog.db")
    repo_path = os.path.join(work, "repo.db")
    print(f"scratch databases: {work}")

    # ---- publish a kinked and a constant-scale calibration -----------
    with CalibrationCatalog(cat_path) as cat:
        cat.replace_points("kink", [(0, 0), (10, 10), (20, 30)])
        p_kink = cat.publish("kink")
        cat.replace_points("linear", [(0, 10), (10, 20), (20, 30)])
        p_lin = cat.publish("linear")
        kink_doc, lin_doc = (cat.export_product(p_kink.product_id),
                             cat.export_product(p_lin.product_id))

    repo = CountRepository(repo_path)
    pid_kink = repo.import_product(kink_doc)
    pid_lin = repo.import_product(lin_doc)

    # ---------------- 1. kink-crossing narrow window ------------------
    heading("1. Narrow window crossing a calibration kink")
    repo.import_spectrum("k1", "kink", pid_kink, 0, 21, 21, [100] * 21)
    g = p_kink.backproject_window(F(19, 2), F(21, 2), 21)
    print("window E [19/2, 21/2) back-projects to channel "
          f"[{g['channel_lo']}, {g['channel_hi']})")
    for j, length in g["coverage"]:
        print(f"  bin {j}: coverage {length}  (slope changes at c=10)")
    r = repo.quantify_window("k1", F(19, 2), F(21, 2), 1, 0.0, 0)
    print("exact allocated N =", r["expected_counts_N"],
          " A_ref =", r["decay"]["A_ref"])

    # ---------------- 1b. constant-scale hand calculation --------------
    heading("1b. Constant-scale hand calculation and forward round trip")
    repo.import_spectrum("lin1", "linear", pid_lin, 1000, 21, 21,
                         [100] * 21)
    r = repo.quantify_window("lin1", F(31, 2), F(69, 4), 1, 0.0, 1000)
    print("window E [31/2, 69/4) <=> channel [11/2, 29/4)")
    print("hand: bins 5,6,7 cover 1/2 + 1 + 1/4 = 7/4; "
          "N = 100 * 7/4 = 175")
    print("stored per-bin:",
          [(x["bin"], x["coverage"]) for x in r["back_projection"]["per_bin"]])
    print("stored N =", r["expected_counts_N"],
          " A = N/T =", r["decay"]["A_ref"], " hand =", 175 / 21)
    forward = expected_counts(100.0, F(1), F(1), F(21), 0.0)
    print("forward model with A=100 gives total counts", forward,
          "== raw sum 2100:", abs(forward - 2100) < 1e-12)

    # ---------------- 2. different-date background --------------------
    heading("2. Background on a different date -> common t_ref subtraction")
    repo.import_spectrum("sample", "kink", pid_kink, 0, 3600, 3600,
                         [10] * 10 + [0] * 11)
    repo.import_spectrum("bg", "kink", pid_kink, 86400, 3600, 1800,
                         [10] * 10 + [0] * 11)
    lam = math.log(2.0) / 691200.0
    net = repo.quantify_net("sample", "bg", 2, 4, F(1, 10), lam, 0)
    s = repo.quantify_window("sample", 2, 4, F(1, 10), lam, 0)
    b = repo.quantify_window("bg", 2, 4, F(1, 10), lam, 0)
    N = F(20)
    hand_s = invert_activity(N, F(1, 10), F(1), F(3600), lam)
    hand_b = to_reference_time(
        invert_activity(N, F(1, 10), F(1, 2), F(3600), lam), lam, 86400, 0)
    print(f"sample A_ref={s['decay']['A_ref']:.10g} hand={hand_s:.10g}")
    print(f"bg     A_ref={b['decay']['A_ref']:.10g} hand={hand_b:.10g} "
          "(own L/T=1/2 and own start date)")
    print(f"net (signed, not clipped) = {net['net']['A_ref_net']:.10g}")

    # ---------------- 3. lambda = 0 limit ------------------------------
    heading("3. lambda = 0 limit (no division by zero)")
    repo.import_spectrum("z", "kink", pid_kink, 500, 10, 5, [4] * 10 + [0] * 11)
    r = repo.quantify_window("z", 0, 10, F(1, 2), 0.0, 99999)
    print("N =", r["expected_counts_N"],
          " I =", r["decay"]["integral_I"], "(= T exactly)")
    print("A = N / (eff * L/T * T) = 40 / 2.5 =", r["decay"]["A_ref"],
          "; reference shift has no effect")

    # ---------------- 4. zero counts -----------------------------------
    heading("4. Zero-count spectrum")
    repo.import_spectrum("empty", "kink", pid_kink, 0, 10, 10, [0] * 21)
    r = repo.quantify_window("empty", 2, 8, 1, 1e-3, 0)
    print("status", r["status"], " N =", r["expected_counts_N"],
          " A_ref =", r["decay"]["A_ref"])

    # ---------------- 5. calibration gaps ------------------------------
    heading("5. Calibration gaps -> INCOMPLETE, unknown never treated as zero")
    repo.import_spectrum("gap", "linear", pid_lin, 0, 10, 10, [1] * 8)
    r1 = repo.quantify_window("gap", 5, 15, 1, 0.0, 0)
    r2 = repo.quantify_window("gap", 20, 28, 1, 0.0, 0)
    r3 = repo.quantify_window("gap", 12, 14, 1, 0.0, 0)
    print("outside energy domain:", r1["status"], "-", r1["back_projection"]["reason"])
    print("back-projects past last measured bin:", r2["status"], "-",
          r2["back_projection"]["reason"])
    print("fully covered window:", r3["status"], "N =", r3["expected_counts_N"])
    net_bad = repo.quantify_net("gap", "empty", 5, 15, 1, 0.0, 0)
    print("net result inherits incompleteness:", net_bad["status"])

    # ---------------- 6. re-calibration --------------------------------
    heading("6. Re-calibration changes fractions; old result keeps identity")
    old = repo.quantify_window("lin1", 15, 16, 1, 0.0, 1000)
    with CalibrationCatalog(cat_path) as cat:
        cat.replace_points("linear", [(0, 10), (10, 30), (20, 50)])
        p_v2 = cat.publish("linear")
        doc_v2 = cat.export_product(p_v2.product_id)
    pid_v2 = repo.import_product(doc_v2)
    repo.import_spectrum("lin2", "linear", pid_v2, 1000, 21, 21, [100] * 21)
    new = repo.quantify_window("lin2", 15, 16, 1, 0.0, 1000)
    print("old product", old["product_id"], "new product", new["product_id"])
    print("old channel projection", old["back_projection"]["channel_lo"],
          old["back_projection"]["channel_hi"], "N =", old["expected_counts_N"])
    print("new channel projection", new["back_projection"]["channel_lo"],
          new["back_projection"]["channel_hi"], "N =", new["expected_counts_N"])
    check_old = repo.verify_result(old["result_id"])
    print("old result still verifies under its own product:", check_old["ok"])

    # ---------------- cache check --------------------------------------
    heading("Cache and verification")
    target = repo.quantify_window("lin1", F(31, 2), F(69, 4), 1, 0.0, 1000)
    again = repo.quantify_window("lin1", F(31, 2), F(69, 4), 1, 0.0, 1000)
    print("repeat call returns identical result id:",
          again["result_id"] == target["result_id"])
    print("stored result recomputes exactly:",
          repo.verify_result(target["result_id"])["ok"])
    for item in repo.list_results()[:4]:
        print(" ", item)
    repo.close()
    print("\nAll example scenarios completed.")


if __name__ == "__main__":
    main()
