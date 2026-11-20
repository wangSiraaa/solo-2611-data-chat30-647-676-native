"""Tests for the VentNet catalog, solver, experiment, and store."""

from __future__ import annotations

import unittest

from ventnet import (
    BalanceError,
    Catalog,
    ConflictError,
    LimitError,
    ValidationError,
)
from ventnet.experiment import Experiment, SourceEvent, Visit, run_experiment
from ventnet.solver import build_timeline
from ventnet.db import Store
from tests import reference


def two_room_catalog(q="0.05", r="0.1", V="30"):
    c = Catalog("two-room")
    c.add_room("A", V).add_room("B", V)
    c.add_supply("A", "0.1").add_supply("B", "0.1")
    c.add_transfer("A", "B", q).add_transfer("B", "A", q)
    c.add_exhaust("A", r).add_exhaust("B", r)
    return c.publish()


class CatalogTests(unittest.TestCase):
    def test_balance_rejects_unbalanced(self):
        c = Catalog("bad")
        c.add_room("A", 10).add_room("B", 10)
        c.add_supply("A", 1)
        c.add_transfer("A", "B", "0.5")
        c.add_exhaust("A", "0.5")
        # B has inflow 0.5 but no outflow
        with self.assertRaises(BalanceError):
            c.publish()

    def test_balance_accepts(self):
        c = two_room_catalog()
        self.assertTrue(c.balance_report()["balanced"])
        self.assertTrue(c.identity.startswith("sha256:"))

    def test_nonnegative_and_positive(self):
        c = Catalog("x")
        with self.assertRaises(ValidationError):
            c.add_room("A", 0)
        with self.assertRaises(ValidationError):
            c.add_room("A", -5)
        c.add_room("A", 5)
        with self.assertRaises(ValidationError):
            c.add_supply("A", -1)

    def test_room_limit(self):
        c = Catalog("many")
        for i in range(8):
            c.add_room(f"r{i}", 1)
        with self.assertRaises(LimitError):
            c.add_room("r8", 1)

    def test_identity_is_content_addressed(self):
        a = two_room_catalog()
        b = two_room_catalog()
        self.assertEqual(a.identity, b.identity)
        c = two_room_catalog(q="0.06")
        self.assertNotEqual(a.identity, c.identity)

    def test_revision_preserves_old(self):
        base = two_room_catalog()
        old_id = base.identity
        # balance-preserving: raise both exchange directions equally
        revised = base.revise_flows({("A", "B"): "0.06", ("B", "A"): "0.06"})
        self.assertEqual(base.identity, old_id)
        self.assertNotEqual(revised.identity, old_id)
        self.assertEqual(revised.parent_identity, old_id)
        self.assertEqual(revised.revision, 1)

    def test_revision_must_balance(self):
        base = two_room_catalog()
        with self.assertRaises(BalanceError):
            base.revise_flow("A", "B", "0.09")  # would unbalance A

    def test_external_product_roundtrip(self):
        base = two_room_catalog()
        product = base.to_dict()
        loaded = Catalog.from_product(product)
        self.assertEqual(loaded.identity, base.identity)


