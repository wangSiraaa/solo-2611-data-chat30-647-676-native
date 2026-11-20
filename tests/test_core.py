"""End-to-end tests for the calibration service and counting repository.

Runs standalone (``python tests/test_core.py``) or under pytest.  All exact
geometry is asserted on Fractions; the float decay layer uses 1e-8 tolerance.
"""

import math
import os
import sys
import tempfile
from fractions import Fraction as F

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spectrum_svc.calibration import CalibrationError, CalibrationProduct
from spectrum_svc.catalog import CalibrationCatalog
from spectrum_svc.decay import (decay_integral, expected_counts,
                                invert_activity, to_reference_time)
from spectrum_svc.limits import COMPLETE, INCOMPLETE
from spectrum_svc.rationals import as_rational
from spectrum_svc.repository import CountRepository, RepositoryError

TOL = 1e-8


def close(a, b):
    return abs(a - b) <= max(TOL, TOL * max(abs(a), abs(b)))


# ----------------------------------------------------------------------
# 1. Hand-computed constant-scale calibration: exact coverage and the
#    forward/backward expected-count round trip.
# ----------------------------------------------------------------------
def test_hand_computed_constant_scale(tmpdir):
    db_cat = os.path.join(tmpdir, "cat.db")
    db_repo = os.path.join(tmpdir, "repo.db")
    with CalibrationCatalog(db_cat) as cat:
        # Constant slope E = C + 10 on [0, 20], points include an extra
        # interior point so piecewise machinery is exercised.
        cat.replace_points("det", [(0, 10), (10, 20), (20, 30)])
        product = cat.publish("det")
        product_json = cat.export_product(product.product_id)

    with CountRepository(db_repo) as repo:
        pid = repo.import_product(product_json)
        counts = [100] * 21  # bins 0..20, uniform counts
        # lambda = 0, L = T, eff = 1  => N = A*T with T = 21; pick A = 100.
        repo.import_spectrum("s1", "det", pid, start_sec=1000,
                             T=21, L=21, counts=counts)

        # Window E in [15, 16)  <=>  channel [5, 6): exactly one bin.
        r = repo.quantify_window("s1", 15, 16, eff=1, lam=0.0, t_ref=1000)
        assert r["status"] == COMPLETE
        assert r["back_projection"]["channel_lo"] == "5/1"
        assert r["back_projection"]["channel_hi"] == "6/1"
        assert r["back_projection"]["per_bin"] == [
            {"bin": 5, "coverage": "1/1", "raw_count": 100}]
        assert r["expected_counts_N"] == "100/1"
        # Hand calculation: A = N / (eff * L/T * T) = 100 / 21.
        assert close(r["decay"]["A_start"], 100.0 / 21.0)
        assert close(r["decay"]["A_ref"], 100.0 / 21.0)

        # Narrow window crossing bin boundaries: E [15.5, 17.25)
        # <=> channel [5.5, 7.25): bins 5 (0.5), 6 (1), 7 (0.25).
        r2 = repo.quantify_window("s1", F(31, 2), F(69, 4),
                                  eff=1, lam=0.0, t_ref=1000)
        cov = {item["bin"]: as_rational(item["coverage"])
               for item in r2["back_projection"]["per_bin"]}
        assert cov == {5: F(1, 2), 6: F(1), 7: F(1, 4)}
        assert as_rational(r2["expected_counts_N"]) == F(175)  # 100*1.75
        assert close(r2["decay"]["A_ref"], 175.0 / 21.0)
        # Forward model reproduces the generated counts.
        N_back = expected_counts(100.0, F(1), F(1), F(21), 0.0)
        assert close(N_back, 2100.0)  # total spectrum counts
        assert sum(counts) == 2100


# ----------------------------------------------------------------------
# 2. Narrow window crossing a calibration kink (slope change).
# ----------------------------------------------------------------------
def test_window_crossing_kink(tmpdir):
    product = CalibrationProduct("k", 1, [(0, 0), (10, 10), (20, 30)])
    # Slopes: 1 then 2. Window E [9, 12): segment1 part [9,10) -> c [9,10),
    # segment2 part [10,12) -> c [10, 11). Total channel length 2.
    g = product.backproject_window(9, 12, n_bins=21)
    assert g["status"] == COMPLETE
    assert (g["channel_lo"], g["channel_hi"]) == (F(9), F(11))
    cov = {j: length for j, length in g["coverage"]}
    assert cov == {9: F(1), 10: F(1)}
    # A window straddling the kink inside one bin: E [9.5, 10.5)
    # -> c [9.5, 10.25).
    g2 = product.backproject_window(F(19, 2), F(21, 2), n_bins=21)
    cov2 = {j: length for j, length in g2["coverage"]}
    assert cov2 == {9: F(1, 2), 10: F(1, 4)}
    assert g2["coverage_total"] == F(3, 4)


