"""Cross-check against archives produced by CPython's own ``zipfile``.

The service does not use ``zipfile`` for reading; these tests make sure the
strict parser agrees with an independent, mature producer and that deliberate
mutations are rejected.
"""

from __future__ import annotations

import os
import struct
import unittest
import zipfile

from archive_service.zip_service import ArchiveReader, ArchiveRejected
from tests.helpers import temp_path, write_bytes


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class StdlibWriterTests(unittest.TestCase):
    def _path(self) -> str:
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_stored_and_deflated_members(self) -> None:
        path = self._path()
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("stored.txt", b"hello stored", compress_type=zipfile.ZIP_STORED)
            zf.writestr("deflated.txt", b"hello deflated" * 20)
            zf.writestr("empty.txt", b"")
        reader = ArchiveReader.open(path)
        bodies = {e.name: reader.read_entry(e.entry_id) for e in reader.list_entries()}
        self.assertEqual(bodies["stored.txt"], b"hello stored")
        self.assertEqual(bodies["deflated.txt"], b"hello deflated" * 20)
        self.assertEqual(bodies["empty.txt"], b"")

    def test_descriptor_archive_from_nonseekable_sink(self) -> None:
        class Sink:
            def __init__(self, path):
                self._fh = open(path, "wb")

            def write(self, data):
                return self._fh.write(data)

            def flush(self):
                return self._fh.flush()

        path = self._path()
        sink = Sink(path)
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("a.csv", b"id\n1\n")
            zf.writestr("b.csv", b"id\n2\n")
        sink._fh.close()
        reader = ArchiveReader.open(path)
        for info in reader.list_entries():
            self.assertTrue(info.has_data_descriptor)
            self.assertTrue(reader.read_entry(info.entry_id))

    def test_duplicate_names_written_by_zipfile(self) -> None:
        path = self._path()
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("dup.csv", b"first")
            zf.writestr("dup.csv", b"second")
        reader = ArchiveReader.open(path)
        a, b = reader.list_entries()
        self.assertEqual(a.name, b.name)
        self.assertEqual(reader.read_entry(a.entry_id), b"first")
        self.assertEqual(reader.read_entry(b.entry_id), b"second")

    def test_corrupted_central_crc_rejected_per_member(self) -> None:
        path = self._path()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("a.csv", b"abcdef")
        data = bytearray(_read(path))
        cd = data.find(b"PK\x01\x02")
        data[cd + 16] ^= 0xFF  # central CRC field
        write_bytes(path, bytes(data))
        # Local header still holds the correct CRC, so the mismatch is caught
        # as a structural contradiction (local vs central).
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)

    def test_corrupted_body_tail_flags_member(self) -> None:
        path = self._path()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("a.csv", b"abcdef" * 10)
        data = bytearray(_read(path))
        # First body byte region: 30-byte header + 5-byte name.
        data[30 + 5 + 55] ^= 0xFF
        write_bytes(path, bytes(data))
        reader = ArchiveReader.open(path)
        self.assertEqual(len(reader.crc_failures), 1)

    def test_zip64_rejected(self) -> None:
        path = self._path()
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a", b"x")
        data = bytearray(_read(path))
        eocd = data.rfind(b"PK\x05\x06")
        data[eocd + 10 : eocd + 12] = b"\xff\xff"  # entries total == 0xffff
        data[eocd + 8 : eocd + 10] = b"\xff\xff"   # entries on disk
        write_bytes(path, bytes(data))
        with self.assertRaises(ArchiveRejected) as ctx:
            ArchiveReader.open(path)
        self.assertIn("ZIP64", str(ctx.exception))

    def test_multi_disk_rejected(self) -> None:
        path = self._path()
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a", b"x")
        data = bytearray(_read(path))
        eocd = data.rfind(b"PK\x05\x06")
        data[eocd + 4 : eocd + 6] = struct.pack("<H", 1)
        write_bytes(path, bytes(data))
        with self.assertRaises(ArchiveRejected):
            ArchiveReader.open(path)


if __name__ == "__main__":
    unittest.main()
