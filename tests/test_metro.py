"""Unit tests for the explainable metro-flow warehouse."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from metro import analysis, catalog, db, gate, queries


def make_conn(path=":memory:"):
    conn = db.connect(path)
    db.init_schema(conn)
    return conn


def stations():
    return [
        {"station": "A", "name": "A", "entry_walk": 60, "exit_walk": 60,
         "min_transfer": 120},
        {"station": "B", "name": "B", "entry_walk": 60, "exit_walk": 60,
         "min_transfer": 120},
        {"station": "C", "name": "C", "entry_walk": 60, "exit_walk": 60,
         "min_transfer": 120},
        {"station": "D", "name": "D", "entry_walk": 60, "exit_walk": 60,
         "min_transfer": 120},
    ]


def stops_train(train_id, spec, start=1000, dwell=20, run=200):
    """spec: list of (station,). Runs arrive/depart with dwell."""
    rows = []
    t = start
    for seq, station in enumerate(spec, start=1):
        rows.append({"train_id": train_id, "seq": seq, "station": station,
                     "arr_ts": t, "dep_ts": t + dwell})
        t += dwell + run
    return rows


class StationTests(unittest.TestCase):
    def test_upsert_and_negative_rejected(self):
        conn = make_conn()
        self.assertEqual(catalog.upsert_stations(conn, stations()), 4)
        with self.assertRaises(catalog.TimetableError):
            catalog.upsert_stations(conn, [
                {"station": "X", "entry_walk": -1, "exit_walk": 0, "min_transfer": 0}
            ])

    def test_unknown_station(self):
        conn = make_conn()
        catalog.upsert_stations(conn, stations())
        with self.assertRaises(catalog.TimetableError):
            catalog.import_timetable(conn, stops_train("T1", ["A", "Z"]), "bad")


class TimetableTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        catalog.upsert_stations(self.conn, stations())

    def test_arrival_after_departure_rejected(self):
        rows = [
            {"train_id": "T1", "seq": 1, "station": "A", "arr_ts": 100, "dep_ts": 90},
        ]
        with self.assertRaises(catalog.TimetableError):
            catalog.import_timetable(self.conn, rows, "bad")

    def test_inter_stop_overlap_rejected(self):
        rows = [
            {"train_id": "T1", "seq": 1, "station": "A", "arr_ts": 100, "dep_ts": 200},
            {"train_id": "T1", "seq": 2, "station": "B", "arr_ts": 150, "dep_ts": 300},
        ]
        with self.assertRaises(catalog.TimetableError):
            catalog.import_timetable(self.conn, rows, "bad")

    def test_duplicate_stop_identity_rejected(self):
        rows = [
            {"train_id": "T1", "seq": 1, "station": "A", "arr_ts": 100, "dep_ts": 120},
            {"train_id": "T1", "seq": 1, "station": "B", "arr_ts": 300, "dep_ts": 320},
        ]
        with self.assertRaises(catalog.TimetableError):
            catalog.import_timetable(self.conn, rows, "bad")

    def test_non_contiguous_seq_rejected(self):
        rows = [
            {"train_id": "T1", "seq": 1, "station": "A", "arr_ts": 100, "dep_ts": 120},
            {"train_id": "T1", "seq": 3, "station": "B", "arr_ts": 300, "dep_ts": 320},
        ]
        with self.assertRaises(catalog.TimetableError):
            catalog.import_timetable(self.conn, rows, "bad")

    def test_loop_repeated_station_allowed(self):
        rows = stops_train("L", ["A", "B", "C", "A"])
        sid = catalog.import_timetable(self.conn, rows, "loop")
        snap = catalog.load_snapshot(self.conn, sid)
        self.assertEqual([s.station for s in snap.trains["L"]], ["A", "B", "C", "A"])
        self.assertEqual([s.seq for s in snap.trains["L"]], [1, 2, 3, 4])

    def test_station_times_without_gates(self):
        rows = stops_train("T1", ["A", "B"]) + stops_train("T2", ["B", "A"], start=1500)
        sid = catalog.import_timetable(self.conn, rows, "v1")
        at_b = catalog.station_times(self.conn, sid, "B")
        self.assertEqual(len(at_b), 2)
        self.assertEqual(at_b[0]["train_id"], "T1")

    def test_correction_rejected_on_conflict(self):
        sid = catalog.import_timetable(self.conn, stops_train("T1", ["A", "B"]), "v1")
        with self.assertRaises(catalog.TimetableError):
            catalog.correct_train_times(
                self.conn, sid, "T1",
                [{"seq": 1, "dep_ts": 5000}], "bad",
            )
        # base snapshot unchanged
        self.assertEqual(len(catalog.train_stops(self.conn, sid, "T1")), 2)

    def test_correction_creates_new_snapshot(self):
        sid = catalog.import_timetable(self.conn, stops_train("T1", ["A", "B"]), "v1")
        new = catalog.correct_train_times(
            self.conn, sid, "T1",
            [{"seq": 1, "arr_ts": 3000, "dep_ts": 3020},
             {"seq": 2, "arr_ts": 3200, "dep_ts": 3220}],
            "v2",
        )
        self.assertNotEqual(sid, new)
        self.assertEqual(catalog.train_stops(self.conn, sid, "T1")[0]["arr_ts"], 1000)
        self.assertEqual(catalog.train_stops(self.conn, new, "T1")[0]["arr_ts"], 3000)


class GatePairingTests(unittest.TestCase):
    def _events(self, rows):
        ev = []
        for i, (card, station, ts, direction) in enumerate(rows):
            ev.append({"event_id": f"E{i}", "card_id": card, "station": station,
                       "ts": ts, "direction": direction})
        return ev

    def _conn(self):
        conn = make_conn()
        catalog.upsert_stations(conn, stations())
        return conn

    def test_basic_pairing(self):
        conn = self._conn()
        gate.store_import(conn, self._events([
            ("c1", "A", 100, "IN"), ("c1", "B", 500, "OUT"),
        ]), "g")
        trips, unclosed, orphans = gate.pairing_for_import(conn, 1)
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0].entry_station, "A")
        self.assertEqual(trips[0].exit_station, "B")

    def test_stale_entry_superseded(self):
        conn = self._conn()
        gate.store_import(conn, self._events([
            ("c1", "A", 100, "IN"), ("c1", "B", 300, "IN"),
            ("c1", "C", 900, "OUT"),
        ]), "g")
        trips, unclosed, orphans = gate.pairing_for_import(conn, 1)
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0].entry_station, "B")
        self.assertEqual([u["event_id"] for u in unclosed], ["E0"])

    def test_orphan_exit_and_missing_exit(self):
        conn = self._conn()
        gate.store_import(conn, self._events([
            ("c1", "A", 100, "OUT"),
            ("c2", "B", 200, "IN"),
        ]), "g")
        trips, unclosed, orphans = gate.pairing_for_import(conn, 1)
        self.assertEqual(len(trips), 0)
        self.assertEqual(len(unclosed), 1)
        self.assertEqual(len(orphans), 1)

    def test_same_timestamp_event_id_order(self):
        conn = self._conn()
        # same ts: event_id ordering decides; E1 is IN, E0 is OUT -> orphan + open
        rows = [
            {"event_id": "E1", "card_id": "c1", "station": "A", "ts": 500, "direction": "IN"},
            {"event_id": "E0", "card_id": "c1", "station": "B", "ts": 500, "direction": "OUT"},
        ]
        gate.store_import(conn, rows, "g")
        trips, unclosed, orphans = gate.pairing_for_import(conn, 1)
        self.assertEqual(len(trips), 0)
        self.assertEqual(len(orphans), 1)
        self.assertEqual(len(unclosed), 1)

    def test_identical_duplicate_dedup(self):
        conn = self._conn()
        rows = [
            {"event_id": "E1", "card_id": "c1", "station": "A", "ts": 100, "direction": "IN"},
            {"event_id": "E1", "card_id": "c1", "station": "A", "ts": 100, "direction": "IN"},
            {"event_id": "E2", "card_id": "c1", "station": "B", "ts": 500, "direction": "OUT"},
        ]
        summary = gate.store_import(conn, rows, "g")
        self.assertEqual(summary["duplicate_rows"], 1)
        self.assertEqual(summary["unique_events"], 2)

    def test_conflicting_duplicate_rejects_batch(self):
        conn = self._conn()
        rows = [
            {"event_id": "E1", "card_id": "c1", "station": "A", "ts": 100, "direction": "IN"},
            {"event_id": "E1", "card_id": "c1", "station": "B", "ts": 100, "direction": "IN"},
        ]
        with self.assertRaises(gate.GateDataError):
            gate.store_import(conn, rows, "g")
        # nothing written
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM gate_events").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM gate_imports").fetchone()[0], 0)

    def test_unknown_station_rejected(self):
        conn = self._conn()
        with self.assertRaises(gate.GateDataError):
            gate.store_import(conn, self._events([("c1", "Z", 100, "IN")]), "g")


class ExplanationTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        catalog.upsert_stations(self.conn, stations())

    def run_case(self, timetable, events, name="v1"):
        sid = catalog.import_timetable(self.conn, timetable, name)
        summary = gate.store_import(self.conn, events, "g")
        bid = analysis.import_gate_and_analyze(
            self.conn, sid, summary["import_id"], "run"
        )
        return sid, bid

    def test_entry_walk_and_exit_walk_enforced(self):
        # train departs A at 1000; entry at 900 ready=960 -> feasible
        timetable = stops_train("T1", ["A", "B"], start=1000)
        ev = [{"event_id": "E1", "card_id": "c", "station": "A", "ts": 900, "direction": "IN"},
              {"event_id": "E2", "card_id": "c", "station": "B", "ts": 2000, "direction": "OUT"}]
        _, bid = self.run_case(timetable, ev)
        trips = queries.list_trips(self.conn, bid)
        self.assertEqual(trips[0]["status"], "EXPLAINABLE")

        # entry too late: 970 ready=1030 > dep 1020
        ev2 = [{"event_id": "E1", "card_id": "c", "station": "A", "ts": 970, "direction": "IN"},
               {"event_id": "E2", "card_id": "c", "station": "B", "ts": 2000, "direction": "OUT"}]
        sid2, bid2 = self.run_case(timetable, ev2, name="v2")
        trips2 = queries.list_trips(self.conn, bid2)
        self.assertEqual(trips2[0]["status"], "UNEXPLAINABLE")

    def test_transfer_min_time_enforced(self):
        # T1 A->B arrives 1230; T2 B->C dep 1300 (gap 70 < 120) -> invalid
        # a later T2 at 1500 makes it valid
        timetable = (
            stops_train("T1", ["A", "B"], start=1000)
            + stops_train("T2", ["B", "C"], start=1300)
            + stops_train("T3", ["B", "C"], start=1500)
        )
        ev = [{"event_id": "E1", "card_id": "c", "station": "A", "ts": 900, "direction": "IN"},
              {"event_id": "E2", "card_id": "c", "station": "C", "ts": 3000, "direction": "OUT"}]
        _, bid = self.run_case(timetable, ev)
        plans = queries.load_plans(self.conn, bid, queries.list_trips(self.conn, bid)[0]["trip_id"])
        second_trains = {p["legs"][1]["train"] for p in plans if p["kind"] == "TRANSFER"}
        self.assertIn("T3", second_trains)
        self.assertNotIn("T2", second_trains)

    def test_two_transfers_not_allowed(self):
        # A->B on T1, B->C on T2, C->D on T3 would need two transfers
        timetable = (
            stops_train("T1", ["A", "B"], start=1000)
            + stops_train("T2", ["B", "C"], start=1500)
            + stops_train("T3", ["C", "D"], start=2000)
        )
        ev = [{"event_id": "E1", "card_id": "c", "station": "A", "ts": 900, "direction": "IN"},
              {"event_id": "E2", "card_id": "c", "station": "D", "ts": 4000, "direction": "OUT"}]
        _, bid = self.run_case(timetable, ev)
        trip = queries.list_trips(self.conn, bid)[0]
        self.assertEqual(trip["status"], "UNEXPLAINABLE")

    def test_same_train_transfer_rejected(self):
        # a single train repeating A->B->A->B could look like a "transfer" to itself
        timetable = stops_train("L", ["A", "B", "A", "B"], start=1000)
        ev = [{"event_id": "E1", "card_id": "c", "station": "A", "ts": 900, "direction": "IN"},
              {"event_id": "E2", "card_id": "c", "station": "B", "ts": 5000, "direction": "OUT"}]
        _, bid = self.run_case(timetable, ev)
        plans = queries.load_plans(
            self.conn, bid, queries.list_trips(self.conn, bid)[0]["trip_id"]
        )
        for plan in plans:
            if plan["kind"] == "TRANSFER":
                self.assertNotEqual(plan["legs"][0]["train"], plan["legs"][1]["train"])

    def test_plan_dedup_by_sequence(self):
        # Two identical trains serve A->B; each is a distinct plan, but within
        # one train the same board/alight sequence appears once.
        timetable = stops_train("T1", ["A", "B"], start=1000)
        ev = [{"event_id": "E1", "card_id": "c", "station": "A", "ts": 900, "direction": "IN"},
              {"event_id": "E2", "card_id": "c", "station": "B", "ts": 3000, "direction": "OUT"}]
        _, bid = self.run_case(timetable, ev)
        plans = queries.load_plans(
            self.conn, bid, queries.list_trips(self.conn, bid)[0]["trip_id"]
        )
        sigs = [(l["train"], l["board_seq"], l["alight_seq"]) for l in
                [p["legs"][0] for p in plans]]
        self.assertEqual(len(sigs), len(set(sigs)))

    def test_loop_plan_uses_repeated_station(self):
        timetable = stops_train("L", ["A", "B", "A"], start=1000)
        ev = [{"event_id": "E1", "card_id": "c", "station": "A", "ts": 900, "direction": "IN"},
              {"event_id": "E2", "card_id": "c", "station": "A", "ts": 3000, "direction": "OUT"}]
        _, bid = self.run_case(timetable, ev)
        plans = queries.load_plans(
            self.conn, bid, queries.list_trips(self.conn, bid)[0]["trip_id"]
        )
        self.assertTrue(any(
            p["legs"][0]["board_seq"] == 1 and p["legs"][0]["alight_seq"] == 3
            for p in plans
        ))

    def test_bounds_and_witness_replay(self):
        timetable = stops_train("T1", ["A", "B", "C"], start=1000)
        ev = [{"event_id": "E1", "card_id": "c", "station": "A", "ts": 900, "direction": "IN"},
              {"event_id": "E2", "card_id": "c", "station": "C", "ts": 3000, "direction": "OUT"}]
        _, bid = self.run_case(timetable, ev)
        flows = queries.query_interval(self.conn, bid, "T1")
        self.assertEqual(len(flows), 2)
        for f in flows:
            for bound in ("MIN", "MAX"):
                w = queries.replay_witness(
                    self.conn, bid, "T1", f["from_seq"], bound
                )
                self.assertTrue(w["ok"], w)
        self.assertTrue(queries.audit_batch(self.conn, bid)["ok"])

    def test_old_batch_keeps_old_snapshot_after_correction(self):
        timetable = stops_train("T1", ["A", "B", "C"], start=1000)
        ev = [{"event_id": "E1", "card_id": "c", "station": "A", "ts": 900, "direction": "IN"},
              {"event_id": "E2", "card_id": "c", "station": "C", "ts": 3000, "direction": "OUT"}]
        sid, bid1 = self.run_case(timetable, ev)
        new_sid = catalog.correct_train_times(
            self.conn, sid, "T1",
            [{"seq": 1, "arr_ts": 4000, "dep_ts": 4020},
             {"seq": 2, "arr_ts": 4200, "dep_ts": 4220},
             {"seq": 3, "arr_ts": 4400, "dep_ts": 4420}],
            "v2",
        )
        bid2 = analysis.import_gate_and_analyze(self.conn, new_sid, 1, "run2")
        old = queries.list_trips(self.conn, bid1)
        new = queries.list_trips(self.conn, bid2)
        self.assertEqual(old[0]["status"], "EXPLAINABLE")
        self.assertEqual(new[0]["status"], "UNEXPLAINABLE")
        # old batch still bound to old snapshot
        self.assertEqual(
            queries.batch_summary(self.conn, bid1)["snapshot_id"], sid
        )
        self.assertEqual(
            queries.batch_summary(self.conn, bid2)["snapshot_id"], new_sid
        )

    def test_unexplainable_not_dropped(self):
        timetable = stops_train("T1", ["A", "B"], start=1000)
        ev = [{"event_id": "E1", "card_id": "c", "station": "A", "ts": 9000, "direction": "IN"},
              {"event_id": "E2", "card_id": "c", "station": "B", "ts": 10000, "direction": "OUT"}]
        _, bid = self.run_case(timetable, ev)
        trips = queries.list_trips(self.conn, bid)
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["status"], "UNEXPLAINABLE")
        self.assertEqual(queries.batch_summary(self.conn, bid)["n_unexplainable"], 1)


class PersistenceTests(unittest.TestCase):
    def test_reopen_queries(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "m.db")
            conn = make_conn(path)
            catalog.upsert_stations(conn, stations())
            sid = catalog.import_timetable(conn, stops_train("T1", ["A", "B"]), "v1")
            gate.store_import(conn, [
                {"event_id": "E1", "card_id": "c", "station": "A", "ts": 900, "direction": "IN"},
                {"event_id": "E2", "card_id": "c", "station": "B", "ts": 2000, "direction": "OUT"},
            ], "g")
            bid = analysis.import_gate_and_analyze(conn, sid, 1, "run")
            conn.close()

            conn2 = db.connect(path)
            trips = queries.list_trips(conn2, bid)
            self.assertEqual(len(trips), 1)
            expl = queries.explain_trip(conn2, bid, trips[0]["trip_id"])
            self.assertTrue(expl["plans"])
            self.assertEqual(queries.audit_batch(conn2, bid)["ok"], True)
            conn2.close()


if __name__ == "__main__":
    unittest.main()
