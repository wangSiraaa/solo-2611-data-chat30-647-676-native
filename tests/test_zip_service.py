"""Archive service: identity, structural validation, limits, source ranges."""

from __future__ import annotations

import os
import struct
import unittest
import zlib

from archive_service.fixtures import Member, ZipBuilder
from archive_service.zip_service import (
    ArchiveChanged,
    ArchiveReader,
    ArchiveRejected,
    EntryChecksumError,
    EntryNotFound,
    MAX_ENTRY_BYTES,
)
from tests.helpers import deflate_raw, make_zip, make_zip_with_stdlib, temp_path, write_bytes


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = temp_path()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        make_zip(
            self.path,
            [
                Member(name="dup.csv", body=b"id\n1\nfirst\n"),
                Member(name="dup.csv", body=b"id\n1\nsecond\n"),
                Member(name="other.csv", body=b"id\n2\n"),
            ],
        )

    def test_duplicate_names_keep_distinct_ids_and_bodies(self) -> None:
        reader = ArchiveReader.open(self.path)
        entries = reader.list_entries()
        self.assertEqual([e.name for e in entries], ["dup.csv", "dup.csv", "other.csv"])
        self.assertEqual(entries[0].index, 0)
        self.assertEqual(entries[1].index, 1)
        self.assertNotEqual(entries[0].entry_id, entries[1].entry_id)
        self.assertEqual(reader.read_entry(entries[0].entry_id), b"id\n1\nfirst\n")
        self.assertEqual(reader.read_entry(entries[1].entry_id), b"id\n1\nsecond\n")

    def test_entry_id_contains_archive_digest(self) -> None:
        reader = ArchiveReader.open(self.path)
        for entry in reader.list_entries():
            self.assertEqual(entry.entry_id, f"{reader.archive_sha256}:{entry.index}")

    def test_unknown_id_rejected(self) -> None:
        reader = ArchiveReader.open(self.path)
        with self.assertRaises(EntryNotFound):
            reader.read_entry("0" * 64 + ":0")
        with self.assertRaises(EntryNotFound):
            reader.get("deadbeef:0")

    def test_source_range_is_compressed_body(self) -> None:
        reader = ArchiveReader.open(self.path)
        info = reader.list_entries()[0]
        start, end = info.source_range
        raw = reader.read_source_range(info.entry_id)
        self.assertEqual(len(raw), end - start)
        self.assertEqual(zlib.decompress(raw, -15), reader.read_entry(info.entry_id))
        self.assertEqual(reader.read_source_range(info.entry_id, 0, 2), raw[:2])
        with self.assertRaises(Exception):
            reader.read_source_range(info.entry_id, 0, len(raw) + 1)


