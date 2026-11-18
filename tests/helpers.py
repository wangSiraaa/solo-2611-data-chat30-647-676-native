"""Shared helpers for the test suite (stdlib unittest only)."""

from __future__ import annotations

import csv
import io
import os
import tempfile
import zlib
from typing import List, Sequence, Tuple

from archive_service.fixtures import Member, ZipBuilder


def write_bytes(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def make_zip(path: str, members: Sequence[Member], comment: bytes = b"") -> str:
    builder = ZipBuilder()
    for member in members:
        builder.add(member)
    return write_bytes(path, builder.build(comment=comment))


class NonSeekableWriter:
    """File-like object without seek/tell, forcing zipfile data descriptors."""

    def __init__(self, path: str):
        self._fh = open(path, "wb")

    def write(self, data):
        return self._fh.write(data)

    def flush(self):
        return self._fh.flush()

    def close(self):
        return self._fh.close()


def make_zip_with_stdlib(path: str, members: Sequence[Tuple[str, bytes]], descriptor: bool = False) -> str:
    """Independent fixture writer: CPython's zipfile (optionally descriptor)."""

    import zipfile

    target = NonSeekableWriter(path) if descriptor else path
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, body in members:
            zf.writestr(name, body)
    if descriptor:
        target.close()
    return path


def independent_table_rows(body: bytes, column_types: Sequence[str]) -> List[Tuple[int, Tuple]]:
    """Parse a CSV member using only the standard csv module.

    Returns ``(physical_start_line, converted_row)``.  This is intentionally
    independent of ``archive_service.csv_reader`` so tests cross-check rather
    than echo the implementation.
    """

    text = body.decode("utf-8")
    reader = csv.reader(io.StringIO(text, newline=""))
    result: List[Tuple[int, Tuple]] = []
    next(reader)  # header
    previous_line = reader.line_num
    for fields in reader:
        start = previous_line + 1
        previous_line = reader.line_num
        row = []
        for raw, declared in zip(fields, column_types):
            if raw == "\\N":
                row.append(None)
            elif declared == "INTEGER":
                row.append(int(raw))
            elif declared == "REAL":
                row.append(float(raw))
            else:
                row.append(raw)
        result.append((start, tuple(row)))
    return result


def deflate_raw(body: bytes) -> bytes:
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    return comp.compress(body) + comp.flush()


def temp_path(suffix: str = ".zip") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    os.unlink(path)
    return path
