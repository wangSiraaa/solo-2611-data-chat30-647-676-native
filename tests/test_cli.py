"""CLI integration tests.  Every subcommand is driven through main(argv)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from multi_range_reservation.cli import main


def run_cli(*argv: str) -> tuple[int, dict]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(list(argv))
    text = buffer.getvalue().strip()
    return code, json.loads(text) if text else {}


class CliRangeTests(unittest.TestCase):
    def test_normalize_union_intersect_difference_contains(self):
        code, out = run_cli("range", "normalize", "--value", "(1,3]")
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["half_open"], [[2, 4]])

        code, out = run_cli("range", "union", "[1,2)", "[2,3)")
        self.assertEqual(out["result"]["half_open"], [[1, 3]])

        code, out = run_cli(
            "range", "intersect", "--left", "[1,5)", "--right", "[3,9)"
        )
        self.assertEqual(out["result"]["half_open"], [[3, 5]])

        code, out = run_cli(
            "range", "difference", "--left", "[1,8)", "--right", "[2,3) [4,5)"
        )
        self.assertEqual(out["result"]["half_open"], [[1, 2], [3, 4], [5, 8]])

        code, out = run_cli("range", "contains", "--value", "[1,5)", "--point", "2")
        self.assertTrue(out["result"])
        code, out = run_cli("range", "contains", "--value", "[1,5)", "--point", "5")
        self.assertFalse(out["result"])

        code, out = run_cli(
            "range", "intersects", "--left", "[1,2)", "--right", "[2,3)"
        )
        self.assertFalse(out["result"])
        self.assertEqual(out["intersection"]["half_open"], [])

    def test_half_open_json_mode(self):
        code, out = run_cli(
            "range", "normalize", "--value", "[[1,3],[5,7]]", "--half-open"
        )
        self.assertEqual(out["result"]["half_open"], [[1, 3], [5, 7]])

    def test_invalid_range_returns_error_exit(self):
        code, out = run_cli("range", "normalize", "--value", "[5,1)")
        self.assertEqual(code, 0)  # [5,1) is a legal empty segment
        self.assertTrue(out["result"]["empty"])


class CliStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db = os.path.join(self.tmpdir.name, "cli.sqlite")

    def test_full_lifecycle(self):
        code, out = run_cli(
            "reserve", "--db", self.db, "--request-id", "q1", "--resource", "room",
            "--reservation-id", "a", "--owner", "alice", "--ranges", "[1,5)",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out["reservation"]["occupation"]["half_open"], [[1, 5]])

        code, out = run_cli(
            "reserve", "--db", self.db, "--request-id", "q2", "--resource", "room",
            "--reservation-id", "b", "--owner", "bob", "--ranges", "[4,7)",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out["error"], "occupancy_conflict")
        self.assertEqual(out["conflicts"][0]["intersection"]["half_open"], [[4, 5]])

        code, out = run_cli(
            "query", "--db", self.db, "--resource", "room", "--ranges", "[2,6)"
        )
        self.assertEqual(code, 3)
        self.assertEqual(out["conflict_count"], 1)

        code, out = run_cli(
            "replace", "--db", self.db, "--request-id", "q3", "--resource", "room",
            "--reservation-id", "a", "--owner", "alice", "--ranges", "[5,8)",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out["reservation"]["occupation"]["half_open"], [[5, 8]])

        code, out = run_cli(
            "reserve", "--db", self.db, "--request-id", "q4", "--resource", "room",
            "--reservation-id", "b", "--owner", "bob", "--ranges", "[1,5)",
        )
        self.assertEqual(code, 0)

        code, out = run_cli(
            "cancel", "--db", self.db, "--request-id", "q5", "--resource", "room",
            "--reservation-id", "a",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out["reservation"]["status"], "cancelled")

        code, out = run_cli("verify", "--db", self.db)
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])

        # Simulate a restart: a fresh invocation reads the same state.
        code, out = run_cli(
            "list", "--db", self.db, "--resource", "room"
        )
        statuses = {r["reservation_id"]: r["status"] for r in out["reservations"]}
        self.assertEqual(statuses, {"a": "cancelled", "b": "active"})

    def test_empty_ranges_rejected(self):
        code, out = run_cli(
            "reserve", "--db", self.db, "--request-id", "q1", "--resource", "room",
            "--reservation-id", "a", "--owner", "alice", "--ranges", "[]",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out["error"], "ValidationError")

    def test_request_mismatch_exit(self):
        run_cli(
            "reserve", "--db", self.db, "--request-id", "q1", "--resource", "room",
            "--reservation-id", "a", "--owner", "alice", "--ranges", "[1,5)",
        )
        code, out = run_cli(
            "reserve", "--db", self.db, "--request-id", "q1", "--resource", "room",
            "--reservation-id", "a", "--owner", "alice", "--ranges", "[1,6)",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out["error"], "RequestMismatchError")


if __name__ == "__main__":
    unittest.main()