class StructuralValidationTests(unittest.TestCase):
    def test_data_descriptor_archive_accepted(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip_with_stdlib(
            path,
            [("a.csv", b"id,val\n1,x\n"), ("b.csv", b"id,val\n2,y\n")],
            descriptor=True,
        )
        reader = ArchiveReader.open(path)
        infos = reader.list_entries()
        self.assertTrue(all(i.has_data_descriptor for i in infos))
        self.assertEqual(reader.read_entry(infos[0].entry_id), b"id,val\n1,x\n")

    def _build_and_patch(self, mutate) -> str:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        builder = ZipBuilder().add(Member(name="m.csv", body=b"id,val\n1,payload\n"))
        data = bytearray(builder.build())
        mutate(data)
        write_bytes(path, bytes(data))
        return path

    def test_local_central_method_mismatch_rejected(self) -> None:
        def mutate(data):
            # local method at offset 8, central method at central+10
            data[8:10] = struct.pack("<H", 0)  # local says stored
        path = self._build_and_patch(mutate)
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)

    def test_local_central_name_mismatch_rejected(self) -> None:
        def mutate(data):
            name_at = 30
            data[name_at] = ord("X")
        path = self._build_and_patch(mutate)
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)

    def test_local_central_length_mismatch_rejected(self) -> None:
        def mutate(data):
            data[18:22] = struct.pack("<I", 999)  # local compressed size
        path = self._build_and_patch(mutate)
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)

    def test_efs_flag_with_invalid_utf8_rejected(self) -> None:
        builder = ZipBuilder().add(Member(name="ok.csv", body=b"id\n1\n"))
        data = bytearray(builder.build())
        data[30] = 0xFF  # corrupt the name but keep the UTF-8 flag
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        write_bytes(path, bytes(data))
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)

    def test_central_directory_overlap_rejected(self) -> None:
        b_name = b"b.csv"
        b_body = b"id\n2\n"
        b_local = (
            struct.pack(
                "<IHHHHHIIIHH",
                0x04034B50,
                20,
                0x800,
                0,
                0,
                0,
                zlib.crc32(b_body) & 0xFFFFFFFF,
                len(b_body),
                len(b_body),
                len(b_name),
                0,
            )
            + b_name
            + b_body
        )
        a_name = b"a.csv"
        a_local = (
            struct.pack(
                "<IHHHHHIIIHH",
                0x04034B50,
                20,
                0x800,
                0,
                0,
                0,
                zlib.crc32(b_local) & 0xFFFFFFFF,
                len(b_local),
                len(b_local),
                len(a_name),
                0,
            )
            + a_name
            + b_local
        )
        cd_offset = len(a_local)
        central = b""
        for name, body, offset in [(a_name, b_local, 0), (b_name, b_body, 30 + len(a_name))]:
            central += (
                struct.pack(
                    "<IHHHHHHIIIHHHHHII",
                    0x02014B50,
                    0x031E,
                    20,
                    0x800,
                    0,
                    0,
                    0,
                    zlib.crc32(body) & 0xFFFFFFFF,
                    len(body),
                    len(body),
                    len(name),
                    0,
                    0,
                    0,
                    0,
                    0,
                    offset,
                )
                + name
            )
        eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 2, 2, len(central), cd_offset, 0)
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        write_bytes(path, a_local + central + eocd)
        with self.assertRaises(ArchiveRejected) as ctx:
            ArchiveReader.open(path)
        self.assertIn("overlap", str(ctx.exception))

    def test_unsupported_method_rejected(self) -> None:
        builder = ZipBuilder().add(Member(name="m.bin", body=b"abc"))
        data = bytearray(builder.build())
        # Set method to 12 in both local and central headers.
        data[8:10] = struct.pack("<H", 12)
        cd = data.find(b"PK\x01\x02")
        data[cd + 10 : cd + 12] = struct.pack("<H", 12)
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        write_bytes(path, bytes(data))
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)

    def test_encrypted_flag_rejected(self) -> None:
        builder = ZipBuilder().add(Member(name="m.bin", body=b"abc"))
        data = bytearray(builder.build())
        data[6:8] = struct.pack("<H", 0x0801)
        cd = data.find(b"PK\x01\x02")
        data[cd + 8 : cd + 10] = struct.pack("<H", 0x0801)
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        write_bytes(path, bytes(data))
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)