class SolverAnalyticTests(unittest.TestCase):
    def test_single_room_exponential(self):
        V, q, s, c0 = 20.0, 0.4, 2.0, 1.0
        c = Catalog("single")
        c.add_room("A", V)
        c.add_supply("A", q)
        c.add_exhaust("A", q)
        c.publish()
        exp = Experiment(
            catalog=c,
            initial_concentration={"A": c0},
            source_events=[SourceEvent(0.0, "A", s)],
            window_start=0.0,
            window_end=50.0,
            visits=[Visit("v", "p", "A", 0.0, 50.0)],
        )
        res = run_experiment(exp)
        c_end, integral = reference.single_room(V, q, s, c0, 0.0, 50.0)
        self.assertAlmostEqual(res.concentrations[-1, 0], c_end, places=9)
        self.assertAlmostEqual(
            res.visit_integrals[0]["concentration_integral_mg_s_per_m3"],
            integral,
            places=9,
        )

    def test_single_room_window_start(self):
        V, q, c0 = 20.0, 0.4, 1.0
        c = Catalog("single")
        c.add_room("A", V).add_supply("A", q).add_exhaust("A", q).publish()
        exp = Experiment(
            catalog=c,
            initial_concentration={"A": c0},
            source_events=[],
            window_start=10.0,
            window_end=40.0,
            visits=[Visit("v", "p", "A", 10.0, 40.0)],
        )
        res = run_experiment(exp)
        c_end, _ = reference.single_room(V, q, 0.0, c0, 0.0, 40.0)
        self.assertAlmostEqual(res.concentrations[-1, 0], c_end, places=9)
        c_at_10, _ = reference.single_room(V, q, 0.0, c0, 0.0, 10.0)
        _, integral = reference.single_room(V, q, 0.0, c_at_10, 10.0, 40.0)
        self.assertAlmostEqual(
            res.visit_integrals[0]["concentration_integral_mg_s_per_m3"], integral, places=9
        )

    def test_two_room_symmetric_eigen(self):
        V, q, r = 30.0, 0.05, 0.1
        c = two_room_catalog(q="0.05", r="0.1", V="30")
        sA, sB = 1.0, 0.0
        c0A, c0B = 0.0, 0.0
        exp = Experiment(
            catalog=c,
            initial_concentration={"A": c0A, "B": c0B},
            source_events=[SourceEvent(0.0, "A", sA)],
            window_start=0.0,
            window_end=60.0,
            visits=[
                Visit("va", "p", "A", 0.0, 60.0),
                Visit("vb", "p2", "B", 5.0, 55.0),
            ],
        )
        res = run_experiment(exp)
        cA, cB = reference.two_room_symmetric(V, q, r, sA, sB, c0A, c0B, 60.0)
        self.assertAlmostEqual(res.concentrations[-1, 0], cA, places=8)
        self.assertAlmostEqual(res.concentrations[-1, 1], cB, places=8)
        IA, IB = reference.two_room_symmetric_integral(
            V, q, r, sA, sB, c0A, c0B, 5.0, 55.0
        )
        self.assertAlmostEqual(
            res.visit_integrals[1]["concentration_integral_mg_s_per_m3"], IB, places=8
        )
        IA_full, _ = reference.two_room_symmetric_integral(
            V, q, r, sA, sB, c0A, c0B, 0.0, 60.0
        )
        self.assertAlmostEqual(
            res.visit_integrals[0]["concentration_integral_mg_s_per_m3"],
            IA_full,
            places=8,
        )


class TimestepTests(unittest.TestCase):
    def test_same_second_grouping_no_zero_segments(self):
        c = two_room_catalog()
        matrix = c.matrices()
        timeline = build_timeline(
            matrix,
            [(10.0, "A", 1.0), (10.0, "B", 2.0)],
            0.0,
            100.0,
        )
        for seg in timeline.segments:
            self.assertGreater(seg.t1 - seg.t0, 0)
        # only one state after t=10
        active = timeline.source_vector_at(10.0)
        self.assertAlmostEqual(active[matrix["index"]["A"]], 1.0)
        self.assertAlmostEqual(active[matrix["index"]["B"]], 2.0)

    def test_conflicting_same_second_rejected(self):
        c = two_room_catalog()
        matrix = c.matrices()
        with self.assertRaises(ValidationError):
            build_timeline(
                matrix,
                [(10.0, "A", 1.0), (10.0, "A", 2.0)],
                0.0,
                100.0,
            )

    def test_zero_flow_and_closed_component(self):
        # Room B fully closed (no supply, no exhaust, no transfer) -> singular but legal
        c = Catalog("closed")
        c.add_room("A", 10).add_room("B", 10)
        c.add_supply("A", 1).add_exhaust("A", 1)
        c.publish()
        exp = Experiment(
            catalog=c,
            initial_concentration={"A": 0, "B": 5},
            source_events=[SourceEvent(0, "A", 2)],
            window_start=0,
            window_end=30,
            visits=[Visit("v", "p", "B", 0, 30)],
        )
        res = run_experiment(exp)
        # B constant
        self.assertAlmostEqual(res.concentrations[-1, 1], 5.0, places=9)
        self.assertAlmostEqual(
            res.visit_integrals[0]["concentration_integral_mg_s_per_m3"], 150.0, places=9
        )
        self.assertTrue(res.mass_balance["ok"])

    def test_closed_two_room_no_exhaust(self):
        # Two rooms exchanging only; no outdoor exchange. Singular K but legal.
        c2 = Catalog("closed-symmetric")
        c2.add_room("A", 10).add_room("B", 10)
        c2.add_transfer("A", "B", "0.5").add_transfer("B", "A", "0.5")
        c2.publish()
        exp = Experiment(
            catalog=c2,
            initial_concentration={"A": 10, "B": 0},
            source_events=[],
            window_start=0,
            window_end=200,
            visits=[Visit("v", "p", "A", 0, 200)],
        )
        res = run_experiment(exp)
        # total mass conserved, no exhaust
        self.assertAlmostEqual(res.concentrations[-1].sum(), 10.0, places=8)
        self.assertAlmostEqual(res.mass_balance["outdoor_exhaust"], 0.0, places=12)
        self.assertTrue(res.mass_balance["ok"])
        # A decays toward 5
        self.assertAlmostEqual(res.concentrations[-1, 0], 5.0, places=6)

    def test_piecewise_source(self):
        # source on [0,20), off [20,60)
        V, q = 10.0, 0.5
        s = 3.0
        c = Catalog("piecewise")
        c.add_room("A", V).add_supply("A", q).add_exhaust("A", q).publish()
        exp = Experiment(
            catalog=c,
            initial_concentration={"A": 0},
            source_events=[SourceEvent(0, "A", s), SourceEvent(20, "A", 0)],
            window_start=0,
            window_end=60,
            visits=[Visit("v", "p", "A", 0, 60)],
        )
        res = run_experiment(exp)
        c20, I1 = reference.single_room(V, q, s, 0, 0, 20)
        c60, I2 = reference.single_room(V, q, 0, c20, 20, 60)
        self.assertAlmostEqual(res.concentrations[-1, 0], c60, places=9)
        self.assertAlmostEqual(
            res.visit_integrals[0]["concentration_integral_mg_s_per_m3"], I1 + I2, places=9
        )
        # explanation must list two segments
        segs = res.explanations[0]["segments_used"]
        self.assertEqual(len(segs), 2)


