"""Delivery manifest/path safety and ZIP-name decoupling."""

from __future__ import annotations

import os
import tempfile
import unittest

from archive_service.delivery import (
    build_delivery_manifest,
    canonical_relative_path,
    deliver,
    safe_join,
)
from archive_service.fixtures import Member
from archive_service.zip_service import ArchiveReader, DeliveryError
from tests.helpers import make_zip, temp_path


class PathCanonicalizationTests(unittest.TestCase):
    def test_accepts_simple_relative(self) -> None:
        self.assertEqual(canonical_relative_path("data/a.csv"), "data/a.csv")
        self.assertEqual(canonical_relative_path("a.csv"), "a.csv")

    def test_rejects_dangerous(self) -> None:
        for bad in [
            "",
            "/etc/passwd",
            "../escape",
            "a/../b",
            "a//b",
            "./a",
            "a/./b",
            "a\\b",
            "C:/x",
            "C:x",
            "a\x00b",
            "a b ",  # trailing whitespace in segment
        ]:
            with self.assertRaises(DeliveryError, msg=bad):
                canonical_relative_path(bad)

    def test_rejects_nfd(self) -> None:
        import unicodedata

        nfd = unicodedata.normalize("NFD", "café")
        with self.assertRaises(DeliveryError):
            canonical_relative_path(nfd)
        self.assertEqual(canonical_relative_path("café"), "café")

    def test_does_not_sanitize_then_overwrite(self) -> None:
        # Stripping ".." would silently turn a traversal into a valid path;
        # this implementation must reject instead.
        with self.assertRaises(DeliveryError):
            canonical_relative_path("safe/../../etc/passwd")


class ManifestCollisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = temp_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        make_zip(
            self.path,
            [
                Member(name="x.csv", body=b"id\n1\n"),
                Member(name="x.csv", body=b"id\n2\n"),
                Member(name="y.csv", body=b"id\n3\n"),
            ],
        )
        self.reader = ArchiveReader.open(self.path)
        self.ids = [e.entry_id for e in self.reader.list_entries()]

    def test_duplicate_destination_rejected(self) -> None:
        with self.assertRaises(DeliveryError):
            build_delivery_manifest(
                self.reader, [(self.ids[0], "a.csv"), (self.ids[1], "a.csv")]
            )

    def test_case_insensitive_collision_rejected(self) -> None:
        with self.assertRaises(DeliveryError):
            build_delivery_manifest(
                self.reader, [(self.ids[0], "A.csv"), (self.ids[1], "a.csv")]
            )

    def test_file_directory_prefix_collision_rejected(self) -> None:
        with self.assertRaises(DeliveryError):
            build_delivery_manifest(
                self.reader, [(self.ids[0], "data"), (self.ids[1], "data/a.csv")]
            )

    def test_same_id_twice_rejected(self) -> None:
        with self.assertRaises(DeliveryError):
            build_delivery_manifest(
                self.reader, [(self.ids[0], "a.csv"), (self.ids[0], "b.csv")]
            )

    def test_duplicate_names_get_safe_distinct_paths(self) -> None:
        manifest = build_delivery_manifest(
            self.reader, [(self.ids[0], "one/x.csv"), (self.ids[1], "two/x.csv")]
        )
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        deliver(self.reader, manifest, root)
        with open(os.path.join(root, "one/x.csv"), "rb") as fh:
            self.assertEqual(fh.read(), b"id\n1\n")
        with open(os.path.join(root, "two/x.csv"), "rb") as fh:
            self.assertEqual(fh.read(), b"id\n2\n")

    def test_safe_join_containment(self) -> None:
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(root))
        self.assertTrue(safe_join(root, "a/b").startswith(os.path.realpath(root)))
        with self.assertRaises(DeliveryError):
            safe_join(root, "../x")

    def test_delivery_rolls_back_on_crc_failure(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(
            path,
            [
                Member(name="ok.csv", body=b"id\n1\n"),
                Member(name="bad.csv", body=b"id\n2\n", crc_override=0, local_crc_override=0),
            ],
        )
        reader = ArchiveReader.open(path, verify_contents=False)
        ids = [e.entry_id for e in reader.list_entries()]
        manifest = build_delivery_manifest(reader, [(ids[0], "ok.csv"), (ids[1], "bad.csv")])
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        with self.assertRaises(Exception):
            deliver(reader, manifest, root)
        self.assertFalse(os.path.exists(os.path.join(root, "ok.csv")))
        self.assertFalse(os.path.exists(os.path.join(root, "bad.csv")))
        self.assertEqual(
            [n for n in os.listdir(root) if n.endswith(".csv")], []
        )


if __name__ == "__main__":
    unittest.main()
