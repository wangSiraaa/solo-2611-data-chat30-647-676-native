"""CSV parsing rules and transactional ingestion semantics."""

from __future__ import annotations

import os
import unittest

from archive_service.csv_reader import (
    CsvError,
    parse_records,
    make_table_spec,
)
from archive_service.fixtures import Member
from archive_service.ingest import (
    ManifestError,
    Warehouse,
    load_manifest_from_dict,
)
from archive_service.zip_service import ArchiveReader
from tests.helpers import make_zip, temp_path


SPEC = make_table_spec(
    "t",
    [("id", "INTEGER", True), ("name", "TEXT", False), ("score", "REAL", False)],
)


class CsvRuleTests(unittest.TestCase):
    def test_null_is_only_backslash_N(self) -> None:
        body = b'id,name,score\n1,\\N,2.5\n2,,3.0\n'
        rows = list(parse_records(body, SPEC))
        self.assertEqual(rows[0][2], (1, None, 2.5))
        self.assertEqual(rows[1][2], (2, "", 3.0))

    def test_quoted_commas_quotes_newlines(self) -> None:
        body = (
            b'id,name,score\r\n'
            b'1,"a,b",1.0\r\n'
            b'2,"he said ""hi""",2.0\r\n'
            b'3,"line1\nline2",3.0\r\n'
        )
        rows = list(parse_records(body, SPEC))
        self.assertEqual(rows[0][2][1], "a,b")
        self.assertEqual(rows[1][2][1], 'he said "hi"')
        self.assertEqual(rows[2][2][1], "line1\nline2")
        # Physical line where each record starts is unaffected by the embedded
        # newline, while the logical record ordinal stays contiguous.
        self.assertEqual([r[0] for r in rows], [2, 3, 4])
        self.assertEqual([r[1] for r in rows], [1, 2, 3])

    def test_empty_field_vs_null(self) -> None:
        body = b"id,name,score\n1,\\N,\\N\n2,,0.0\n"
        rows = list(parse_records(body, SPEC))
        self.assertIsNone(rows[0][2][1])
        self.assertEqual(rows[1][2][1], "")

    def test_type_errors_carry_line(self) -> None:
        body = b"id,name,score\n1,a,not-a-number\n"
        with self.assertRaises(CsvError) as ctx:
            list(parse_records(body, SPEC))
        self.assertEqual(ctx.exception.line, 2)
        with self.assertRaises(CsvError) as ctx:
            list(parse_records(b"id,name,score\nx,a,1.0\n", SPEC))
        self.assertEqual(ctx.exception.line, 2)

    def test_header_must_match(self) -> None:
        with self.assertRaises(CsvError):
            list(parse_records(b"id,score,name\n1,1.0,a\n", SPEC))

    def test_field_count_must_match(self) -> None:
        with self.assertRaises(CsvError) as ctx:
            list(parse_records(b"id,name,score\n1,a\n", SPEC))
        self.assertEqual(ctx.exception.line, 2)

    def test_unquoted_quote_rejected(self) -> None:
        with self.assertRaises(CsvError):
            list(parse_records(b'id,name,score\n1,a"b,1\n', SPEC))

    def test_utf8_required(self) -> None:
        with self.assertRaises(CsvError):
            list(parse_records(b"id,name,score\n1,\xff,1\n", SPEC))

    def test_pk_null_rejected(self) -> None:
        with self.assertRaises(CsvError):
            list(parse_records(b"id,name,score\n\\N,a,1\n", SPEC))

    def test_non_ascii_numeric_forms_rejected(self) -> None:
        # int()/float() would accept these; the declared types must not.
        for value in ["1_000", " 1", "0x10", "١٢٣"]:
            with self.assertRaises(CsvError, msg=value):
                list(parse_records(f"id,name,score\n{value},a,1\n".encode(), SPEC))
        for value in ["nan", "inf", "1_0", " 1.5"]:
            with self.assertRaises(CsvError, msg=value):
                list(parse_records(f"id,name,score\n1,a,{value}\n".encode(), SPEC))

    def test_encoding_must_be_utf8(self) -> None:
        with self.assertRaises(CsvError):
            list(parse_records(b"id,name,score\n1,a,1\n", SPEC, encoding="latin-1"))