class BalanceTests(unittest.TestCase):
    def test_mass_balance_with_internal_flow(self):
        c = two_room_catalog()
        exp = Experiment(
            catalog=c,
            initial_concentration={"A": 5, "B": 1},
            source_events=[SourceEvent(0, "A", 2)],
            window_start=0,
            window_end=80,
            visits=[],
        )
        res = run_experiment(exp)
        mb = res.mass_balance
        # dStorage = source - exhaust
        self.assertAlmostEqual(
            mb["delta_storage"], mb["source_input"] - mb["outdoor_exhaust"], places=7
        )
        self.assertTrue(mb["ok"])
        # internal flows not double counted: residual tiny
        self.assertLess(abs(mb["residual"]), 1e-8)

    def test_exhaust_mass_matches_single_room(self):
        V, q, s, c0 = 15.0, 0.3, 1.0, 0.0
        c = Catalog("s")
        c.add_room("A", V).add_supply("A", q).add_exhaust("A", q).publish()
        exp = Experiment(
            catalog=c,
            initial_concentration={"A": c0},
            source_events=[SourceEvent(0, "A", s)],
            window_start=0,
            window_end=100,
            visits=[],
        )
        res = run_experiment(exp)
        # exhaust integral = q * integral c dt ; integral from 0..inf would be s/q * T
        # compare stored mass with q*int(c)
        _, integral = reference.single_room(V, q, s, c0, 0.0, 100.0)
        self.assertAlmostEqual(res.exhausted_mass[-1, 0], q * integral, places=8)


