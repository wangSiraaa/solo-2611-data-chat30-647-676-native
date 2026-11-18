"""End-to-end verification against independently generated ground truth.

The tests never trust ingest's own parser for correctness: every member is
parsed again with the standard-library ``csv`` module, compared row-by-row to
``ground_truth.json`` (rows, types and physical source line), and every SQLite
row is traced back through its recorded ``entry_id`` to the original member.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from archive_service import generate
from archive_service.delivery import build_delivery_manifest, deliver
from archive_service.ingest import Warehouse, load_manifest_from_dict
from archive_service.zip_service import ArchiveReader, ArchiveRejected
from tests.helpers import independent_table_rows


class DemoArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.summary = generate.build_delivery()
        cls.archive = cls.summary["archive"]
        with open(cls.summary["truth"], encoding="utf-8") as fh:
            cls.truth = json.load(fh)
        with open(cls.summary["manifest"], encoding="utf-8") as fh:
            cls.manifest_payload = json.load(fh)
        cls.manifest = load_manifest_from_dict(cls.manifest_payload)

    def test_independent_verification_report(self) -> None:
        from archive_service import verify

        db = tempfile.mktemp(suffix=".sqlite")
        self.addCleanup(lambda: os.path.exists(db) and os.unlink(db))
        report = verify.verify_demo(self.summary, db_path=db)
        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["checks"]["rows_compared"], 125)
        self.assertEqual(report["checks"]["provenance_rows_reverified"], 125)
        self.assertEqual(report["checks"]["delivered_members"], 6)
        for outcome in report["adversarial_archives"].values():
            self.assertIn(outcome["result"], {"opened", "rejected"})
        self.assertTrue(
            all(v["result"] == "rejected" for v in report["delivery_paths"].values())
        )

    def test_archive_listing_preserves_duplicate_names_and_utf8(self) -> None:
        reader = ArchiveReader.open(self.archive)
        entries = reader.list_entries()
        names = [e.name for e in entries]
        self.assertEqual(names.count("data/customers.csv"), 2)
        self.assertIn("data/事件-é.csv", names)
        self.assertIn("data/legacy-café.csv", names)
        self.assertTrue(entries[3].has_data_descriptor)
        self.assertEqual(entries[2].method_name, "stored")
        # The legacy-café member has no EFS bit but still decodes as UTF-8.
        self.assertFalse(entries[5].utf8_name)
        self.assertEqual(entries[5].name, "data/legacy-café.csv")
        self.assertEqual(
            [e.entry_id for e in entries],
            [f"{reader.archive_sha256}:{i}" for i in range(len(entries))],
        )

    def test_independent_member_read_matches_truth(self) -> None:
        reader = ArchiveReader.open(self.archive)
        for table, info in self.truth["tables"].items():
            types = self._column_types(table)
            for entry_id, expected in info["members"].items():
                body = reader.read_entry(entry_id)
                actual = independent_table_rows(body, types)
                self.assertEqual(len(actual), len(expected), entry_id)
                for (line, row), exp in zip(actual, expected):
                    self.assertEqual(line, exp["line"], f"{entry_id} line")
                    self.assertEqual(list(row), exp["row"], f"{entry_id} row")

    def test_decoy_is_never_loaded(self) -> None:
        reader = ArchiveReader.open(self.archive)
        decoy_id = next(iter(self.truth["decoy_member"]))
        decoy_rows = self.truth["decoy_member"][decoy_id]
        # The decoy member is readable, but not part of the customers table.
        body = reader.read_entry(decoy_id)
        self.assertTrue(body)
        manifest_ids = [
            eid for table in self.manifest.tables for eid in table.entry_ids
        ]
        self.assertNotIn(decoy_id, manifest_ids)
        self.assertEqual(len(decoy_rows), 5)

    def _column_types(self, table: str):
        for t in self.manifest_payload["tables"]:
            if t["table"] == table:
                return [c[1] for c in t["columns"]]
        raise AssertionError(table)

    def test_ingest_matches_truth_and_provenance(self) -> None:
        reader = ArchiveReader.open(self.archive)
        db = tempfile.mktemp(suffix=".sqlite")
        self.addCleanup(lambda: os.path.exists(db) and os.unlink(db))
        with Warehouse(db) as w:
            report = w.ingest(reader, self.manifest)
            self.assertTrue(report.ok, report.as_dict())

            for table, truth in self.truth["tables"].items():
                expected_rows = [tuple(r) for r in truth["all_rows"]]
                rows = w.table_rows(table)
                self.assertEqual(len(rows), len(expected_rows), table)
                self.assertEqual(rows, expected_rows, table)
                # Compare types too, not just values.
                for row, exp in zip(rows, expected_rows):
                    self.assertEqual([type(v) for v in row], [type(v) for v in exp])

                # Provenance: rebuild expected (entry_id, line) in manifest and
                # member order, then trace every DB row back to its original.
                expected_prov = []
                for entry_id in next(
                    t.entry_ids for t in self.manifest.tables if t.spec.table_name == table
                ):
                    expected_prov.extend(
                        (entry_id, m["line"])
                        for m in truth["members"][entry_id]
                    )
                actual_prov = w.all_provenance(table)
                self.assertEqual(
                    [(e, l) for _rowid, e, l in actual_prov], expected_prov, table
                )
                for rowid, entry_id, line in actual_prov:
                    body = reader.read_entry(entry_id)  # reopen + re-verify
                    parsed = independent_table_rows(body, self._column_types(table))
                    match = [r for start, r in parsed if start == line]
                    self.assertEqual(len(match), 1)
                    db_row = w.conn.execute(
                        f'SELECT * FROM "{table}" WHERE rowid=?', (rowid,)
                    ).fetchone()
                    self.assertEqual(tuple(db_row), match[0])

    def test_reopen_database_and_archive_by_entry_id(self) -> None:
        reader = ArchiveReader.open(self.archive)
        db = tempfile.mktemp(suffix=".sqlite")
        self.addCleanup(lambda: os.path.exists(db) and os.unlink(db))
        with Warehouse(db) as w:
            self.assertTrue(w.ingest(reader, self.manifest).ok)
        # Close both, then reopen.
        del w
        reader2 = ArchiveReader.open(self.archive)
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        for table, truth in self.truth["tables"].items():
            expected = [tuple(r) for r in truth["all_rows"]]
            rows = conn.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            self.assertEqual([tuple(r) for r in rows], expected)
            prov = conn.execute(
                "SELECT rowid_value, entry_id, source_line FROM _zs_row_source "
                "WHERE table_name=? ORDER BY rowid_value",
                (table,),
            ).fetchall()
            for rowid, entry_id, line in prov:
                body = reader2.read_entry(entry_id)
                parsed = independent_table_rows(body, self._column_types(table))
                self.assertIn(line, [start for start, _ in parsed])
                expected_row = next(r for start, r in parsed if start == line)
                self.assertEqual(tuple(expected_row), tuple(rows[rowid - 1]))

    def test_delivery_tree_reproves_identity(self) -> None:
        reader = ArchiveReader.open(self.archive)
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        items = [
            (info.entry_id, f"{info.index:02d}/{info.name}")
            for info in reader.list_entries()
        ]
        manifest = build_delivery_manifest(reader, items)
        written = deliver(reader, manifest, root)
        self.assertEqual(len(written), len(items))
        for entry_id, rel, length in written:
            path = os.path.join(root, *rel.split("/"))
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), reader.read_entry(entry_id))
            self.assertEqual(os.path.getsize(path), length)


class AdversarialArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.summary = generate.build_delivery()
        cls.bad_dir = generate.BAD_DIR

    def path(self, name: str) -> str:
        return os.path.join(self.bad_dir, name)

    def test_duplicate_name_archive_keeps_both_bodies(self) -> None:
        reader = ArchiveReader.open(self.path("duplicate_name.zip"))
        e0, e1 = reader.list_entries()
        self.assertEqual(e0.name, e1.name)
        self.assertNotEqual(e0.entry_id, e1.entry_id)
        self.assertEqual(reader.read_entry(e0.entry_id), b"id,name\n1,first\n")
        self.assertEqual(reader.read_entry(e1.entry_id), b"id,name\n2,second\n")

    def test_data_descriptor_archive_verified(self) -> None:
        reader = ArchiveReader.open(self.path("data_descriptor.zip"))
        infos = reader.list_entries()
        self.assertTrue(infos[0].has_data_descriptor)
        self.assertFalse(infos[1].has_data_descriptor)
        for info in infos:
            self.assertTrue(reader.read_entry(info.entry_id))

    def test_forged_length_rejected(self) -> None:
        with self.assertRaises(ArchiveRejected) as ctx:
            ArchiveReader.open(self.path("forged_length.zip"))
        self.assertIn("exceeded declared", str(ctx.exception))

    def test_fake_central_length_rejected(self) -> None:
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(self.path("fake_central_length.zip"))

    def test_crc_corruption_flags_only_the_affected_member(self) -> None:
        reader = ArchiveReader.open(self.path("crc_corruption.zip"))
        self.assertEqual(len(reader.crc_failures), 1)
        bad_id = next(iter(reader.crc_failures))
        self.assertEqual(reader.get(bad_id).index, 1)
        self.assertTrue(reader.read_entry(reader.list_entries()[0].entry_id))
        with self.assertRaises(Exception) as ctx:
            reader.read_entry(bad_id)
        self.assertIn("CRC32", str(ctx.exception))

    def test_overlap_rejected(self) -> None:
        with self.assertRaises(ArchiveRejected) as ctx:
            ArchiveReader.open(self.path("overlap.zip"))
        self.assertIn("overlap", str(ctx.exception))

    def test_bad_utf8_name_rejected(self) -> None:
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(self.path("bad_utf8_name.zip"))

    def test_overlap_archive_cannot_load(self) -> None:
        db = tempfile.mktemp(suffix=".sqlite")
        self.addCleanup(lambda: os.path.exists(db) and os.unlink(db))
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(self.path("overlap.zip"))


class CrossMemberPrimaryKeyTests(unittest.TestCase):
    def test_cross_member_duplicate_pk_aborts_table(self) -> None:
        from archive_service.fixtures import Member
        from tests.helpers import make_zip, temp_path

        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(
            path,
            [
                Member(name="a.csv", body=b"id,name\n1,a\n2,b\n"),
                Member(name="b.csv", body=b"id,name\n2,dup\n3,c\n"),
            ],
        )
        reader = ArchiveReader.open(path)
        sha = reader.archive_sha256
        manifest = load_manifest_from_dict(
            {
                "archive_sha256": sha,
                "tables": [
                    {
                        "table": "t",
                        "columns": [["id", "INTEGER", True], ["name", "TEXT", False]],
                        "entry_ids": [f"{sha}:0", f"{sha}:1"],
                    }
                ],
            }
        )
        db = tempfile.mktemp(suffix=".sqlite")
        self.addCleanup(lambda: os.path.exists(db) and os.unlink(db))
        with Warehouse(db) as w:
            report = w.ingest(reader, manifest)
            self.assertFalse(report.ok)
            self.assertEqual(report.failed_tables[0]["entry_id"], f"{sha}:1")
            self.assertEqual(report.failed_tables[0]["line"], 2)
            self.assertFalse(w._table_exists("t"))


if __name__ == "__main__":
    unittest.main()