# ----------------------------------------------------------------------
# 3. Background measured on a different date; same t_ref subtraction.
# ----------------------------------------------------------------------
def test_background_different_dates(tmpdir):
    repo = CountRepository(os.path.join(tmpdir, "r.db"))
    pid = repo.import_product(
        CalibrationProduct("d", 1, [(0, 0), (10, 10)]).to_json_with_id())
    counts = [10] * 10
    # Sample at t=0, background one day later, both one-hour real/live time.
    repo.import_spectrum("sample", "d", pid, 0, T=3600, L=3600, counts=counts)
    repo.import_spectrum("bg", "d", pid, 86400, T=3600, L=1800,
                         counts=counts)
    lam = math.log(2.0) / 691200.0  # 8-day half-life, units 1/s
    net = repo.quantify_net("sample", "bg", 2, 4, eff=F(1, 10),
                            lam=lam, t_ref=0)
    assert net["status"] == COMPLETE
    s = repo.quantify_window("sample", 2, 4, F(1, 10), lam, 0)
    b = repo.quantify_window("bg", 2, 4, F(1, 10), lam, 0)
    # Independent hand calculation.
    N = F(20)  # two full bins * 10
    A_s = invert_activity(N, F(1, 10), F(1), F(3600), lam)
    A_b = invert_activity(N, F(1, 10), F(1, 2), F(3600), lam)
    A_b_ref = to_reference_time(A_b, lam, 86400, 0)
    assert close(s["decay"]["A_ref"], A_s)
    assert close(b["decay"]["A_ref"], A_b_ref)
    assert close(net["net"]["A_ref_net"], A_s - A_b_ref)
    repo.close()


# ----------------------------------------------------------------------
# 4. lambda = 0 takes the limit (no division by zero); reference time is
#    irrelevant then.
# ----------------------------------------------------------------------
def test_lambda_zero_limit(tmpdir):
    assert close(decay_integral(F(3600), 0.0), 3600.0)
    repo = CountRepository(os.path.join(tmpdir, "r0.db"))
    pid = repo.import_product(
        CalibrationProduct("d", 1, [(0, 0), (10, 10)]).to_json_with_id())
    repo.import_spectrum("z", "d", pid, 0, T=10, L=5, counts=[4] * 10)
    r = repo.quantify_window("z", 0, 10, eff=F(1, 2), lam=0.0, t_ref=99999)
    # N = 40; factor = 0.5 * (5/10) * 10 = 2.5  => A = 16.
    assert r["expected_counts_N"] == "40/1"
    assert close(r["decay"]["A_start"], 16.0)
    assert close(r["decay"]["A_ref"], 16.0)
    repo.close()


# ----------------------------------------------------------------------
# 5. Zero counts give zero (non-negative, exact) activity.
# ----------------------------------------------------------------------
def test_zero_counts(tmpdir):
    repo = CountRepository(os.path.join(tmpdir, "rz.db"))
    pid = repo.import_product(
        CalibrationProduct("d", 1, [(0, 0), (10, 10)]).to_json_with_id())
    repo.import_spectrum("empty", "d", pid, 0, T=10, L=10,
                         counts=[0] * 10)
    r = repo.quantify_window("empty", 1, 9, eff=1, lam=1e-3, t_ref=0)
    assert r["status"] == COMPLETE
    assert r["expected_counts_N"] == "0/1"
    assert r["decay"]["A_ref"] == 0.0
    repo.close()


# ----------------------------------------------------------------------
# 6. Calibration gaps: outside the energy domain OR covering unmeasured
#    bins => INCOMPLETE, unknown region never counted as zero.
# ----------------------------------------------------------------------
def test_calibration_gap(tmpdir):
    repo = CountRepository(os.path.join(tmpdir, "rg.db"))
    # Energy domain [10, 30]; spectrum measures only 8 bins.
    pid = repo.import_product(
        CalibrationProduct("d", 1, [(0, 10), (10, 30)]).to_json_with_id())
    repo.import_spectrum("g", "d", pid, 0, T=10, L=10, counts=[1] * 8)
    # Energy outside domain.
    r1 = repo.quantify_window("g", 5, 15, eff=1, lam=0.0, t_ref=0)
    assert r1["status"] == INCOMPLETE and r1["decay"]["A_ref"] is None
    # Inside domain but back-projects past bin 7 (E=26 -> c=8).
    r2 = repo.quantify_window("g", 20, 28, eff=1, lam=0.0, t_ref=0)
    assert r2["status"] == INCOMPLETE
    assert "unmeasured channel bins" in r2["back_projection"]["reason"]
    # A fully covered window is still complete.
    r3 = repo.quantify_window("g", 12, 14, eff=1, lam=0.0, t_ref=0)
    assert r3["status"] == COMPLETE
    repo.close()


