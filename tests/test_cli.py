"""Tests for both command line entry points."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tests.helpers import temp_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )


class PathServiceCliTest(unittest.TestCase):
    def test_lax_hits(self) -> None:
        doc = json.dumps({"a": [{"x": 1}, {"y": 2}, {"x": None}]})
        path = json.dumps([{"key": "a"}, {"wildcard": "array"}, {"key": "x"}])
        result = run("-m", "jsonpath_service", "--doc", doc, "--path", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["hits"],
            [
                {"path": ["a", 0, "x"], "value": 1},
                {"path": ["a", 2, "x"], "value": None},
            ],
        )

    def test_strict_error_is_json_with_location(self) -> None:
        doc = json.dumps({"a": [{"x": 1}, {"y": 2}]})
        path = json.dumps([{"key": "a"}, {"wildcard": "array"}, {"key": "x"}])
        result = run("-m", "jsonpath_service", "--doc", doc, "--path", path, "--strict")
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"], "PathError")
        self.assertIn("a[1]", payload["detail"])

    def test_filter_expression(self) -> None:
        doc = json.dumps({"items": [{"k": "a", "n": 1}, {"k": "a"}]})
        path = json.dumps(
            [
                {"key": "items"},
                {
                    "filter": {
                        "and": [
                            {"eq": [[{"key": "k"}], "a"]},
                            {"exists": [{"key": "n"}]},
                        ]
                    }
                },
            ]
        )
        result = run("-m", "jsonpath_service", "--doc", doc, "--path", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["hits"], [{"path": ["items", 0], "value": {"k": "a", "n": 1}}]
        )


class RepositoryCliTest(unittest.TestCase):
    def test_full_lifecycle(self) -> None:
        with temp_db() as db:
            doc = json.dumps({"items": [{"kind": "alpha"}, {"kind": "beta"}]})
            result = run(
                "-m", "docrepo", "replace", "--db", db, "--doc-id", "d1", "--doc", doc
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["revision"], 1)

            path = json.dumps([{"key": "items"}, {"wildcard": "array"}, {"key": "kind"}])
            result = run(
                "-m", "docrepo", "register-query", "--db", db, "--name", "kinds",
                "--path", path,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            result = run("-m", "docrepo", "ensure-indexes", "--db", db)
            self.assertEqual(result.returncode, 0, result.stderr)

            result = run("-m", "docrepo", "search", "--db", db, "--name", "kinds")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(
                payload["matches"][0]["hits"],
                [
                    {"path": ["items", 0, "kind"], "value": "alpha"},
                    {"path": ["items", 1, "kind"], "value": "beta"},
                ],
            )

            result = run(
                "-m", "docrepo", "explain", "--db", db, "--name", "kinds",
                "--compare-scan",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            explanation = json.loads(result.stdout)
            self.assertTrue(explanation["scan_reference"]["identical"])

            result = run("-m", "docrepo", "scan", "--db", db, "--path", path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["matches"], payload["matches"])

            result = run(
                "-m", "docrepo", "delete", "--db", db, "--doc-id", "d1",
                "--expected-revision", "1",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            result = run("-m", "docrepo", "search", "--db", db, "--name", "kinds")
            self.assertEqual(json.loads(result.stdout)["matches"], [])

    def test_stale_revision_reports_error(self) -> None:
        with temp_db() as db:
            doc = json.dumps({"v": 1})
            run("-m", "docrepo", "replace", "--db", db, "--doc-id", "d", "--doc", doc)
            run("-m", "docrepo", "replace", "--db", db, "--doc-id", "d", "--doc", doc)
            result = run(
                "-m", "docrepo", "replace", "--db", db, "--doc-id", "d",
                "--doc", doc, "--expected-revision", "1",
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["error"], "StaleRevision")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
