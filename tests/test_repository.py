"""Reservation repository tests: atomicity, identity, persistence, conflicts."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest

from multi_range_reservation.ranges import RangeService, RangeValue, normalize
from multi_range_reservation.repository import (
    OccupancyConflict,
    RequestMismatchError,
    ReservationExists,
    ReservationNotFound,
    ReservationStateError,
    ReservationStore,
    ValidationError,
)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db = os.path.join(self.tmpdir.name, "reservations.sqlite")
        self.store = ReservationStore(self.db)
        self.addCleanup(self._close)
        self._counter = 0

    def _close(self) -> None:
        # Nothing to close: every operation opens and closes its connection.
        pass

    def rid(self, prefix: str = "req") -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def create(self, reservation_id, ranges, *, resource="room", owner="alice", request_id=None):
        return self.store.create(
            request_id or self.rid(), resource, reservation_id, owner, ranges
        )

    def assert_conflicts(self, exc: OccupancyConflict, expected_ids, expected_intersections):
        got_ids = [item["reservation_id"] for item in exc.conflicts]
        self.assertEqual(sorted(got_ids), sorted(expected_ids))
        got = {
            item["reservation_id"]: item["intersection"]["half_open"]
            for item in exc.conflicts
        }
        self.assertEqual(got, expected_intersections)


class CreateAndQueryTests(StoreTestCase):
    def test_create_and_status_persist_after_reopen(self):
        response = self.create("b1", "[1,5)")
        self.assertEqual(response["status"], "success")
        self.assertEqual(response["reservation"]["occupation"]["half_open"], [[1, 5]])

        reopened = ReservationStore(self.db)
        reservation = reopened.get("room", "b1")
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation["status"], "active")
        self.assertEqual(reservation["owner"], "alice")
        self.assertEqual(reservation["occupation"]["half_open"], [[1, 5]])
        self.assertTrue(reopened.verify()["ok"])

    def test_empty_occupation_rejected_without_trace(self):
        for index, empty in enumerate([[], "∅", "[4,4)", "(2,3)", {"half_open": [[9, 9]]}]):
            with self.subTest(empty=empty):
                reservation_id = f"empty-{index}"
                rid = self.rid()
                with self.assertRaises(ValidationError):
                    self.store.create(rid, "room", reservation_id, "alice", empty)
                self.assertIsNone(self.store.get("room", reservation_id))
                # A failed validation did not consume the request identity:
                # using it now for a valid request succeeds.
                response = self.store.create(
                    rid, "room", reservation_id, "alice", "[1,2)"
                )
                self.assertEqual(response["status"], "success")
                with self.assertRaises(ValidationError):
                    self.store.replace(
                        self.rid(), "room", reservation_id, "alice", empty
                    )
                self.assertEqual(
                    self.store.get("room", reservation_id)["occupation"]["half_open"],
                    [[1, 2]],
                )
                self.store.cancel(self.rid(), "room", reservation_id)

    def test_holes_do_not_conflict_and_are_not_filled(self):
        self.create("a", "[1,2)")
        self.create("b", "[3,4)")
        # Point 2 is absent, so [2,3) is free.
        self.assertTrue(self.store.query("room", "[2,3)")["available"])
        self.create("c", "[2,3)")
        self.assertEqual(
            sorted(r["occupation"]["half_open"][0][0] for r in self.store.list_reservations()),
            [1, 2, 3],
        )
        # The union is not [1,4): point 4 and point 0 are outside.
        self.assertFalse(self.store.query("room", "[4,5)")["conflicts"])
        self.assertTrue(self.store.query("room", "[1,4)")["conflict_count"] == 3)

    def test_open_closed_endpoints_equivalent_occupation(self):
        self.create("a", "(1,3]")  # points {2,3}
        with self.assertRaises(OccupancyConflict) as ctx:
            self.create("b", "[3,5)")  # shares point 3
        self.assert_conflicts(ctx.exception, ["a"], {"a": [[3, 4]]})
        self.create("c", "[4,5)")  # only point 4, no overlap
        self.assertTrue(self.store.query("room", "[2,4)")["conflict_count"] == 1)

    def test_different_segmentation_same_set_is_one_reservation(self):
        self.create("a", "[1,2) [3,4)")
        with self.assertRaises(OccupancyConflict):
            self.create("b", "[1,4)")  # point 2 is included here, overlap exists
        self.create("c", "[2,3)")  # exactly the missing point
        with self.assertRaises(OccupancyConflict):
            self.create("d", "[2,3)")

    def test_query_returns_identities_and_actual_intersections(self):
        self.create("a", "[1,5)")
        self.create("b", "[5,9)")
        self.create("c", "[9,12)")
        report = self.store.query("room", "[4,10)")
        self.assertEqual(report["conflict_count"], 3)
        self.assertFalse(report["available"])
        by_id = {c["reservation_id"]: c for c in report["conflicts"]}
        self.assertEqual(by_id["a"]["intersection"]["half_open"], [[4, 5]])
        self.assertEqual(by_id["b"]["intersection"]["half_open"], [[5, 9]])
        self.assertEqual(by_id["c"]["intersection"]["half_open"], [[9, 10]])
        self.assertEqual(by_id["a"]["occupation"]["half_open"], [[1, 5]])
        # Each conflict carries the concrete overlap, not a bare boolean.
        self.assertIn("intersection", by_id["a"])
        self.assertNotIsInstance(by_id["a"]["intersection"]["half_open"][0][0], bool)

    def test_query_ignores_other_resources_and_cancelled(self):
        self.create("a", "[1,3)")
        self.create("b", "[1,5)", resource="other")
        cancelled = self.create("c", "[3,5)")
        self.store.cancel(self.rid(), "room", "c")
        self.assertEqual(cancelled["reservation"]["status"], "active")
        report = self.store.query("room", "[1,5)")
        self.assertEqual([c["reservation_id"] for c in report["conflicts"]], ["a"])
        self.assertTrue(self.store.query("room", "[3,5)")["available"])

    def test_query_accepts_empty_set(self):
        self.create("a", "[1,5)")
        report = self.store.query("room", "∅")
        self.assertTrue(report["available"])
        self.assertEqual(report["conflicts"], [])

    def test_duplicate_reservation_id_rejected(self):
        self.create("a", "[1,2)")
        with self.assertRaises(ReservationExists):
            self.create("a", "[8,9)")

    def test_reservation_id_is_global_across_resources(self):
        self.create("a", "[1,2)", resource="room")
        # The identity is the reservation_id, so the same id on another
        # resource is still a duplicate.
        with self.assertRaises(ReservationExists):
            self.create("a", "[1,2)", resource="other")
        self.assertIsNone(self.store.get("other", "a"))
        self.assertEqual(self.store.get("room", "a")["resource"], "room")

    def test_cancelled_identity_is_retained_and_not_reused(self):
        self.create("a", "[1,2)")
        self.store.cancel(self.rid(), "room", "a")
        # Cancellation releases the time points but keeps the identity, so the
        # same reservation_id cannot be created again.
        with self.assertRaises(ReservationExists):
            self.create("a", "[8,9)")
        self.assertEqual(self.store.get("room", "a")["status"], "cancelled")


class RequestIdentityTests(StoreTestCase):
    def test_replay_returns_exact_original_receipt(self):
        first = self.create("a", "[1,5)", request_id="req-x")
        # Later state changes do not alter the stored receipt.
        self.store.replace(self.rid(), "room", "a", "alice", "[1,3)")
        self.store.cancel(self.rid(), "room", "a")
        replay = self.store.create("req-x", "room", "a", "alice", "[1,5)")
        self.assertEqual(replay, first)
        self.assertEqual(self.store.query("room", "[1,5)")["available"], True)

    def test_equivalent_representation_replays_same_receipt(self):
        first = self.create("a", "[1,4)", request_id="req-x")
        replay = self.store.create(
            "req-x", "room", "a", "alice", "[1,2) [2,3) [3,4)"
        )
        self.assertEqual(replay, first)
        with self.assertRaises(RequestMismatchError):
            self.store.create("req-x", "room", "a", "alice", "[1,5)")
        with self.assertRaises(RequestMismatchError):
            self.store.create("req-x", "room", "a", "bob", "[1,4)")

    def test_request_id_cannot_be_reused_across_actions(self):
        self.create("a", "[1,5)", request_id="req-x")
        with self.assertRaises(RequestMismatchError):
            self.store.cancel("req-x", "room", "a")
        with self.assertRaises(RequestMismatchError):
            self.store.replace("req-x", "room", "a", "alice", "[1,2)")

    def test_failed_request_does_not_consume_identity(self):
        self.create("a", "[1,5)")
        rid = "req-retry"
        with self.assertRaises(OccupancyConflict):
            self.store.create(rid, "room", "b", "bob", "[4,6)")
        self.assertIsNone(self.store.get("room", "b"))
        # Free the conflicting point, then the same request succeeds.
        self.store.cancel(self.rid(), "room", "a")
        response = self.store.create(rid, "room", "b", "bob", "[4,6)")
        self.assertEqual(response["status"], "success")
        # Different payload under the now-committed identity is rejected.
        with self.assertRaises(RequestMismatchError):
            self.store.create(rid, "room", "b", "bob", "[7,8)")
        self.assertTrue(self.store.verify()["ok"])

    def test_failed_replace_leaves_old_row_and_no_receipt(self):
        self.create("a", "[1,5)")
        self.create("b", "[6,9)")
        rid = "req-bad-replace"
        with self.assertRaises(OccupancyConflict):
            self.store.replace(rid, "room", "a", "alice", "[4,8)")
        self.assertEqual(self.store.get("room", "a")["occupation"]["half_open"], [[1, 5]])
        self.assertEqual(
            self.store.get("room", "a")["owner"], "alice"
        )
        # The identity is untouched, so the corrected request can use it.
        response = self.store.replace(rid, "room", "a", "alice", "[4,6)")
        self.assertEqual(response["reservation"]["occupation"]["half_open"], [[4, 6]])
        self.assertTrue(self.store.verify()["ok"])

    def test_unknown_request_errors_do_not_create_receipts(self):
        with self.assertRaises(ReservationNotFound):
            self.store.replace("req-missing", "room", "ghost", "alice", "[1,2)")
        with self.assertRaises(ReservationNotFound):
            self.store.cancel("req-missing-2", "room", "ghost")
        report = self.store.verify()
        self.assertTrue(report["ok"])
        self.assertEqual(report["info"]["request_count"], 0)


class ReplaceTests(StoreTestCase):
    def test_replace_across_own_old_interval(self):
        self.create("a", "[1,5)")
        self.create("b", "[8,12)")
        # Crossing over its own old interval into free space is fine.
        response = self.store.replace(self.rid(), "room", "a", "alice", "[4,8)")
        self.assertEqual(response["reservation"]["occupation"]["half_open"], [[4, 8]])
        # Adjacency is not a conflict.
        response = self.store.replace(self.rid(), "room", "a", "alice", "[4,7)")
        self.assertEqual(response["reservation"]["occupation"]["half_open"], [[4, 7]])
        self.assertTrue(self.store.verify()["ok"])

    def test_replace_checks_final_occupation_not_difference(self):
        self.create("a", "[1,2)")
        self.create("b", "[3,4)")
        # A's own old point 1 is irrelevant; the final set [1,4) hits B.
        with self.assertRaises(OccupancyConflict) as ctx:
            self.store.replace(self.rid(), "room", "a", "alice", "[1,4)")
        self.assert_conflicts(ctx.exception, ["b"], {"b": [[3, 4]]})
        # Final set [2,3) touches neither B nor the old A; only the final
        # occupation is checked, so this must succeed.
        self.store.replace(self.rid(), "room", "a", "alice", "[2,3)")
        ids = {
            c["reservation_id"]
            for c in self.store.query("room", "[1,4)")["conflicts"]
        }
        self.assertEqual(ids, {"a", "b"})
        self.assertEqual(
            [c["reservation_id"] for c in self.store.query("room", "[2,3)")["conflicts"]],
            ["a"],
        )
        # The old occupation was released by the in-place update.
        self.assertEqual(
            self.store.get("room", "a")["occupation"]["half_open"], [[2, 3]]
        )

    def test_replace_equivalent_representation_is_stable(self):
        self.create("a", "[1,3) [5,7)")
        before = self.store.get("room", "a")["occupation"]["half_open"]
        self.assertEqual(before, [[1, 3], [5, 7]])
        response = self.store.replace(self.rid(), "room", "a", "alice", "[1,3) [5,7)")
        self.assertEqual(response["reservation"]["occupation"]["half_open"], before)
        response = self.store.replace(
            self.rid(), "room", "a", "alice", "[1,2) [2,3) [5,6) [6,7)"
        )
        self.assertEqual(response["reservation"]["occupation"]["half_open"], before)
        response = self.store.replace(
            self.rid(), "room", "a", "alice",
            [{"lower": 5, "upper": 6}, {"lower": 1, "upper": 2}],
        )
        self.assertEqual(response["reservation"]["occupation"]["half_open"], before)

    def test_replace_cancelled_reservation_fails(self):
        self.create("a", "[1,5)")
        self.store.cancel(self.rid(), "room", "a")
        with self.assertRaises(ReservationStateError):
            self.store.replace(self.rid(), "room", "a", "alice", "[2,3)")
        self.assertEqual(self.store.get("room", "a")["status"], "cancelled")

    def test_replace_does_not_leave_a_visible_window(self):
        """The conflict check and the UPDATE share one write transaction."""

        store = self.store
        observed: dict[str, object] = {}

        class Probe(ReservationStore):
            def _conflicts(self, conn, resource, candidate, **kwargs):  # type: ignore[override]
                result = super()._conflicts(conn, resource, candidate, **kwargs)
                # We are inside BEGIN IMMEDIATE, after the final-occupation
                # check and before the UPDATE.  A second writer must not be
                # able to begin a transaction here.
                probe = sqlite3.connect(self.path, timeout=0, isolation_level=None)
                try:
                    try:
                        probe.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as exc:
                        observed["blocked"] = str(exc)
                    else:
                        observed["blocked"] = None
                        probe.execute("ROLLBACK")
                    observed["old_row_visible_to_probe"] = probe.execute(
                        "SELECT ranges_json, status FROM reservations "
                        "WHERE resource='room' AND reservation_id='a'"
                    ).fetchone()
                finally:
                    probe.close()
                return result

        self.create("a", "[1,5)")
        probe_store = Probe(self.db)
        probe_store.replace(self.rid(), "room", "a", "alice", "[2,8)")
        self.assertIn("locked", str(observed["blocked"]).lower())
        self.assertFalse(observed["old_row_visible_to_probe"] is None)
        self.assertEqual(
            self.store.get("room", "a")["occupation"]["half_open"], [[2, 8]]
        )
        self.assertTrue(self.store.verify()["ok"])


class CancelAndReuseTests(StoreTestCase):
    def test_cancel_releases_every_point_and_keeps_identity(self):
        self.create("a", "[1,5)")
        self.create("b", "[6,8)")
        self.store.cancel(self.rid(), "room", "a")
        row = self.store.get("room", "a")
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["owner"], "alice")
        self.assertEqual(row["occupation"]["half_open"], [[1, 5]])
        self.assertTrue(self.store.query("room", "[1,5)")["available"])
        # The freed range can be used by a new reservation.
        self.create("c", "[1,5)")
        with self.assertRaises(OccupancyConflict):
            self.create("d", "[4,7)")  # conflicts with c and b
        self.assertTrue(self.store.verify()["ok"])

    def test_cancel_is_persistent_and_second_cancel_is_safe(self):
        self.create("a", "[1,5)")
        first = self.store.cancel("cancel-1", "room", "a")
        second = self.store.cancel("cancel-2", "room", "a")
        self.assertEqual(first["reservation"]["status"], "cancelled")
        self.assertEqual(second["reservation"]["status"], "cancelled")
        # Replay still returns the original receipt.
        self.assertEqual(self.store.cancel("cancel-1", "room", "a"), first)
        reopened = ReservationStore(self.db)
        self.assertEqual(reopened.get("room", "a")["status"], "cancelled")
        self.assertTrue(reopened.verify()["ok"])

    def test_cancel_unknown_reservation(self):
        with self.assertRaises(ReservationNotFound):
            self.store.cancel(self.rid(), "room", "ghost")


class SemanticDependencyTests(StoreTestCase):
    def test_repository_uses_equivalent_injected_range_service(self):
        class SplittingService(RangeService):
            """Same set semantics via singleton expansion."""

            def normalize(self, value=()):
                value = super().normalize(value)
                singles = [
                    {"lower": p, "upper": p}
                    for lo, hi in value.pairs
                    for p in range(lo, hi)
                ]
                return super().normalize(singles)

        store = ReservationStore(os.path.join(self.tmpdir.name, "svc.sqlite"), range_service=SplittingService())
        store.create("r1", "room", "a", "alice", "[1,4)")
        with self.assertRaises(OccupancyConflict):
            store.create("r2", "room", "b", "bob", "[3,6)")
        store.create("r3", "room", "c", "carol", "[6,8)")
        self.assertTrue(store.verify()["ok"])
        # The default service reads the same canonical semantics back.
        self.assertEqual(store.query("room", "[2,4)")["conflict_count"], 1)

    def test_changing_normalization_changes_reservation_judgement(self):
        """A deliberately wrong endpoint rule must really change outcomes."""

        class UpperInclusiveBug(RangeService):
            """Incorrectly treats the upper endpoint as inclusive."""

            def normalize(self, value=()):
                half_open = super().normalize(value).as_pairs()
                buggy = [[lo, hi + 1] for lo, hi in half_open]
                return RangeValue({"half_open": buggy})

        correct_db = os.path.join(self.tmpdir.name, "correct.sqlite")
        buggy_db = os.path.join(self.tmpdir.name, "buggy.sqlite")
        correct = ReservationStore(correct_db)
        buggy = ReservationStore(buggy_db, range_service=UpperInclusiveBug())

        for store, label in ((correct, "correct"), (buggy, "buggy")):
            store.create(f"{label}-r1", "room", "a", "alice", "[1,2)")
            try:
                store.create(f"{label}-r2", "room", "b", "bob", "[2,3)")
            except OccupancyConflict:
                outcome = "rejected"
            else:
                outcome = "accepted"
            self.assertEqual(
                outcome,
                "accepted" if label == "correct" else "rejected",
                f"{label} service produced {outcome}",
            )

    def test_dropped_hole_bug_changes_reservation_outcome(self):
        """A service that fills holes wrongly changes accept/reject decisions."""

        class HoleFillingBug(RangeService):
            """Incorrectly merges over gaps (fills missing integer points)."""

            def normalize(self, value=()):
                base = super().normalize(value).as_pairs()
                if not base:
                    return RangeValue("∅")
                return RangeValue(
                    {"half_open": [[base[0][0], base[-1][1]]]}
                )

        correct = ReservationStore(os.path.join(self.tmpdir.name, "holes-correct.sqlite"))
        buggy = ReservationStore(
            os.path.join(self.tmpdir.name, "holes-buggy.sqlite"),
            range_service=HoleFillingBug(),
        )
        for store, label in ((correct, "correct"), (buggy, "buggy")):
            store.create(f"{label}-a", "room", "a", "alice", "[1,2) [3,4)")
            try:
                store.create(f"{label}-b", "room", "b", "bob", "[2,3)")
            except OccupancyConflict:
                outcome = "rejected"
            else:
                outcome = "accepted"
            # The hole is point 2, so it is free for the correct service.  The
            # buggy service silently occupies it, so it wrongly rejects.
            self.assertEqual(
                outcome, "accepted" if label == "correct" else "rejected",
                f"{label} service produced {outcome}",
            )
            # And the occupied set itself differs, proving the repository is
            # consuming produced semantics, not raw input segmentation.
            occupation = store.get("room", "a")["occupation"]["half_open"]
            self.assertEqual(
                occupation,
                [[1, 2], [3, 4]] if label == "correct" else [[1, 4]],
            )

    def test_dropped_last_point_bug_wrongly_allows_overlap(self):
        """A service that loses a point can wrongly accept a conflicting row."""

        class DropUpperPointBug(RangeService):
            """Incorrectly drops the last integer when parsing user segments.

            It is idempotent on already-stored half-open data, which is the
            contract the repository relies on for reads.
            """

            def normalize(self, value=()):
                if isinstance(value, dict) and "half_open" in value:
                    return super().normalize(value)
                base = super().normalize(value).as_pairs()
                shrunk = [[lo, hi - 1] for lo, hi in base if lo < hi - 1]
                return RangeValue({"half_open": shrunk})

        correct = ReservationStore(os.path.join(self.tmpdir.name, "drop-correct.sqlite"))
        buggy = ReservationStore(
            os.path.join(self.tmpdir.name, "drop-buggy.sqlite"),
            range_service=DropUpperPointBug(),
        )
        for store, label in ((correct, "correct"), (buggy, "buggy")):
            # [1,3] occupies points 1, 2 and 3.
            store.create(f"{label}-a", "room", "a", "alice", "[1,3]")
            try:
                # Point 3 collides.  The buggy service forgot it, and its
                # candidate [3,5) still has point 4, so it is not empty.
                store.create(f"{label}-b", "room", "b", "bob", "[3,5)")
            except OccupancyConflict:
                outcome = "rejected"
            else:
                outcome = "accepted"
            self.assertEqual(
                outcome, "rejected" if label == "correct" else "accepted",
                f"{label} service produced {outcome}",
            )
        buggy_report = buggy.verify()
        self.assertTrue(buggy_report["ok"], buggy_report["errors"])

    def test_algebra_is_usable_without_repository(self):
        # No database, no request identity: pure functions only.
        self.assertEqual(normalize("[1,2) [3,4)").as_pairs(), [[1, 2], [3, 4]])
        self.assertFalse(normalize("[1,2)").intersects("[2,3)"))
        self.assertTrue(normalize("[1,3)").intersects("[2,3)"))
        self.assertEqual(normalize("[1,5)").difference("[2,3)").as_pairs(), [[1, 2], [3, 5]])


class ConcurrencyTests(StoreTestCase):
    def test_two_connections_race_for_last_point(self):
        self.create("base", "[0,99)")
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            store = ReservationStore(self.db)
            barrier.wait()
            try:
                response = store.create(
                    f"race-{index}", "room", f"contender-{index}", f"user-{index}", "[99,100)"
                )
            except OccupancyConflict as exc:
                with lock:
                    outcomes.append(("conflict", exc))
            except Exception as exc:  # pragma: no cover - unexpected
                with lock:
                    outcomes.append(("error", exc))
            else:
                with lock:
                    outcomes.append(("success", response))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(outcomes), 2, outcomes)
        kinds = sorted(kind for kind, _ in outcomes)
        self.assertEqual(kinds, ["conflict", "success"], outcomes)
        winner = next(payload for kind, payload in outcomes if kind == "success")
        loser = next(payload for kind, payload in outcomes if kind == "conflict")
        self.assertIsInstance(winner, dict)
        self.assertIsInstance(loser, OccupancyConflict)
        active = [
            r for r in self.store.list_reservations(resource="room", status="active")
            if r["occupation"]["half_open"] == [[99, 100]]
        ]
        self.assertEqual(len(active), 1)
        self.assertTrue(self.store.verify()["ok"])

    def test_race_with_separate_processes(self):
        import subprocess
        import sys

        self.create("base", "[0,99)")
        gate_dir = os.path.join(self.tmpdir.name, "gates")
        os.makedirs(gate_dir)
        script = r"""