# ----------------------------------------------------------------------
# 7. Re-calibration really changes fractions and intensities; old results
#    keep their identity.
# ----------------------------------------------------------------------
def test_recalibration_changes_fractions(tmpdir):
    cat = CalibrationCatalog(os.path.join(tmpdir, "c.db"))
    repo = CountRepository(os.path.join(tmpdir, "rr.db"))
    cat.replace_points("d", [(0, 0), (10, 10)])
    p1 = cat.publish("d")
    pid1 = repo.import_product(cat.export_product(p1.product_id))
    repo.import_spectrum("s", "d", pid1, 0, 10, 10, [10] * 10)
    r_old = repo.quantify_window("s", 2, 4, 1, 0.0, 0)
    old_id = r_old["result_id"]
    assert as_rational(r_old["expected_counts_N"]) == F(20)

    # New calibration E = 2C (steeper): same energy window shrinks to one bin.
    cat.replace_points("d", [(0, 0), (10, 20)])
    p2 = cat.publish("d")
    assert p2.product_id != p1.product_id
    pid2 = repo.import_product(cat.export_product(p2.product_id))
    repo.import_spectrum("s2", "d", pid2, 0, 10, 10, [10] * 10)
    r_new = repo.quantify_window("s2", 2, 4, 1, 0.0, 0)
    assert r_new["product_id"] == pid2
    assert (r_new["back_projection"]["channel_lo"],
            r_new["back_projection"]["channel_hi"]) == ("1/1", "2/1")
    assert as_rational(r_new["expected_counts_N"]) == F(10)
    assert close(r_new["decay"]["A_ref"], 1.0)
    # Old cached result untouched and still verifiable.
    check = repo.verify_result(old_id)
    assert check["ok"]
    assert repo.verify_result(r_new["result_id"])["ok"]
    cat.close()
    repo.close()


# ----------------------------------------------------------------------
# 8. Negative net value is kept, not clipped.
# ----------------------------------------------------------------------
def test_negative_net_kept(tmpdir):
    repo = CountRepository(os.path.join(tmpdir, "rn.db"))
    pid = repo.import_product(
        CalibrationProduct("d", 1, [(0, 0), (10, 10)]).to_json_with_id())
    repo.import_spectrum("lo", "d", pid, 0, 10, 10, [1] * 10)
    repo.import_spectrum("hi", "d", pid, 0, 10, 10, [9] * 10)
    net = repo.quantify_net("lo", "hi", 0, 10, 1, 0.0, 0)
    assert net["net"]["A_ref_net"] < 0
    assert close(net["net"]["A_ref_net"], (10 - 90) / 10.0)
    repo.close()


# ----------------------------------------------------------------------
# 9. Catalog rules: strict monotonicity, no extrapolation, atomic failure.
# ----------------------------------------------------------------------
def test_catalog_rules(tmpdir):
    cat = CalibrationCatalog(os.path.join(tmpdir, "c2.db"))
    cat.replace_points("d", [(0, 0), (5, 5), (10, 20)])
    try:
        # (6,18) after (5,5) is fine, but (4,4) after (6,18) breaks both
        # channel and energy strict monotonicity.
        cat.replace_points("d", [(0, 0), (5, 5), (6, 18), (4, 4)])
        raise AssertionError("non-monotonic points were accepted")
    except CalibrationError:
        pass
    # Failed update leaves the previous version in place.
    assert len(cat.get_points("d")) == 3
    product = cat.publish("d")
    try:
        product.energy_to_channel(25)
        assert False
    except CalibrationError:
        pass
    try:
        product.channel_to_energy(-1)
        assert False
    except CalibrationError:
        pass
    # External product tampering is rejected.
    import json
    payload = json.loads(json.dumps(
        {"product_id": product.product_id, **product.to_dict()}))
    payload["points"][0][1] = "1/1"
    try:
        CalibrationProduct.from_dict(payload)
        assert False
    except CalibrationError:
        pass
    cat.close()


# ----------------------------------------------------------------------
# 10. Stable small-lambda integral matches the series expansion.
# ----------------------------------------------------------------------
def test_stable_integral(tmpdir=None):
    T = F(100)
    for lam in (1e-12, 1e-6, 1e-3, 0.1, 1.0):
        got = decay_integral(T, lam)
        x = lam * 100.0
        if x < 1e-3:
            series = 100.0 * (1 - x / 2 + x * x / 6)
            assert close(got, series)
        else:
            assert got > 0
    assert close(decay_integral(T, 0.0), 100.0)