class VisitTests(unittest.TestCase):
    def test_conflict_rejected_whole_batch(self):
        c = two_room_catalog()
        with self.assertRaises(ConflictError):
            Experiment(
                catalog=c,
                initial_concentration={"A": 0, "B": 0},
                source_events=[],
                window_start=0,
                window_end=100,
                visits=[
                    Visit("v1", "alice", "A", 0, 50),
                    Visit("v2", "alice", "B", 30, 80),
                    Visit("v3", "bob", "A", 0, 10),
                ],
            )

    def test_touching_visits_ok(self):
        c = two_room_catalog()
        exp = Experiment(
            catalog=c,
            initial_concentration={"A": 0, "B": 0},
            source_events=[],
            window_start=0,
            window_end=100,
            visits=[
                Visit("v1", "alice", "A", 0, 50),
                Visit("v2", "alice", "B", 50, 80),
            ],
        )
        res = run_experiment(exp)
        self.assertAlmostEqual(
            res.person_totals["alice"]["integral_mg_s_per_m3"],
            res.visit_integrals[0]["concentration_integral_mg_s_per_m3"]
            + res.visit_integrals[1]["concentration_integral_mg_s_per_m3"],
            places=12,
        )

    def test_integral_not_endpoint_trapezoid(self):
        # strongly exponential room; trapezoid of endpoints differs a lot
        V, q, c0 = 5.0, 2.0, 10.0
        c = Catalog("decay")
        c.add_room("A", V).add_supply("A", q).add_exhaust("A", q).publish()
        exp = Experiment(
            catalog=c,
            initial_concentration={"A": c0},
            source_events=[],
            window_start=0,
            window_end=4.0,
            visits=[Visit("v", "p", "A", 0, 4.0)],
        )
        res = run_experiment(exp)
        trapz = (c0 + res.concentrations[-1, 0]) / 2 * 4.0
        _, exact = reference.single_room(V, q, 0.0, c0, 0, 4.0)
        got = res.visit_integrals[0]["concentration_integral_mg_s_per_m3"]
        self.assertAlmostEqual(got, exact, places=7)
        self.assertGreater(abs(got - trapz), 1.0)


class ExternalProductTests(unittest.TestCase):
    def test_store_consumes_external_product(self):
        base = two_room_catalog()
        product = base.to_dict()  # same format as an external product
        store = Store(":memory:")
        try:
            ext = Catalog.from_product(product)
            store.save_catalog(ext)
            exp = store.import_experiment(
                catalog=ext,
                initial_concentration={"A": 0, "B": 0},
                source_events=[{"time": 0, "room": "A", "strength": 1.0}],
                window=[0, 60],
                visits=[{"visit_id": "v", "person": "p", "room": "B", "start": 0, "end": 60}],
            )
            payload = store.run(exp.identity)
            self.assertTrue(payload["mass_balance"]["ok"])
            self.assertEqual(payload["catalog_identity"], base.identity)
        finally:
            store.close()

    def test_tampered_product_detected(self):
        base = two_room_catalog()
        product = base.to_dict()
        # tamper a flow to an unbalanced value
        for edge in product["canonical"]["edges"]:
            if edge["src"] == "A" and edge["dst"] == "B":
                edge["flow"] = {"num": 9, "den": 100}
        with self.assertRaises(BalanceError):
            Catalog.from_product(product)


class TransferResponseTests(unittest.TestCase):
    def test_response_recomputable(self):
        from ventnet.transfer import transfer_response

        base = two_room_catalog()
        resp = transfer_response(base)
        table = resp.response_table(10.0)
        self.assertEqual(table["catalog_identity"], base.identity)
        # self transfer A->A between 0 and 1
        g_aa = table["concentration_from_initial"]["A->A"]
        self.assertTrue(0.0 < g_aa < 1.0)

    def test_source_operator_matches_experiment(self):
        from ventnet.transfer import transfer_response

        base = two_room_catalog()
        resp = transfer_response(base)
        H = resp.source_operator(0.0, 50.0)
        # constant source s applied to zero initial -> c(50) = H @ s
        import numpy as np

        s = np.array([1.0, 0.0])
        predicted = H @ s
        exp = Experiment(
            catalog=base,
            initial_concentration={"A": 0, "B": 0},
            source_events=[SourceEvent(0.0, "A", 1.0)],
            window_start=0.0,
            window_end=50.0,
            visits=[],
        )
        res = run_experiment(exp)
        self.assertAlmostEqual(res.concentrations[-1, 0], predicted[0], places=7)
        self.assertAlmostEqual(res.concentrations[-1, 1], predicted[1], places=7)


class WindowTests(unittest.TestCase):
    def test_window_start_mass_balance(self):
        # Run with window starting after process start; mass balance over window
        base = two_room_catalog()
        exp = Experiment(
            catalog=base,
            initial_concentration={"A": 3, "B": 1},
            source_events=[SourceEvent(0, "A", 1.0)],
            window_start=20,
            window_end=80,
            visits=[Visit("v", "p", "A", 20, 80)],
        )
        res = run_experiment(exp)
        mb = res.mass_balance
        self.assertAlmostEqual(
            mb["delta_storage"], mb["source_input"] - mb["outdoor_exhaust"], places=7
        )
        self.assertTrue(mb["ok"])

    def test_visit_outside_window_rejected(self):
        base = two_room_catalog()
        with self.assertRaises(ValidationError):
            Experiment(
                catalog=base,
                initial_concentration={"A": 0, "B": 0},
                source_events=[],
                window_start=10,
                window_end=50,
                visits=[Visit("v", "p", "A", 0, 30)],
            )