import json, os, sys, time
from multi_range_reservation import ReservationStore, OccupancyConflict
db, rid, rsv, owner, gate_dir = sys.argv[1:]
open(os.path.join(gate_dir, rid), 'w').close()
deadline = time.time() + 30
while len(os.listdir(gate_dir)) < 2 and time.time() < deadline:
    time.sleep(0.002)
store = ReservationStore(db)
try:
    store.create(rid, 'room', rsv, owner, '[99,100)')
except OccupancyConflict:
    print(json.dumps({'outcome': 'conflict', 'id': rsv}))
else:
    print(json.dumps({'outcome': 'success', 'id': rsv}))
"""
        procs = []
        for i in range(2):
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, self.db, f"proc-{i}", f"p{i}", f"u{i}", gate_dir],
                    cwd=os.getcwd(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            outputs.append(json.loads(out))
        self.assertEqual(sorted(o["outcome"] for o in outputs), ["conflict", "success"])
        self.assertTrue(self.store.verify()["ok"])


class VerifyTests(StoreTestCase):
    def test_verify_detects_overlapping_active_rows(self):
        self.create("a", "[1,5)")
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO reservations (resource, reservation_id, owner, ranges_json, status, seq) "
                "VALUES ('room', 'tampered', 'mallory', '[[3,7]]', 'active', 999)"
            )
            conn.commit()
        finally:
            conn.close()
        report = self.store.verify()
        self.assertFalse(report["ok"])
        self.assertTrue(any("overlap" in error for error in report["errors"]))
        self.assertTrue(any("tampered" in error for error in report["errors"]))

    def test_verify_detects_corrupt_metadata_and_receipt(self):
        self.create("a", "[1,5)")
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE metadata SET value = '0' WHERE key = 'next_seq'")
            conn.execute("UPDATE requests SET fingerprint = 'deadbeef'")
            conn.commit()
        finally:
            conn.close()
        report = self.store.verify()
        self.assertFalse(report["ok"])
        joined = "\n".join(report["errors"])
        self.assertIn("next_seq", joined)
        self.assertIn("fingerprint", joined)

    def test_verify_reports_equivalent_rows_without_failing(self):
        self.create("a", "[1,5)")
        # Simulate a row written by another equivalent representation.
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "UPDATE reservations SET ranges_json = '[[1,2],[2,5]]' "
                "WHERE reservation_id = 'a'"
            )
            conn.commit()
        finally:
            conn.close()
        report = self.store.verify()
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["info"]["normalized_equivalent_rows"], 1)
        # And the repository still sees the correct point set.
        with self.assertRaises(OccupancyConflict):
            self.create("b", "[4,6)")


if __name__ == "__main__":
    unittest.main()