# ----------------------------------------------------------------------
# 11. Enforced limits: 4096 channels, 50 spectra, 8 points, 10 windows.
# ----------------------------------------------------------------------
def test_enforced_limits(tmpdir):
    from spectrum_svc.limits import (MAX_CALIBRATION_POINTS, MAX_SPECTRA,
                                     MAX_WINDOWS)
    cat = CalibrationCatalog(os.path.join(tmpdir, "lc.db"))
    repo = CountRepository(os.path.join(tmpdir, "lr.db"))
    too_many = [(i, 2 * i) for i in range(MAX_CALIBRATION_POINTS + 1)]
    try:
        cat.replace_points("d", too_many)
        raise AssertionError("point limit not enforced")
    except CalibrationError:
        pass
    cat.replace_points("d", [(0, 0), (10, 10)])
    pid = repo.import_product(cat.export_product(cat.publish("d").product_id))
    try:
        repo.import_spectrum("wide", "d", pid, 0, 10, 10, [0] * 4097)
        raise AssertionError("channel limit not enforced")
    except RepositoryError:
        pass
    repo.import_spectrum("ok", "d", pid, 0, 10, 10, [0] * 4096)
    for i in range(MAX_SPECTRA - 1):
        repo.import_spectrum(f"s{i}", "d", pid, 0, 10, 10, [0] * 4)
    try:
        repo.import_spectrum("overflow", "d", pid, 0, 10, 10, [0] * 4)
        raise AssertionError("spectrum limit not enforced")
    except RepositoryError:
        pass
    try:
        repo.quantify_windows("ok", [(i, i + 1) for i in range(MAX_WINDOWS + 1)],
                              1, 0.0, 0)
        raise AssertionError("window limit not enforced")
    except RepositoryError:
        pass
    cat.close()
    repo.close()


# ----------------------------------------------------------------------
# 12. External product tampering is rejected on import; every stored
#     result step remains available for replay.
# ----------------------------------------------------------------------
def test_tampered_product_and_audit_trail(tmpdir):
    import json
    repo = CountRepository(os.path.join(tmpdir, "ta.db"))
    product = CalibrationProduct("d", 1, [(0, 0), (5, 5), (10, 20)])
    good = json.loads(product.to_json_with_id())
    tampered = dict(good)
    tampered["points"] = [["0/1", "1/1"], *good["points"][1:]]
    try:
        repo.import_product(json.dumps(tampered))
        raise AssertionError("tampered product accepted")
    except RepositoryError:
        pass
    pid = repo.import_product(product.to_json_with_id())
    repo.import_spectrum("a", "d", pid, 7, F(9, 2), F(8, 3),
                         [3, 0, 5, 7, 2, 1, 4, 0, 9, 6])
    r = repo.quantify_window("a", F(3, 2), F(7, 2), F(2, 3), 1e-4, 100)
    # Audit trail: raw counts, coverage, timing correction, date conversion.
    bins = {x["bin"]: x for x in r["back_projection"]["per_bin"]}
    assert bins[2]["raw_count"] == 5 and bins[3]["raw_count"] == 7
    assert r["timing"] == {
        "start_sec": 7, "t_ref": 100, "T": "9/2", "L": "8/3",
        "live_fraction_L_over_T": "16/27"}
    assert r["decay"]["A_start"] is not None
    assert r["decay"]["A_ref"] is not None
    assert repo.verify_result(r["result_id"])["ok"]
    repo.close()


# ----------------------------------------------------------------------
# 13. Non-overlapping window rule; adjacent (touching) windows are fine.
# ----------------------------------------------------------------------
def test_window_overlap_rule(tmpdir):
    repo = CountRepository(os.path.join(tmpdir, "wo.db"))
    pid = repo.import_product(
        CalibrationProduct("d", 1, [(0, 0), (10, 10)]).to_json_with_id())
    repo.import_spectrum("w", "d", pid, 0, 10, 10, [1] * 10)
    try:
        repo.quantify_windows("w", [(1, 3), (2, 4)], 1, 0.0, 0)
        raise AssertionError("overlapping windows accepted")
    except RepositoryError:
        pass
    results = repo.quantify_windows("w", [(1, 3), (3, 5)], 1, 0.0, 0)
    assert [r["status"] for r in results] == [COMPLETE, COMPLETE]
    repo.close()


def _run_all():
    import shutil
    failures = 0
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    for name, fn in tests:
        tmp = tempfile.mkdtemp(prefix="spec_")
        try:
            fn(tmp)
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)