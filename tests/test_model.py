"""Model-based randomized tests cross-checked by brute-force enumeration.

The model below is intentionally naive: it stores literal Python sets of
integers.  Every repository decision (accept/conflict, query intersections,
status after cancel, receipt replay/mismatch) is compared against the model,
and every failed operation must leave both reservation rows and request
receipts byte-for-byte unchanged.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import tempfile
import unittest

from multi_range_reservation.repository import (
    OccupancyConflict,
    RequestMismatchError,
    ReservationExists,
    ReservationNotFound,
    ReservationStateError,
    ReservationStore,
    ValidationError,
)
from multi_range_reservation.repository import _canonical_payload

RESOURCES = ("res-a", "res-b")


def materialize(segments) -> frozenset[int]:
    """Enumerate points for a list of {lower, upper, flags} segments."""

    points: set[int] = set()
    for seg in segments:
        lo = seg["lower"]
        hi = seg["upper"]
        lo_closed = seg.get("lower_inclusive", True)
        hi_closed = seg.get("upper_inclusive", True)
        start = lo if lo_closed else lo + 1
        end = hi if hi_closed else hi - 1
        if start <= end:
            points.update(range(start, end + 1))
    return frozenset(points)


def render_text(pairs: list[list[int]]) -> str:
    if not pairs:
        return "∅"
    return " ∪ ".join(f"[{lo}, {hi})" for lo, hi in pairs)


def canonical_point_pairs(points: frozenset[int]) -> list[list[int]]:
    ordered = sorted(points)
    pairs: list[list[int]] = []
    for point in ordered:
        if pairs and point == pairs[-1][1]:
            pairs[-1][1] = point + 1
        else:
            pairs.append([point, point + 1])
    return pairs


def random_segments(rng: random.Random, max_segments: int = 3):
    segments = []
    for _ in range(rng.randint(1, max_segments)):
        lo = rng.randint(-1, 12)
        hi = rng.randint(-1, 12)
        segments.append(
            {
                "lower": lo,
                "upper": hi,
                "lower_inclusive": rng.random() < 0.5,
                "upper_inclusive": rng.random() < 0.5,
            }
        )
    return segments


def equivalent_representation(rng: random.Random, points: frozenset[int]):
    """Another representation of exactly the same point set."""

    ordered = sorted(points)
    if not ordered:
        return []
    # Split runs into singleton/paired segments and shuffle the order.
    segments = []
    index = 0
    while index < len(ordered):
        run_end = index
        while run_end + 1 < len(ordered) and ordered[run_end + 1] == ordered[run_end] + 1:
            run_end += 1
        run = ordered[index : run_end + 1]
        cut = rng.randint(1, len(run))
        for part in (run[:cut], run[cut:]):
            if not part:
                continue
            if rng.random() < 0.5:
                segments.append({"lower": part[0], "upper": part[-1]})
            else:
                segments.append(
                    {
                        "lower": part[0] - 1,
                        "upper": part[-1] + 1,
                        "lower_inclusive": False,
                        "upper_inclusive": False,
                    }
                )
        index = run_end + 1
    rng.shuffle(segments)
    return segments


class Model:
    def __init__(self) -> None:
        self.reservations: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.order = 0

    def active(self, resource: str) -> dict[str, frozenset[int]]:
        return {
            rid: entry["points"]
            for rid, entry in self.reservations.items()
            if entry["resource"] == resource and entry["status"] == "active"
        }

    def lookup_receipt(self, request_id, action, payload):
        if request_id not in self.receipts:
            return None
        action_seen, payload_seen, response = self.receipts[request_id]
        if action_seen != action or payload_seen != payload:
            raise RequestMismatchError(request_id, action)
        return response

    def create(self, request_id, resource, reservation_id, owner, segments):
        points = materialize(segments)
        if not points:
            raise ValidationError("empty")
        payload = {
            "action": "create",
            "request_id": request_id,
            "resource": resource,
            "reservation_id": reservation_id,
            "owner": owner,
            "ranges": canonical_point_pairs(points),
        }
        replay = self.lookup_receipt(request_id, "create", payload)
        if replay is not None:
            return replay
        if reservation_id in self.reservations:
            raise ReservationExists(resource, reservation_id)
        conflicts = {
            rid: sorted(points & occ) for rid, occ in self.active(resource).items()
            if points & occ
        }
        if conflicts:
            raise OccupancyConflict(resource, self._conflict_rows(resource, conflicts))
        self.order += 1
        entry = {
            "resource": resource,
            "reservation_id": reservation_id,
            "owner": owner,
            "points": points,
            "status": "active",
            "seq": self.order,
        }
        self.reservations[reservation_id] = entry
        response = {
            "request_id": request_id,
            "action": "create",
            "status": "success",
            "reservation": self._row(entry),
        }
        self.receipts[request_id] = ("create", payload, response)
        return response

    def replace(self, request_id, resource, reservation_id, owner, segments):
        points = materialize(segments)
        if not points:
            raise ValidationError("empty")
        payload = {
            "action": "replace",
            "request_id": request_id,
            "resource": resource,
            "reservation_id": reservation_id,
            "owner": owner,
            "ranges": canonical_point_pairs(points),
        }
        replay = self.lookup_receipt(request_id, "replace", payload)
        if replay is not None:
            return replay
        entry = self.reservations.get(reservation_id)
        if entry is None or entry["resource"] != resource:
            raise ReservationNotFound(resource, reservation_id)
        if entry["status"] != "active":
            raise ReservationStateError("not active")
        conflicts = {
            rid: sorted(points & occ)
            for rid, occ in self.active(resource).items()
            if rid != reservation_id and points & occ
        }
        if conflicts:
            raise OccupancyConflict(resource, self._conflict_rows(resource, conflicts))
        entry["owner"] = owner
        entry["points"] = points
        response = {
            "request_id": request_id,
            "action": "replace",
            "status": "success",
            "reservation": self._row(entry),
        }
        self.receipts[request_id] = ("replace", payload, response)
        return response

    def cancel(self, request_id, resource, reservation_id):
        payload = {
            "action": "cancel",
            "request_id": request_id,
            "resource": resource,
            "reservation_id": reservation_id,
        }
        replay = self.lookup_receipt(request_id, "cancel", payload)
        if replay is not None:
            return replay
        entry = self.reservations.get(reservation_id)
        if entry is None or entry["resource"] != resource:
            raise ReservationNotFound(resource, reservation_id)
        entry["status"] = "cancelled"
        response = {
            "request_id": request_id,
            "action": "cancel",
            "status": "success",
            "reservation": self._row(entry),
        }
        self.receipts[request_id] = ("cancel", payload, response)
        return response

    def query(self, resource, points):
        conflicts = []
        for rid, occ in sorted(self.active(resource).items(), key=lambda kv: self.reservations[kv[0]]["seq"]):
            overlap = points & occ
            if overlap:
                conflicts.append(
                    {
                        "reservation_id": rid,
                        "intersection": canonical_point_pairs(frozenset(overlap)),
                    }
                )
        return conflicts

    @staticmethod
    def _row(entry):
        return {
            "resource": entry["resource"],
            "reservation_id": entry["reservation_id"],
            "owner": entry["owner"],
            "status": entry["status"],
            "occupation": {
                "half_open": canonical_point_pairs(entry["points"]),
                "text": render_text(canonical_point_pairs(entry["points"])),
            },
            "seq": entry["seq"],
        }

    def _conflict_rows(self, resource, conflicts):
        rows = []
        for rid, points in conflicts.items():
            entry = self.reservations[rid]
            rows.append(
                {
                    "resource": resource,
                    "reservation_id": rid,
                    "owner": entry["owner"],
                    "occupation": {
                "half_open": canonical_point_pairs(entry["points"]),
                "text": render_text(canonical_point_pairs(entry["points"])),
            },
                    "intersection": {
                        "half_open": canonical_point_pairs(frozenset(points)),
                        "text": render_text(canonical_point_pairs(frozenset(points))),
                    },
                }
            )
        return rows


class ModelBasedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db = os.path.join(self.tmpdir.name, "model.sqlite")
        self.store = ReservationStore(self.db)
        self.model = Model()

    def snapshot(self):
        conn = sqlite3.connect(self.db)
        try:
            reservations = conn.execute(
                "SELECT * FROM reservations ORDER BY resource, reservation_id"
            ).fetchall()
            receipts = conn.execute(
                "SELECT * FROM requests ORDER BY request_id"
            ).fetchall()
            meta = conn.execute("SELECT * FROM metadata ORDER BY key").fetchall()
        finally:
            conn.close()
        return reservations, receipts, meta

    def apply(self, method: str, args: tuple, kwargs: dict):
        """Run the same operation on repository and model; compare outcomes."""

        before = self.snapshot()
        try:
            actual = getattr(self.store, method)(*args, **kwargs)
            actual_error = None
        except (OccupancyConflict, RequestMismatchError, ValidationError,
                ReservationExists, ReservationNotFound, ReservationStateError) as exc:
            actual = None
            actual_error = exc
        try:
            expected = getattr(self.model, method)(*args, **kwargs)
            expected_error = None
        except (OccupancyConflict, RequestMismatchError, ValidationError,
                ReservationExists, ReservationNotFound, ReservationStateError) as exc:
            expected = None
            expected_error = exc

        if expected_error is not None:
            self.assertIsNotNone(actual_error, (method, args, expected_error))
            self.assertIs(type(actual_error), type(expected_error), (method, args))
            if isinstance(actual_error, OccupancyConflict):
                self.assertEqual(
                    sorted(c["reservation_id"] for c in actual_error.conflicts),
                    sorted(c["reservation_id"] for c in expected_error.conflicts),
                )
                actual_map = {
                    c["reservation_id"]: c["intersection"]["half_open"]
                    for c in actual_error.conflicts
                }
                expected_map = {
                    c["reservation_id"]: c["intersection"]["half_open"]
                    for c in expected_error.conflicts
                }
                self.assertEqual(actual_map, expected_map)
            # A failure must not leave partial state behind.
            self.assertEqual(self.snapshot(), before, f"{method} leaked state on error")
        else:
            self.assertIsNone(actual_error, (method, args))
            self.assertEqual(
                actual["request_id"], expected["request_id"]
            )
            self.assertEqual(actual["action"], expected["action"])
            self.assertEqual(actual["status"], "success")
            reservation = actual["reservation"]
            expected_reservation = expected["reservation"]
            self.assertEqual(reservation["status"], expected_reservation["status"])
            self.assertEqual(reservation["owner"], expected_reservation["owner"])
            self.assertEqual(
                reservation["occupation"]["half_open"],
                expected_reservation["occupation"]["half_open"],
            )
        return actual_error

    def test_random_operation_sequence_against_enumeration(self):
        rng = random.Random(0xC0FFEE)
        counters = {"create": 0, "replace": 0, "cancel": 0, "replay": 0}
        request_counter = 0
        # request_id -> (action, exact args used, original segments)
        successful_requests: dict[str, tuple[str, tuple, object]] = {}

        def new_request_id():
            nonlocal request_counter
            request_counter += 1
            return f"req-{request_counter}"

        ids = [f"r{i}" for i in range(6)]
        for step in range(400):
            operation = rng.choices(
                ("create", "replace", "cancel", "replay", "query", "list"),
                weights=(40, 20, 10, 12, 13, 5),
            )[0]
            resource = rng.choice(RESOURCES)
            reservation_id = rng.choice(ids)
            segments = random_segments(rng)

            if operation == "create":
                request_id = new_request_id()
                owner = rng.choice(("alice", "bob"))
                error = self.apply(
                    "create",
                    (request_id, resource, reservation_id, owner, segments),
                    {},
                )
                counters["create"] += 1
                if error is None:
                    successful_requests[request_id] = (
                        "create",
                        (request_id, resource, reservation_id, owner, segments),
                        segments,
                    )
            elif operation == "replace":
                request_id = new_request_id()
                owner = rng.choice(("alice", "bob"))
                error = self.apply(
                    "replace",
                    (request_id, resource, reservation_id, owner, segments),
                    {},
                )
                counters["replace"] += 1
                if error is None:
                    successful_requests[request_id] = (
                        "replace",
                        (request_id, resource, reservation_id, owner, segments),
                        segments,
                    )
            elif operation == "cancel":
                request_id = new_request_id()
                error = self.apply(
                    "cancel", (request_id, resource, reservation_id), {}
                )
                counters["cancel"] += 1
                if error is None:
                    successful_requests[request_id] = (
                        "cancel",
                        (request_id, resource, reservation_id),
                        None,
                    )
            elif operation == "replay":
                if not successful_requests:
                    continue
                request_id = rng.choice(list(successful_requests))
                action, stored_args, original_segments = successful_requests[request_id]
                if rng.random() < 0.55:
                    # Exact same canonical request must replay the exact same
                    # receipt.  Optionally feed an equivalent representation.
                    args_to_replay = list(stored_args)
                    if original_segments is not None and rng.random() < 0.5:
                        args_to_replay[-1] = equivalent_representation(
                            rng, materialize(original_segments)
                        )
                    before = self.snapshot()
                    actual = self.store.__getattribute__(action)(*args_to_replay)
                    expected = self.model.__getattribute__(action)(*stored_args)
                    self.assertEqual(self.snapshot(), before, "replay wrote state")
                    self.assertEqual(
                        _canonical_payload(actual), _canonical_payload(expected)
                    )
                else:
                    # A genuinely different payload under the same id is
                    # rejected and must not change any state.
                    mutated = list(stored_args)
                    if action != "cancel":
                        mutated[-1] = [
                            {
                                "lower": 0,
                                "upper": 0,
                                "lower_inclusive": True,
                                "upper_inclusive": True,
                            }
                        ]
                        mutated[-2] = "mallory"
                    else:
                        mutated[2] = "z"
                    error = self.apply(action, tuple(mutated), {})
                    self.assertIsInstance(error, RequestMismatchError)
                counters["replay"] += 1
            elif operation == "query":
                points = materialize(segments)
                actual = self.store.query(resource, segments)
                expected = self.model.query(resource, points)
                self.assertEqual(
                    sorted(
                        (item["reservation_id"], item["intersection"]["half_open"])
                        for item in actual["conflicts"]
                    ),
                    sorted(
                        (item["reservation_id"], item["intersection"])
                        for item in expected
                    ),
                )
                self.assertEqual(actual["available"], not expected)
            else:
                actual_rows = {
                    (row["resource"], row["reservation_id"]): (
                        row["status"],
                        row["owner"],
                        row["occupation"]["half_open"],
                    )
                    for row in self.store.list_reservations(resource=resource)
                }
                expected_rows = {
                    (entry["resource"], rid): (
                        entry["status"], entry["owner"],
                        canonical_point_pairs(entry["points"]),
                    )
                    for rid, entry in self.model.reservations.items()
                    if entry["resource"] == resource
                }
                self.assertEqual(actual_rows, expected_rows)

        # Sanity: the sequence exercised every operation.
        for name, count in counters.items():
            self.assertGreater(count, 0, f"generator never produced {name}")

        report = self.store.verify()
        self.assertTrue(report["ok"], report["errors"])

        # Reopening the database preserves every successful record.
        reopened = ReservationStore(self.db)
        for rid, entry in self.model.reservations.items():
            row = reopened.get(entry["resource"], rid)
            self.assertIsNotNone(row, rid)
            self.assertEqual(row["status"], entry["status"])
            self.assertEqual(row["owner"], entry["owner"])
            self.assertEqual(
                row["occupation"]["half_open"], canonical_point_pairs(entry["points"])
            )
        self.assertTrue(reopened.verify()["ok"])

        # Every successful request replays its exact stored receipt and never
        # overwrites it, regardless of later state changes.
        for request_id, (action, payload, response) in self.model.receipts.items():
            conn = sqlite3.connect(self.db)
            try:
                stored = json.loads(
                    conn.execute(
                        "SELECT response_json FROM requests WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()[0]
                )
            finally:
                conn.close()
            self.assertEqual(stored, json.loads(json.dumps(response)))

    def test_equivalent_representations_produce_identical_receipts(self):
        base = {"lower": 1, "upper": 3, "lower_inclusive": True, "upper_inclusive": False}
        extra = {"lower": 3, "upper": 5, "lower_inclusive": True, "upper_inclusive": False}
        response = self.store.create("eq-1", "res-a", "x", "alice", [base, extra])
        replay = self.store.create(
            "eq-1", "res-a", "x", "alice",
            [
                {"lower": 1, "upper": 4, "lower_inclusive": True, "upper_inclusive": False},
                {"lower": 0, "upper": 2, "lower_inclusive": False, "upper_inclusive": False},
                {"lower": 4, "upper": 5, "lower_inclusive": True, "upper_inclusive": False},
            ],
        )
        self.assertEqual(replay, response)
        self.assertEqual(replay["reservation"]["occupation"]["half_open"], [[1, 5]])

    def test_set_of_receipts_matches_model_after_failures(self):
        self.store.create("ok-1", "res-a", "x", "alice", "[1,3)")
        before = self.snapshot()
        for attempt in range(30):
            try:
                self.store.create(
                    f"fail-{attempt}", "res-a", f"y{attempt}", "bob", "[2,4)"
                )
            except OccupancyConflict:
                pass
            else:  # pragma: no cover - first attempt must conflict
                self.fail("expected conflict")
            self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
