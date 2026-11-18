"""Cross-seed randomized stress, checked purely by integer enumeration.

This is deliberately independent of the :class:`Model` class in ``test_model``:
the reference state is plain Python sets, and every expected outcome is derived
by enumerating points.  If the algebra, storage format, or transaction handling
drifts, these assertions fail.
"""

from __future__ import annotations

import os
import random
import tempfile
import unittest

from multi_range_reservation.repository import (
    OccupancyConflict,
    ReservationExists,
    ReservationNotFound,
    ReservationStateError,
    ReservationStore,
    ValidationError,
)
from tests.test_model import (
    canonical_point_pairs,
    materialize,
    random_segments,
)

SEEDS = range(12)
STEPS = 250
RESOURCES = ("A", "B")
ID_POOL = [f"id{i}" for i in range(6)]


class StressTests(unittest.TestCase):
    def run_seed(self, seed: int) -> None:
        rng = random.Random(seed)
        with tempfile.TemporaryDirectory() as tmpdir:
            db = os.path.join(tmpdir, "stress.sqlite")
            store = ReservationStore(db)
            active: dict[str, tuple[str, frozenset[int]]] = {}
            cancelled: dict[str, str] = {}  # reservation_id -> resource
            receipts: dict[str, tuple[str, tuple, dict]] = {}
            counter = 0

            for step in range(STEPS):
                operation = rng.choice(
                    ("create", "create", "replace", "cancel", "query", "replay")
                )
                resource = rng.choice(RESOURCES)
                reservation_id = rng.choice(ID_POOL)
                segments = random_segments(rng, max_segments=4)
                points = materialize(segments)
                context = (seed, step, operation)

                if operation == "create":
                    counter += 1
                    request_id = f"q{counter}"
                    if not points:
                        expected = "invalid"
                    elif reservation_id in active or reservation_id in cancelled:
                        expected = "exists"
                    else:
                        expected = (
                            "conflict"
                            if any(
                                r == resource and p & points
                                for r, p in active.values()
                            )
                            else "ok"
                        )
                    try:
                        out = store.create(
                            request_id, resource, reservation_id, "o", segments
                        )
                        outcome = "ok"
                        if expected == "ok":
                            active[reservation_id] = (resource, points)
                            receipts[request_id] = (
                                "create",
                                (request_id, resource, reservation_id, "o", segments),
                                out,
                            )
                    except ValidationError:
                        outcome = "invalid"
                    except ReservationExists:
                        outcome = "exists"
                    except OccupancyConflict:
                        outcome = "conflict"
                    self.assertEqual(outcome, expected, context)

                elif operation == "replace":
                    counter += 1
                    request_id = f"q{counter}"
                    if not points:
                        expected = "invalid"
                    elif (
                        reservation_id in cancelled
                        and cancelled[reservation_id] == resource
                    ):
                        expected = "state"
                    elif reservation_id not in active:
                        expected = "notfound"
                    elif active[reservation_id][0] != resource:
                        expected = "notfound"
                    else:
                        expected = (
                            "conflict"
                            if any(
                                other != reservation_id
                                and r == resource
                                and p & points
                                for other, (r, p) in active.items()
                            )
                            else "ok"
                        )
                    try:
                        out = store.replace(
                            request_id, resource, reservation_id, "o", segments
                        )
                        outcome = "ok"
                        if expected == "ok":
                            active[reservation_id] = (resource, points)
                            receipts[request_id] = (
                                "replace",
                                (request_id, resource, reservation_id, "o", segments),
                                out,
                            )
                    except ValidationError:
                        outcome = "invalid"
                    except ReservationStateError:
                        outcome = "state"
                    except ReservationNotFound:
                        outcome = "notfound"
                    except OccupancyConflict:
                        outcome = "conflict"
                    self.assertEqual(outcome, expected, context)

                elif operation == "cancel":
                    counter += 1
                    request_id = f"q{counter}"
                    owner = (
                        active[reservation_id][0]
                        if reservation_id in active
                        else cancelled.get(reservation_id)
                    )
                    expected = "ok" if owner == resource else "notfound"
                    try:
                        out = store.cancel(request_id, resource, reservation_id)
                        outcome = "ok"
                        if reservation_id in active and active[reservation_id][0] == resource:
                            active.pop(reservation_id)
                            cancelled[reservation_id] = resource
                        receipts[request_id] = (
                            "cancel",
                            (request_id, resource, reservation_id),
                            out,
                        )
                    except ReservationNotFound:
                        outcome = "notfound"
                    self.assertEqual(outcome, expected, context)

                elif operation == "query":
                    report = store.query(resource, segments)
                    expected = sorted(
                        (rid, canonical_point_pairs(frozenset(points & p)))
                        for rid, (r, p) in active.items()
                        if r == resource and p & points
                    )
                    actual = sorted(
                        (c["reservation_id"], c["intersection"]["half_open"])
                        for c in report["conflicts"]
                    )
                    self.assertEqual(actual, expected, context)
                    self.assertEqual(report["available"], not expected, context)

                else:  # replay
                    if not receipts:
                        continue
                    request_id = rng.choice(list(receipts))
                    action, args, original = receipts[request_id]
                    self.assertEqual(
                        getattr(store, action)(*args), original, context
                    )

            report = store.verify()
            self.assertTrue(report["ok"], (seed, report["errors"]))

            # Reopen and confirm the committed state survived.
            reopened = ReservationStore(db)
            for reservation_id, (resource, points) in active.items():
                row = reopened.get(resource, reservation_id)
                self.assertIsNotNone(row, (seed, reservation_id))
                self.assertEqual(row["status"], "active")
                self.assertEqual(
                    row["occupation"]["half_open"],
                    canonical_point_pairs(points),
                    (seed, reservation_id),
                )
            for reservation_id, resource in cancelled.items():
                row = reopened.get_by_id(reservation_id)
                self.assertIsNotNone(row)
                self.assertEqual(row["status"], "cancelled")
                self.assertEqual(row["resource"], resource)
            self.assertTrue(reopened.verify()["ok"])

    def test_all_seeds(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                self.run_seed(seed)


if __name__ == "__main__":
    unittest.main()