class LimitsTests(unittest.TestCase):
    def test_source_change_limit(self):
        c = two_room_catalog()
        events = [SourceEvent(t, "A", 1.0) for t in range(51)]
        with self.assertRaises(LimitError):
            Experiment(
                catalog=c,
                initial_concentration={"A": 0, "B": 0},
                source_events=events,
                window_start=0,
                window_end=100,
                visits=[],
            )

    def test_duration_limit(self):
        c = two_room_catalog()
        with self.assertRaises(LimitError):
            Experiment(
                catalog=c,
                initial_concentration={"A": 0, "B": 0},
                source_events=[],
                window_start=0,
                window_end=10001,
                visits=[],
            )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cat = two_room_catalog()
        self.store.save_catalog(self.cat)

    def tearDown(self):
        self.store.close()

    def _import(self, catalog=None):
        return self.store.import_experiment(
            catalog=catalog or self.cat,
            initial_concentration={"A": 0, "B": 0},
            source_events=[{"time": 0, "room": "A", "strength": 1.0}],
            window=[0, 60],
            visits=[
                {"visit_id": "v1", "person": "alice", "room": "A", "start": 0, "end": 40},
                {"visit_id": "v2", "person": "alice", "room": "B", "start": 40, "end": 60},
            ],
        )

    def test_verify_catalog(self):
        report = self.store.verify_catalog(self.cat.identity)
        self.assertTrue(report["ok"])

    def test_run_and_persist(self):
        exp = self._import()
        payload = self.store.run(exp.identity)
        self.assertTrue(payload["mass_balance"]["ok"])
        stored = self.store.get_result(exp.identity)
        self.assertEqual(stored["identity"], exp.identity)

    def test_revision_changes_neighbor_curve(self):
        exp = self._import()
        base = self.store.run(exp.identity)
        revised = self.store.revise_flows(
            self.cat.identity, {("A", "B"): "0.08", ("B", "A"): "0.08"}
        )
        self.store.save_catalog(revised)
        exp2 = self.store.import_experiment(
            catalog=revised,
            initial_concentration={"A": 0, "B": 0},
            source_events=[{"time": 0, "room": "A", "strength": 1.0}],
            window=[0, 60],
            visits=[
                {"visit_id": "v1", "person": "alice", "room": "A", "start": 0, "end": 40},
                {"visit_id": "v2", "person": "alice", "room": "B", "start": 40, "end": 60},
            ],
        )
        new = self.store.run(exp2.identity)
        # B concentration differs
        base_b = base["concentrations"]["B"]
        new_b = new["concentrations"]["B"]
        self.assertGreater(max(abs(x - y) for x, y in zip(base_b, new_b)), 1e-3)
        # visiting B integral changes
        base_v2 = next(v for v in base["visit_integrals"] if v["visit_id"] == "v2")
        new_v2 = next(v for v in new["visit_integrals"] if v["visit_id"] == "v2")
        self.assertNotAlmostEqual(
            base_v2["concentration_integral_mg_s_per_m3"],
            new_v2["concentration_integral_mg_s_per_m3"],
            places=6,
        )
        # old experiment retained old basis
        old = self.store.get_result(exp.identity)
        self.assertEqual(old["catalog_identity"], self.cat.identity)
        self.assertEqual(old["visit_integrals"][0]["basis_catalog_identity"], self.cat.identity)

    def test_inputs_persist_independently(self):
        exp = self._import()
        rows = self.store.conn.execute(
            "SELECT * FROM source_events WHERE experiment_id=?", (exp.identity,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        visits = self.store.conn.execute(
            "SELECT * FROM visits WHERE experiment_id=?", (exp.identity,)
        ).fetchall()
        self.assertEqual(len(visits), 2)
        # load before running works (inputs only)
        reloaded = self.store.load_experiment(exp.identity)
        self.assertEqual(reloaded.identity, exp.identity)


if __name__ == "__main__":
    unittest.main()
