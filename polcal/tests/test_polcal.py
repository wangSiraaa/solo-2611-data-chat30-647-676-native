"""End-to-end tests for the limited synthetic model only.

Every case uses hand-generated I/Q/U and hand-computed readings from the model
s=(I+Q*cos2θ+U*sin2θ)/2, y=g*s+b.  No real instrument or physical accuracy is
implied.
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest

from polcal import (
    CalibrationStore,
    ObservationWarehouse,
    ValidationError,
    apply_channel_correction,
    canonical_theta,
    ideal_s,
    model,
)

# Fixed orthogonal design: theta = 0, 45, 90 (mod 180). Rank 3 with rows
# [1, cos2θ, sin2θ].
ORTHO = {"c0": 0.0, "c45": 45.0, "c90": 90.0}
ORTHO2 = {"c0": 0.0, "c45": 45.0, "c90": 90.0, "c135": 135.0}

# hand-picked I/Q/U reference truths
REF = {"I": 1.0, "Q": 0.3, "U": -0.4}
REF2 = {"I": 1.2, "Q": -0.2, "U": 0.5}
REF3 = {"I": 0.8, "Q": 0.0, "U": 0.0}

# per-channel gains (true) used to synthesize readings
GAINS = {"c0": 2.0, "c45": 1.5, "c90": 0.8, "c135": 1.1}
OFFSETS = {"c0": 0.1, "c45": -0.2, "c90": 0.3, "c135": 0.0}


def synth_y(I, Q, U, channels, gains, offsets):
    return {ch: gains[ch] * ideal_s(I, Q, U, th) + offsets[ch]
            for ch, th in channels.items()}


def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.unlink(path)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.cal_path = tmp_db()
        self.wh_path = tmp_db()
        self.cal = CalibrationStore(self.cal_path)
        self.cal.init_db()
        self.wh = ObservationWarehouse(self.wh_path)
        self.wh.init_db()

    def tearDown(self):
        for p in (self.cal_path, self.wh_path):
            if os.path.exists(p):
                os.unlink(p)

    def measure(self, ref_id, iq, y, *, correct=False):
        return self.cal.record_measurement(
            ref_id, iq["I"], iq["Q"], iq["U"], y, correct=correct)

    def ingest_truth(self, name, iq, channels, gains=None, offsets=None,
                     bad_gains=None):
        gains = gains or GAINS
        offsets = offsets or OFFSETS
        y = synth_y(iq["I"], iq["Q"], iq["U"], channels,
                    bad_gains or gains, offsets)
        return self.measure(name, iq, y)


class TestCalibration(Base):
    def test_fit_recovers_known_gain_and_residual(self):
        design = self.cal.register_channels(ORTHO)
        for name, iq in (("r1", REF), ("r2", REF2), ("r3", REF3)):
            self.ingest_truth(name, iq, ORTHO)
        prod = self.cal.publish(design)
        for ch in ORTHO:
            c = prod["payload"]["channels"][ch]
            self.assertEqual(c["status"], "calibrated", ch)
            self.assertAlmostEqual(c["gain"], GAINS[ch], places=9)
            self.assertAlmostEqual(c["offset"], OFFSETS[ch], places=9)
            self.assertLess(c["residual_ss"], 1e-18)

    def test_insufficient_distinct_s_uncalibratable(self):
        design = self.cal.register_channels(ORTHO)
        # two references with identical (I,Q,U) -> identical s for every channel
        self.ingest_truth("r1", REF, ORTHO)
        self.ingest_truth("r1b", REF, ORTHO)
        prod = self.cal.publish(design)
        for ch in ORTHO:
            c = prod["payload"]["channels"][ch]
            self.assertEqual(c["status"], "uncalibratable")
            self.assertEqual(c["reason"], "insufficient_distinct_s")

    def test_single_measurement_uncalibratable(self):
        design = self.cal.register_channels(ORTHO)
        self.ingest_truth("r1", REF, ORTHO)
        prod = self.cal.publish(design)
        for ch in ORTHO:
            self.assertEqual(prod["payload"]["channels"][ch]["status"],
                             "uncalibratable")
            self.assertEqual(prod["payload"]["channels"][ch]["reason"],
                             "insufficient_measurements")

    def test_gain_nonpositive_is_rejected(self):
        design = self.cal.register_channels(ORTHO)
        bad_gains = {ch: -GAINS[ch] for ch in ORTHO}
        for name, iq in (("r1", REF), ("r2", REF2)):
            self.ingest_truth(name, iq, ORTHO, bad_gains=bad_gains)
        prod = self.cal.publish(design)
        for ch in ORTHO:
            c = prod["payload"]["channels"][ch]
            self.assertEqual(c["status"], "uncalibratable")
            self.assertEqual(c["reason"], "gain_nonpositive")
            self.assertLess(c["attempted_gain"], 0.0)
            # rejected fit parameters and residuals are retained
            self.assertIsNotNone(c["residual_ss"])

    def test_add_reference_creates_new_product_old_immutable(self):
        design = self.cal.register_channels(ORTHO)
        for name, iq in (("r1", REF), ("r2", REF2)):
            self.ingest_truth(name, iq, ORTHO)
        p1 = self.cal.publish(design)
        self.ingest_truth("r3", REF3, ORTHO)
        p2 = self.cal.publish(design)
        self.assertEqual(p2["product_version"], p1["product_version"] + 1)
        old = self.cal.get_product(p1["product_id"])
        self.assertEqual(old["payload_sha256"], p1["payload_sha256"])
        self.assertEqual(old["payload"]["channels"]["c0"]["n_points"], 2)
        self.assertEqual(p2["payload"]["channels"]["c0"]["n_points"], 3)

    def test_retract_reference_changes_next_product(self):
        design = self.cal.register_channels(ORTHO)
        for name, iq in (("r1", REF), ("r2", REF2), ("r3", REF3)):
            self.ingest_truth(name, iq, ORTHO)
        p1 = self.cal.publish(design)
        self.assertEqual(p1["payload"]["channels"]["c0"]["n_points"], 3)
        self.assertEqual(sorted(p1["payload"]["channels"]["c0"]["n_points"]
                                for _ in [0]), [3])
        mid = [m for m in self.cal.list_measurements() if m["reference_id"] == "r2"][0]
        self.cal.retract_measurement(mid["measurement_id"], "reference withdrawn")
        p2 = self.cal.publish(design)
        self.assertEqual(p2["product_version"], p1["product_version"] + 1)
        self.assertEqual(p2["payload"]["channels"]["c0"]["n_points"], 2)
        # old product still reports the withdrawn measurement was present
        old = self.cal.get_product(p1["product_id"])
        self.assertEqual(old["payload"]["channels"]["c0"]["n_points"], 3)

    def test_products_immutable_trigger(self):
        import sqlite3
        design = self.cal.register_channels(ORTHO)
        self.ingest_truth("r1", REF, ORTHO)
        self.ingest_truth("r2", REF2, ORTHO)
        p = self.cal.publish(design)
        with sqlite3.connect(self.cal_path) as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE products SET payload_json='{}' WHERE product_id=?",
                             (p["product_id"],))
        with sqlite3.connect(self.cal_path) as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM products WHERE product_id=?",
                             (p["product_id"],))

    def test_same_reference_id_multiple_measurements(self):
        self.measure("r1", REF, {"c0": 1.0})
        # same reference id measured again -> a new measurement id
        self.measure("r1", REF, {"c0": 1.1})
        rows = self.cal.list_measurements()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["measurement_id"], rows[1]["measurement_id"])
        self.assertTrue(all(r["reference_id"] == "r1" for r in rows))

    def test_correction_of_reference_bumps_version(self):
        self.measure("r1", REF, {"c0": 1.0})
        self.measure("r1", REF2, {"c0": 1.1}, correct=True)
        ref = [r for r in self.cal.list_references() if r["reference_id"] == "r1"][0]
        self.assertEqual(ref["reference_version"], 2)
        t = self.cal.trace_reference("r1")
        actions = [e["action"] for e in t["events"]]
        self.assertIn("correct", actions)

    def test_reference_identity_vs_remeasurement(self):
        design = self.cal.register_channels(ORTHO)
        self.ingest_truth("r1", REF, ORTHO)
        self.ingest_truth("r2", REF2, ORTHO)
        # re-measure r1: an independent measurement, not a re-send
        self.ingest_truth("r1", REF, ORTHO)
        meas = self.cal.list_measurements()
        self.assertEqual([m["reference_id"] for m in meas], ["r1", "r2", "r1"])
        prod = self.cal.publish(design)
        self.assertEqual(prod["payload"]["channels"]["c0"]["n_points"], 3)


class TestWarehouse(Base):
    def setUp(self):
        super().setUp()
        self.design = self.cal.register_channels(ORTHO2)
        for name, iq in (("r1", REF), ("r2", REF2), ("r3", REF3)):
            self.ingest_truth(name, iq, ORTHO2)
        self.prod = self.cal.publish(self.design)

    def make_exposure(self, eid, I, Q, U, channels=ORTHO2, chosen=None,
                      weights=None):
        chosen = channels if chosen is None else chosen
        readings = []
        for ch in chosen:
            y = GAINS[ch] * ideal_s(I, Q, U, channels[ch]) + OFFSETS[ch]
            w = 1.0 if weights is None else weights[ch]
            readings.append({"channel": ch, "y_raw": y, "weight": w})
        self.wh.add_exposure(eid, readings)

    def test_orthogonal_inversion_recovers_IQU(self):
        self.make_exposure("e1", 0.8, 0.3, -0.2)
        run = self.wh.invert("e1", self.prod["payload"])
        self.assertEqual(run["solution"]["run_status"], "solved")
        self.assertEqual(run["solution"]["status"], "consistent")
        s = run["solution"]
        self.assertAlmostEqual(s["I"], 0.8, places=9)
        self.assertAlmostEqual(s["Q"], 0.3, places=9)
        self.assertAlmostEqual(s["U"], -0.2, places=9)
        self.assertLess(s["sum_squared_weighted_residual"], 1e-18)
        self.assertEqual(len(s["channels"]), 4)
        for c in s["channels"]:
            self.assertIn("corrected_s", c)
            self.assertIn("predicted_y_raw", c)
            self.assertAlmostEqual(c["residual"], 0.0, places=9)

    def test_all_angles_repeated_rank_deficient(self):
        # a design where every channel shares theta; use unit gain/zero offset
        angles = {"a": 30.0, "b": 30.0, "c": 30.0, "d": 30.0}
        design = self.cal.register_channels(angles)
        for name, iq in (("r1", REF), ("r2", REF2)):
            y = synth_y(iq["I"], iq["Q"], iq["U"], angles,
                        {k: 1.0 for k in angles}, {k: 0.0 for k in angles})
            self.measure(name, iq, y)
        prod = self.cal.publish(design)
        self.wh.add_exposure("e", [
            {"channel": ch, "weight": 1.0,
             "y_raw": ideal_s(0.5, 0.1, 0.1, 30.0)} for ch in angles])
        run = self.wh.invert("e", prod["payload"])
        self.assertEqual(run["solution"]["run_status"], "rank_deficient")
        self.assertIsNone(run["solution"]["I"])
        self.assertLess(run["solution"]["rank_diagnostics"]["rank"], 3)

    def test_near_collinear_rank_deficient(self):
        angles = {"a": 0.0, "b": 0.0000000001, "c": 0.0000000002}
        design = self.cal.register_channels(angles)
        for name, iq in (("r1", REF), ("r2", REF2)):
            y = synth_y(iq["I"], iq["Q"], iq["U"], angles,
                        {k: 1.0 for k in angles}, {k: 0.0 for k in angles})
            self.measure(name, iq, y)
        prod = self.cal.publish(design)
        self.wh.add_exposure("e", [
            {"channel": "a", "y_raw": 1.0, "weight": 1.0},
            {"channel": "b", "y_raw": 1.1, "weight": 1.0},
            {"channel": "c", "y_raw": 1.2, "weight": 1.0}])
        run = self.wh.invert("e", prod["payload"])
        self.assertEqual(run["solution"]["run_status"], "rank_deficient")

    def test_missing_channel_routed_not_zero_filled(self):
        self.make_exposure("e", 0.8, 0.3, -0.2, chosen=["c0", "c45", "c90"])
        run = self.wh.invert("e", self.prod["payload"])
        sol = run["solution"]
        self.assertEqual(sol["run_status"], "solved")
        self.assertIn("c135", sol["qc"]["missing"])
        self.assertNotIn("c135", [c["channel"] for c in sol["channels"]])

    def test_uncalibratable_channel_routed(self):
        cal2_path = tmp_db()
        cal2 = CalibrationStore(cal2_path)
        cal2.init_db()
        angles = {"c0": 0.0, "c45": 45.0, "c90": 90.0}
        design = cal2.register_channels(angles)
        # vary U only so c90's s = (I - Q)/2 is constant across references
        for name, iq in (("a", {"I": 1.0, "Q": 0.2, "U": 0.1}),
                         ("b", {"I": 1.0, "Q": 0.2, "U": 0.4})):
            y = synth_y(iq["I"], iq["Q"], iq["U"], angles, GAINS, OFFSETS)
            cal2.record_measurement(name, iq["I"], iq["Q"], iq["U"], y)
        prod = cal2.publish(design)
        self.assertEqual(prod["payload"]["channels"]["c90"]["status"],
                         "uncalibratable")
        self.wh.add_exposure("e", [
            {"channel": "c0", "y_raw": 1.0, "weight": 1.0},
            {"channel": "c45", "y_raw": 1.0, "weight": 1.0},
            {"channel": "c90", "y_raw": 1.0, "weight": 1.0}])
        run = self.wh.invert("e", prod["payload"])
        self.assertIn("c90", run["solution"]["qc"]["uncalibratable"])
        os.unlink(cal2_path)

    def test_insufficient_channels_refused_not_zero_filled(self):
        self.make_exposure("e", 0.8, 0.3, -0.2, chosen=["c0", "c45"])
        run = self.wh.invert("e", self.prod["payload"])
        self.assertEqual(run["solution"]["run_status"], "insufficient_channels")
        self.assertIsNone(run["solution"]["I"])

    def test_gain_error_breaks_consistency(self):
        bad = dict(self.prod["payload"])
        bad["channels"] = {k: dict(v) for k, v in bad["channels"].items()}
        bad["channels"]["c0"]["gain"] = GAINS["c0"] * 3.0
        self.make_exposure("e", 0.8, 0.3, -0.2)
        run = self.wh.invert("e", bad)
        self.assertEqual(run["solution"]["run_status"], "solved")
        self.assertGreater(run["solution"]["sum_squared_weighted_residual"], 1e-3)

    def test_zero_intensity_dolp_undefined(self):
        self.make_exposure("e", 0.0, 0.0, 0.0)
        run = self.wh.invert("e", self.prod["payload"])
        sol = run["solution"]
        self.assertEqual(sol["status"], "consistent")
        self.assertFalse(sol["polarization"]["dolp_defined"])
        self.assertIn("dolp_undefined_I_zero",
                      sol["polarization"]["undefined_reasons"])
        self.assertFalse(sol["polarization"]["angle_defined"])

    def test_angle_undefined_when_QU_zero(self):
        self.make_exposure("e", 1.0, 0.0, 0.0)
        run = self.wh.invert("e", self.prod["payload"])
        sol = run["solution"]
        self.assertTrue(sol["polarization"]["dolp_defined"])
        self.assertAlmostEqual(sol["polarization"]["dolp"], 0.0, places=9)
        self.assertFalse(sol["polarization"]["angle_defined"])
        self.assertIn("angle_undefined_Q_U_zero",
                      sol["polarization"]["undefined_reasons"])

    def test_boundary_radial_equals_I2(self):
        I = 0.7
        self.make_exposure("e", I, I * math.cos(0.3), I * math.sin(0.3))
        run = self.wh.invert("e", self.prod["payload"])
        sol = run["solution"]
        self.assertEqual(sol["status"], "consistent")
        self.assertAlmostEqual(sol["polarization"]["dolp"], 1.0, places=6)
        self.assertAlmostEqual(sol["polarization"]["angle_deg"],
                               math.degrees(0.3) / 2.0, places=6)

    def test_model_inconsistent_keeps_unconstrained(self):
        I, Q, U = 0.5, 0.5, 0.5  # radial=0.5 > I^2=0.25
        self.make_exposure("e", I, Q, U)
        run = self.wh.invert("e", self.prod["payload"])
        sol = run["solution"]
        self.assertEqual(sol["run_status"], "solved")
        self.assertEqual(sol["status"], "model_inconsistent")
        self.assertAlmostEqual(sol["I"], I, places=9)
        self.assertAlmostEqual(sol["Q"], Q, places=9)
        self.assertAlmostEqual(sol["U"], U, places=9)
        self.assertFalse(sol["polarization"]["dolp_defined"])

    def test_negative_I_flagged_inconsistent(self):
        self.make_exposure("e", -0.5, 0.1, 0.1)
        run = self.wh.invert("e", self.prod["payload"])
        sol = run["solution"]
        self.assertEqual(sol["status"], "model_inconsistent")
        self.assertAlmostEqual(sol["I"], -0.5, places=9)

    def test_angle_normalized_to_0_180(self):
        # Q<0, U=0 -> atan2(0,negative)/2 = 90 deg
        self.make_exposure("e", 1.0, -0.5, 0.0)
        run = self.wh.invert("e", self.prod["payload"])
        ang = run["solution"]["polarization"]["angle_deg"]
        self.assertGreaterEqual(ang, 0.0)
        self.assertLess(ang, 180.0)
        self.assertAlmostEqual(ang, 90.0, places=6)

    def test_rerun_after_retraction_changes_solution(self):
        # a genuinely wrong reading in one reference biases the fit; after
        # retracting it, a rerun against the new product differs.
        self.make_exposure("e", 0.8, 0.3, -0.2)
        # inject a bad reference (stated 1,0,0 but readings +5%)
        bad_y = {ch: (GAINS[ch] * ideal_s(1.0, 0.0, 0.0, ORTHO2[ch])
                      + OFFSETS[ch]) * 1.05 for ch in ORTHO2}
        self.measure("bad", {"I": 1.0, "Q": 0.0, "U": 0.0}, bad_y)
        biased = self.cal.publish(self.design)
        run1 = self.wh.invert("e", biased["payload"])
        self.cal.retract_reference("bad", "withdrawn")
        clean = self.cal.publish(self.design)
        run2 = self.wh.rerun(run1["run_id"], clean["payload"])
        # gains differ, so the inversion of the same readings differs
        self.assertNotAlmostEqual(
            biased["payload"]["channels"]["c0"]["gain"],
            clean["payload"]["channels"]["c0"]["gain"], places=8)
        self.assertNotAlmostEqual(run1["solution"]["Q"], run2["solution"]["Q"],
                                  places=8)
        # old run is untouched and still bound to the biased product
        old = self.wh.get_run(run1["run_id"])
        self.assertEqual(old["product_id"], biased["product_id"])
        self.assertEqual(old["solution"]["Q"], run1["solution"]["Q"])

    def test_exposure_unique_channel(self):
        with self.assertRaises(ValidationError):
            self.wh.add_exposure("dup", [
                {"channel": "c0", "y_raw": 1.0, "weight": 1.0},
                {"channel": "c0", "y_raw": 2.0, "weight": 1.0}])

    def test_nonpositive_weight_rejected(self):
        with self.assertRaises(ValidationError):
            self.wh.add_exposure("e", [
                {"channel": "c0", "y_raw": 1.0, "weight": 0.0}])

    def test_solution_minimizes_reported_raw_space_ssr(self):
        # Unequal gains and unequal weights: the solve must minimize the
        # raw-space weighted RSS it reports, not the design-space one.
        readings = []
        truth = (0.8, 0.3, -0.2)
        for i, ch in enumerate(ORTHO2):
            y = (GAINS[ch] * ideal_s(*truth, ORTHO2[ch]) + OFFSETS[ch]
                 + 0.004 * (i + 1) * (-1) ** i)  # deterministic perturbation
            readings.append({"channel": ch, "y_raw": y,
                             "weight": float(i + 1)})  # weights 1..4
        self.wh.add_exposure("e", readings)
        run = self.wh.invert("e", self.prod["payload"])
        sol = run["solution"]
        I, Q, U = sol["I"], sol["Q"], sol["U"]
        pch = self.prod["payload"]["channels"]

        def raw_ssr(a, b, c):
            tot = 0.0
            for r in readings:
                ch = r["channel"]
                s = ideal_s(a, b, c, pch[ch]["theta_deg"])
                yhat = pch[ch]["gain"] * s + pch[ch]["offset"]
                resid = r["y_raw"] - yhat
                tot += r["weight"] * resid * resid
            return tot

        base = raw_ssr(I, Q, U)
        self.assertAlmostEqual(base, sol["sum_squared_weighted_residual"], places=12)
        # perturbing the solution cannot reduce the reported (minimized) SSR
        for d in (1e-4, -1e-4):
            self.assertGreaterEqual(raw_ssr(I + d, Q, U), base - 1e-15)
            self.assertGreaterEqual(raw_ssr(I, Q + d, U), base - 1e-15)
            self.assertGreaterEqual(raw_ssr(I, Q, U + d), base - 1e-15)

    def test_reference_correction_rerun_old_run_bound(self):
        self.make_exposure("e", 0.8, 0.3, -0.2)
        run1 = self.wh.invert("e", self.prod["payload"])
        v1 = self.prod["product_version"]

        # correct a reference value (version bump) and republish
        self.measure("r2", {"I": 1.5, "Q": 0.2, "U": 0.6},
                     {"c0": 7.0, "c45": 5.0, "c90": 4.0, "c135": 5.0},
                     correct=True)
        prod2 = self.cal.publish(self.design)
        self.assertEqual(prod2["product_version"], v1 + 1)

        run2 = self.wh.rerun(run1["run_id"], prod2["payload"])
        self.assertEqual(run2["product_id"], prod2["product_id"])
        self.assertEqual(run2["reof_of"], run1["run_id"])
        # old run still points at old product and unchanged solution
        old = self.wh.get_run(run1["run_id"])
        self.assertEqual(old["product_id"], self.prod["product_id"])
        self.assertEqual(old["product_version"], v1)
        self.assertAlmostEqual(old["solution"]["I"], 0.8, places=9)


class TestImportAndTrace(Base):
    def build_external(self):
        chans = {}
        for ch in ORTHO:
            chans[ch] = {
                "theta_deg": ORTHO[ch], "status": "calibrated",
                "gain": GAINS[ch], "offset": OFFSETS[ch],
                "n_points": 3, "n_distinct_s": 3, "reason": None,
                "residual_ss": 0.0, "r_squared": 1.0,
                "attempted_gain": GAINS[ch], "attempted_offset": OFFSETS[ch]}
        return {
            "format": model.PRODUCT_FORMAT,
            "model_version": model.MODEL_VERSION,
            "model_statement": model.MODEL_STATEMENT,
            "disclaimer": model.DISCLAIMER,
            "product_id": "external-A",
            "product_version": 7,
            "created_at": "2026-01-01T00:00:00Z",
            "kind": "external",
            "source_product_id": None,
            "design_id": "ext-design",
            "design_fingerprint": "abc",
            "config": dict(model.DEFAULT_CONFIG),
            "channels": chans,
            "status_summary": {"calibrated": 3, "uncalibratable": 0},
        }

    def test_external_product_consumed_without_refs(self):
        payload = self.build_external()
        imported = self.cal.import_product(payload)
        self.assertEqual(imported["kind"], "external")
        readings = []
        for ch in ORTHO:
            c = payload["channels"][ch]
            readings.append({"channel": ch, "weight": 1.0,
                             "y_raw": c["gain"] * ideal_s(0.6, 0.2, -0.1,
                                                          c["theta_deg"])
                             + c["offset"]})
        self.wh.add_exposure("e", readings)
        stored = self.cal.get_product(imported["product_id"], kind="external")
        run = self.wh.invert("e", stored["payload"])
        self.assertEqual(run["solution"]["status"], "consistent")
        self.assertAlmostEqual(run["solution"]["I"], 0.6, places=9)
        self.assertAlmostEqual(run["solution"]["Q"], 0.2, places=9)

    def test_warehouse_consumes_foreign_payload_directly(self):
        # A warehouse backed by its own DB and given an external payload it never
        # imported anywhere: it must invert using only the payload content.
        wh2_path = tmp_db()
        wh2 = ObservationWarehouse(wh2_path)
        wh2.init_db()
        try:
            payload = self.build_external()
            wh2.add_exposure("e", [
                {"channel": ch, "weight": 1.0,
                 "y_raw": payload["channels"][ch]["gain"]
                 * ideal_s(0.6, 0.2, -0.1, payload["channels"][ch]["theta_deg"])
                 + payload["channels"][ch]["offset"]}
                for ch in ORTHO])
            run = wh2.invert("e", payload)
            self.assertEqual(run["solution"]["status"], "consistent")
            self.assertAlmostEqual(run["solution"]["U"], -0.1, places=9)
            self.assertEqual(run["product_id"], "external-A")
            self.assertEqual(run["product_version"], 7)
        finally:
            os.unlink(wh2_path)

    def test_import_rejects_bad_product(self):
        with self.assertRaises(ValidationError):
            self.cal.import_product({"format": "nope"})
        bad = {
            "format": model.PRODUCT_FORMAT,
            "model_version": model.MODEL_VERSION,
            "config": dict(model.DEFAULT_CONFIG),
            "channels": {"c0": {"theta_deg": 0.0, "status": "uncalibratable",
                                "reason": None}},
        }
        with self.assertRaises(ValidationError):
            self.cal.import_product(bad)

    def test_import_dedup_and_integrity(self):
        payload = self.build_external()
        a = self.cal.import_product(payload)
        # same content, different upstream id/version -> deduplicated
        p2 = dict(payload)
        p2["product_id"] = "external-A-copy"
        p2["product_version"] = 999
        b = self.cal.import_product(p2)
        self.assertTrue(b["deduplicated"])
        self.assertEqual(b["product_id"], a["product_id"])
        # stored payload hash matches the stored payload itself
        stored = self.cal.get_product(a["product_id"], kind="external")
        self.assertEqual(model.sha256_of(stored["payload"]),
                         stored["payload_sha256"])

    def test_standalone_correction(self):
        design = self.cal.register_channels(ORTHO)
        for name, iq in (("r1", REF), ("r2", REF2)):
            self.ingest_truth(name, iq, ORTHO)
        prod = self.cal.publish(design)
        y = GAINS["c0"] * ideal_s(0.9, 0.1, 0.2, 0.0) + OFFSETS["c0"]
        out = apply_channel_correction(prod["payload"], "c0", y)
        self.assertAlmostEqual(out["corrected_s"], ideal_s(0.9, 0.1, 0.2, 0.0),
                               places=9)

    def test_uncalibratable_correction_refused(self):
        design = self.cal.register_channels(ORTHO)
        self.ingest_truth("r1", REF, ORTHO)
        prod = self.cal.publish(design)
        with self.assertRaises(ValidationError):
            apply_channel_correction(prod["payload"], "c0", 1.0)

    def test_reopen_run_integrity(self):
        design = self.cal.register_channels(ORTHO)
        for name, iq in (("r1", REF), ("r2", REF2)):
            self.ingest_truth(name, iq, ORTHO)
        prod = self.cal.publish(design)
        self.wh.add_exposure("e", [
            {"channel": ch, "weight": 1.0,
             "y_raw": GAINS[ch] * ideal_s(0.5, 0.1, 0.1, ORTHO[ch]) + OFFSETS[ch]}
            for ch in ORTHO])
        run = self.wh.invert("e", prod["payload"])
        re = self.wh.reopen_run(run["run_id"], prod["payload"])
        self.assertTrue(re["integrity"]["run_solution_sha256_ok"])
        self.assertTrue(re["integrity"]["product_sha256_ok"])

    def test_trace_exposure_and_product(self):
        design = self.cal.register_channels(ORTHO)
        for name, iq in (("r1", REF), ("r2", REF2)):
            self.ingest_truth(name, iq, ORTHO)
        prod = self.cal.publish(design)
        self.wh.add_exposure("e", [
            {"channel": ch, "weight": 1.0,
             "y_raw": GAINS[ch] * ideal_s(0.5, 0.1, 0.1, ORTHO[ch]) + OFFSETS[ch]}
            for ch in ORTHO])
        run = self.wh.invert("e", prod["payload"])
        texp = self.wh.trace_exposure("e")
        self.assertEqual(texp["runs"][0]["run_id"], run["run_id"])
        tprod = self.cal.trace_product(prod["product_id"])
        self.assertEqual(tprod["product"]["product_id"], prod["product_id"])

    def test_angle_period_180(self):
        self.assertAlmostEqual(canonical_theta(-30.0), 150.0)
        self.assertAlmostEqual(canonical_theta(210.0), 30.0)

    def test_retract_whole_reference_bumps_version(self):
        design = self.cal.register_channels(ORTHO)
        for name, iq in (("r1", REF), ("r2", REF2), ("r3", REF3)):
            self.ingest_truth(name, iq, ORTHO)
        p1 = self.cal.publish(design)
        self.assertEqual(p1["payload"]["channels"]["c0"]["n_points"], 3)
        out = self.cal.retract_reference("r2", "reference withdrawn")
        self.assertEqual(len(out["retracted_measurement_ids"]), 1)
        p2 = self.cal.publish(design)
        self.assertEqual(p2["product_version"], p1["product_version"] + 1)
        self.assertEqual(p2["payload"]["channels"]["c0"]["n_points"], 2)
        # retracted measurement retained with truthul status
        rows = self.cal.list_measurements()
        r2 = [m for m in rows if m["reference_id"] == "r2"][0]
        self.assertEqual(r2["status"], "retracted")
        self.assertEqual(r2["retraction_reason"], "reference withdrawn")
        # old published product still reports 3 points
        old = self.cal.get_product(p1["product_id"])
        self.assertEqual(old["payload"]["channels"]["c0"]["n_points"], 3)

    def test_trace_reference_provenance_in_product(self):
        design = self.cal.register_channels(ORTHO)
        self.ingest_truth("r1", REF, ORTHO)
        self.ingest_truth("r2", REF2, ORTHO)
        p1 = self.cal.publish(design)
        tr = self.cal.trace_reference("r1")
        prod = [x for x in tr["product_versions"]
                if x["product_id"] == p1["product_id"]][0]
        self.assertTrue(prod["included"])
        self.assertEqual(prod["included_reference_versions"], [1])
        # after retracting r1, the next product no longer includes it
        self.cal.retract_reference("r1", "withdrawn")
        p2 = self.cal.publish(design)
        tr = self.cal.trace_reference("r1")
        p2entry = [x for x in tr["product_versions"]
                   if x["product_id"] == p2["product_id"]][0]
        self.assertFalse(p2entry["included"])

    def test_trace_product_retains_fit_points(self):
        design = self.cal.register_channels(ORTHO)
        for name, iq in (("r1", REF), ("r2", REF2)):
            self.ingest_truth(name, iq, ORTHO)
        prod = self.cal.publish(design)
        tr = self.cal.trace_product(prod["product_id"])
        for ch in ORTHO:
            pts = tr["fit_points"][ch]
            self.assertEqual(len(pts), 2)
            for pt in pts:
                self.assertIn("s", pt)
                self.assertIn("y", pt)
                self.assertIn("residual", pt)
                # independently recompute the residual from the product gain
                c = tr["product"]["channels"][ch]
                self.assertAlmostEqual(
                    pt["y"] - (c["gain"] * pt["s"] + c["offset"]),
                    pt["residual"], places=9)


if __name__ == "__main__":
    unittest.main()