class UnknownIdTests(unittest.TestCase):
    def test_entry_id_from_other_archive_rejected(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(path, [Member(name="a.csv", body=b"id,name,score\n1,x,1.0\n")])
        reader = ArchiveReader.open(path)
        manifest = load_manifest_from_dict(
            {
                "archive_sha256": "0" * 64,
                "tables": [
                    {
                        "table": "t",
                        "columns": [["id", "INTEGER", True], ["name", "TEXT", False], ["score", "REAL", False]],
                        "entry_ids": [f"{'0' * 64}:0"],
                    }
                ],
            }
        )
        with self.assertRaises(ManifestError):
            Warehouse(temp_path(suffix=".sqlite")).ingest(reader, manifest)

    def test_manifest_sha_mismatch_rejected(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(path, [Member(name="a.csv", body=b"id\n1\n")])
        reader = ArchiveReader.open(path)
        with self.assertRaises(ManifestError):
            load_manifest_from_dict({"archive_sha256": "0" * 64, "tables": []})


class TransactionSemanticsTests(unittest.TestCase):
    def _manifest(self, sha, entries):
        return load_manifest_from_dict(
            {
                "archive_sha256": sha,
                "tables": [
                    {
                        "table": table,
                        "columns": [["id", "INTEGER", True], ["name", "TEXT", False], ["score", "REAL", False]],
                        "entry_ids": [f"{sha}:{i}" for i in ids],
                    }
                    for table, ids in entries
                ],
            }
        )

    def _db(self) -> str:
        path = temp_path(suffix=".sqlite")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_bad_second_member_rolls_back_whole_table(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(
            path,
            [
                Member(name="good.csv", body=b"id,name,score\n1,a,1.0\n"),
                Member(name="bad.csv", body=b"id,name,score\n2,b,not-real\n"),
            ],
        )
        reader = ArchiveReader.open(path)
        sha = reader.archive_sha256
        manifest = self._manifest(sha, [("combined", [0, 1]), ("other", [0])])
        db = self._db()
        with Warehouse(db) as w:
            report = w.ingest(reader, manifest)
            self.assertEqual(report.committed_tables, ["other"])
            self.assertEqual(len(report.failed_tables), 1)
            failure = report.failed_tables[0]
            self.assertEqual(failure["entry_id"], f"{sha}:1")
            self.assertEqual(failure["line"], 2)
            self.assertFalse(w._table_exists("combined"))
            self.assertEqual(
                w.conn.execute('SELECT COUNT(*) FROM "other"').fetchone()[0], 1
            )

    def test_duplicate_pk_across_members_rejected(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(
            path,
            [
                Member(name="a.csv", body=b"id,name,score\n1,a,1.0\n2,b,2.0\n"),
                Member(name="b.csv", body=b"id,name,score\n2,c,3.0\n"),
            ],
        )
        reader = ArchiveReader.open(path)
        sha = reader.archive_sha256
        manifest = self._manifest(sha, [("t", [0, 1])])
        db = self._db()
        with Warehouse(db) as w:
            report = w.ingest(reader, manifest)
            self.assertFalse(report.ok)
            self.assertEqual(report.failed_tables[0]["entry_id"], f"{sha}:1")
            self.assertEqual(report.failed_tables[0]["line"], 2)
            self.assertIn("duplicate primary key", report.failed_tables[0]["message"])
            self.assertFalse(w._table_exists("t"))

    def test_crc_bad_member_fails_only_its_table(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(
            path,
            [
                Member(name="good.csv", body=b"id,name,score\n1,a,1.0\n"),
                Member(name="bad.csv", body=b"id,name,score\n2,b,2.0\n", crc_override=0, local_crc_override=0),
            ],
        )
        reader = ArchiveReader.open(path, verify_contents=False)
        sha = reader.archive_sha256
        manifest = self._manifest(sha, [("good", [0]), ("badtable", [1])])
        db = self._db()
        with Warehouse(db) as w:
            report = w.ingest(reader, manifest)
            self.assertEqual(report.committed_tables, ["good"])
            self.assertEqual(report.failed_tables[0]["entry_id"], f"{sha}:1")
            self.assertEqual(report.failed_tables[0]["line"], None)
            self.assertFalse(w._table_exists("badtable"))

    def test_existing_table_not_overwritten(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(path, [Member(name="a.csv", body=b"id,name,score\n1,a,1.0\n")])
        reader = ArchiveReader.open(path)
        sha = reader.archive_sha256
        manifest = self._manifest(sha, [("t", [0])])
        db = self._db()
        with Warehouse(db) as w:
            w.ingest(reader, manifest)
            report = w.ingest(reader, manifest)
            self.assertFalse(report.ok)
            self.assertIn("already exists", report.failed_tables[0]["message"])
            self.assertEqual(
                w.conn.execute('SELECT COUNT(*) FROM "t"').fetchone()[0], 1
            )

    def test_wrong_member_name_is_not_resolved_by_name(self) -> None:
        """Entry 0 and 1 share a name; only id 0 is in the manifest."""
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(
            path,
            [
                Member(name="same.csv", body=b"id,name,score\n1,first,1.0\n"),
                Member(name="same.csv", body=b"id,name,score\n9,decoy,9.0\n"),
            ],
        )
        reader = ArchiveReader.open(path)
        sha = reader.archive_sha256
        manifest = self._manifest(sha, [("t", [0])])
        db = self._db()
        with Warehouse(db) as w:
            report = w.ingest(reader, manifest)
            self.assertTrue(report.ok)
            rows = w.table_rows("t")
            self.assertEqual(rows, [(1, "first", 1.0)])
            prov = w.all_provenance("t")
            self.assertEqual(prov, [(1, f"{sha}:0", 2)])


class ManifestValidationTests(unittest.TestCase):
    def test_bad_sha_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            load_manifest_from_dict({"archive_sha256": "zz", "tables": []})

    def test_empty_tables_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            load_manifest_from_dict({"archive_sha256": "0" * 64, "tables": []})

    def test_duplicate_entry_id_in_table_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            load_manifest_from_dict(
                {
                    "archive_sha256": "0" * 64,
                    "tables": [
                        {
                            "table": "t",
                            "columns": [["id", "INTEGER", True]],
                            "entry_ids": ["abc:0", "abc:0"],
                        }
                    ],
                }
            )

    def test_no_primary_key_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            load_manifest_from_dict(
                {
                    "archive_sha256": "0" * 64,
                    "tables": [
                        {
                            "table": "t",
                            "columns": [["id", "INTEGER", False]],
                            "entry_ids": ["abc:0"],
                        }
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