class LengthAndCrcTests(unittest.TestCase):
    def test_forged_uncompressed_length_rejected(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(
            path,
            [
                Member(
                    name="m.csv",
                    body=b"id\n1\n" + b"x" * 100,
                    usize_override=5,
                    local_usize_override=5,
                )
            ],
        )
        with self.assertRaises(ArchiveRejected) as ctx:
            ArchiveReader.open(path)
        self.assertIn("exceeded declared", str(ctx.exception))

    def test_forged_compressed_length_rejected(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(
            path,
            [
                Member(
                    name="m.csv",
                    body=b"id\n1\nhello world\n",
                    csize_override=4,
                    local_csize_override=4,
                )
            ],
        )
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)

    def test_crc_mismatch_detected_on_read(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(path, [Member(name="m.csv", body=b"id\n1\nabc\n", crc_override=1, local_crc_override=1)])
        reader = ArchiveReader.open(path, verify_contents=False)
        info = reader.list_entries()[0]
        with self.assertRaises(EntryChecksumError) as ctx:
            reader.read_entry(info.entry_id)
        self.assertIn(info.entry_id, str(ctx.exception))

    def test_crc_mismatch_is_member_level_failure(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(
            path,
            [
                Member(name="ok.csv", body=b"id\n1\n"),
                Member(name="bad.csv", body=b"id\n2\n", crc_override=1, local_crc_override=1),
            ],
        )
        reader = ArchiveReader.open(path)  # full verification
        self.assertEqual(list(reader.crc_failures), [reader.list_entries()[1].entry_id])
        # The good member remains readable; the bad one raises per-member.
        self.assertEqual(reader.read_entry(reader.list_entries()[0].entry_id), b"id\n1\n")
        with self.assertRaises(EntryChecksumError):
            reader.read_entry(reader.list_entries()[1].entry_id)

    def test_descriptor_disagreeing_with_central_rejected(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(
            path,
            [
                Member(
                    name="m.csv",
                    body=b"id\n1\n",
                    descriptor=True,
                    local_csize_override=999,
                    local_usize_override=999,
                )
            ],
        )
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)

    def test_truncated_file_rejected(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(path, [Member(name="m.csv", body=b"id\n1\n" + b"y" * 100)])
        with open(path, "rb") as fh:
            data = fh.read()
        write_bytes(path, data[:-30])
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)

    def test_no_pk_scan_for_entries(self) -> None:
        """A stored body containing 'PK\x03\x04' must not be mistaken for a header."""

        body = b"id\n1\nPK\x03\x04not-a-header\x00\x00\n"
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(path, [Member(name="tricky.csv", body=body, method=0)])
        reader = ArchiveReader.open(path)
        info = reader.list_entries()[0]
        self.assertEqual(reader.read_entry(info.entry_id), body)
        self.assertEqual(info.uncompressed_size, len(body))


class LimitTests(unittest.TestCase):
    def test_entry_limit_uses_actual_output(self) -> None:
        # Declared size says small (so the CD is accepted), but the actual
        # deflate stream expands past 8 MiB.  The forged declaration would pass
        # a metadata-only check; actual output must be measured.
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        big = b"a" * (MAX_ENTRY_BYTES + 10)
        payload = deflate_raw(big)
        make_zip(
            path,
            [
                Member(
                    name="bomb.csv",
                    body=big,
                    usize_override=10,
                    local_usize_override=10,
                    csize_override=len(payload),
                    local_csize_override=len(payload),
                )
            ],
        )
        with self.assertRaises(ArchiveRejected) as ctx:
            ArchiveReader.open(path)
        self.assertIn("exceeded declared", str(ctx.exception))

    def test_total_limit_is_enforced(self) -> None:
        # 13 members of ~8 MiB would exceed 100 MiB if actually decompressed.
        # Each member is under the 8 MiB cap, so the total must be computed
        # from real output.
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        builder = ZipBuilder()
        body = b"z" * (8 * 1024 * 1024 - 100)
        for i in range(13):
            builder.add(Member(name=f"m{i}.bin", body=body))
        write_bytes(path, builder.build())
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)

    def test_entry_limit_is_per_member(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        body = b"q" * (MAX_ENTRY_BYTES + 1)
        make_zip(
            path,
            [
                Member(
                    name="too-big.bin",
                    body=body,
                    usize_override=MAX_ENTRY_BYTES,
                    local_usize_override=MAX_ENTRY_BYTES,
                    csize_override=len(deflate_raw(body)),
                    local_csize_override=len(deflate_raw(body)),
                )
            ],
        )
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)


class ReopenTests(unittest.TestCase):
    def test_changed_archive_detected(self) -> None:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        make_zip(path, [Member(name="m.csv", body=b"id\n1\n")])
        reader = ArchiveReader.open(path)
        info = reader.list_entries()[0]
        # Rewrite to a different but valid archive of the same size class.
        make_zip(path, [Member(name="m.csv", body=b"id\n2\n")])
        with self.assertRaises(ArchiveChanged):
            reader.read_entry(info.entry_id)

    def test_read_after_close_and_reopen(self) -> None:
        from archive_service.generate import build_delivery

        summary = build_delivery()
        reader = ArchiveReader.open(summary["archive"])
        ids = [e.entry_id for e in reader.list_entries()]
        del reader
        reader2 = ArchiveReader.open(summary["archive"])
        for entry_id in ids:
            self.assertEqual(reader2.read_entry(entry_id), ArchiveReader.open(summary["archive"]).read_entry(entry_id))


if __name__ == "__main__":
    unittest.main()
