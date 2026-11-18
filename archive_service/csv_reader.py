"""Strict CSV decoding for ingestion.

Rules (deliberately narrow and explicit):

* Bytes are decoded as UTF-8 with no BOM.
* RFC 4180 quoting: quoted fields may contain commas and embedded newlines;
  a doubled quote inside a quoted field is one literal quote.
* ``\\N`` (two characters: backslash and capital N) is SQL NULL.
* An empty field is the empty string, distinct from NULL.
* Record terminators are CRLF or LF; a final record may omit the terminator.
* Logical line numbers count physical lines, starting at 1 with the header.
* Every record must have exactly the declared column count and every type
  conversion is strict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional, Sequence, Tuple

NULL_TOKEN = "\\N"

_INTEGER_RE = re.compile(r"[+-]?[0-9]+\Z")
_REAL_RE = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z"
)


class CsvError(Exception):
    def __init__(self, line: int, message: str, record: Optional[int] = None):
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.record = record
        self.message = message


@dataclass(frozen=True)
class Column:
    name: str
    declared_type: str
    primary_key: bool = False

    def __post_init__(self) -> None:
        valid = {"TEXT", "INTEGER", "REAL"}
        if self.declared_type not in valid:
            raise ValueError(f"unsupported column type {self.declared_type!r}")

    @property
    def sql_type(self) -> str:
        return self.declared_type


@dataclass(frozen=True)
class TableSpec:
    table_name: str
    columns: Tuple[Column, ...]

    @property
    def primary_key(self) -> Tuple[Column, ...]:
        return tuple(c for c in self.columns if c.primary_key)

    @property
    def column_names(self) -> Tuple[str, ...]:
        return tuple(c.name for c in self.columns)


def _validate_identifier(name: str, what: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError(f"{what} must be a non-empty string")
    if "\x00" in name:
        raise ValueError(f"{what} contains NUL")
    if not (name[0].isalpha() or name[0] == "_"):
        raise ValueError(f"{what} must start with a letter or underscore")
    if not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"{what} may contain only letters, digits and underscores")


_RESERVED_COLUMN_NAMES = {"rowid", "_rowid_", "oid"}


def make_table_spec(
    table_name: str,
    columns: Sequence[Tuple[str, str, bool]],
) -> TableSpec:
    """Build and validate a table specification.

    ``columns`` is a sequence of ``(name, type, primary_key)`` tuples.
    Names are validated case-insensitively because SQLite identifiers are
    case-insensitive, and names that would shadow SQLite's implicit rowid are
    rejected (the provenance ledger is keyed on rowid).
    """

    _validate_identifier(table_name, "table name")
    built: List[Column] = []
    seen = set()
    for name, declared_type, primary_key in columns:
        _validate_identifier(name, "column name")
        folded = name.casefold()
        if folded in seen:
            raise ValueError(f"duplicate column {name!r}")
        if folded in _RESERVED_COLUMN_NAMES:
            raise ValueError(f"column name {name!r} is reserved by SQLite")
        seen.add(folded)
        built.append(Column(name=name, declared_type=declared_type, primary_key=bool(primary_key)))
    if not built:
        raise ValueError("a table needs at least one column")
    if not any(c.primary_key for c in built):
        raise ValueError(f"table {table_name!r} declares no primary key")
    return TableSpec(table_name=table_name, columns=tuple(built))


def _convert(raw: str, column: Column, line: int) -> Any:
    if raw == NULL_TOKEN:
        if column.primary_key:
            raise CsvError(line, f"primary key column {column.name!r} may not be NULL")
        return None
    if column.declared_type == "TEXT":
        return raw
    if column.declared_type == "INTEGER":
        if raw == "":
            raise CsvError(line, f"empty string is not a valid INTEGER for {column.name!r}")
        if not _INTEGER_RE.match(raw):
            raise CsvError(line, f"invalid INTEGER {raw!r} for column {column.name!r}")
        return int(raw, 10)
    if column.declared_type == "REAL":
        if raw == "":
            raise CsvError(line, f"empty string is not a valid REAL for {column.name!r}")
        if not _REAL_RE.match(raw):
            raise CsvError(line, f"invalid REAL {raw!r} for column {column.name!r}")
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            raise CsvError(line, f"non-finite REAL {raw!r} for column {column.name!r}")
        return value
    raise AssertionError(column.declared_type)


class CsvRecordReader:
    """Incremental RFC-4180 reader that tracks physical line numbers."""

    def __init__(self, data: bytes, *, source: str = "member", encoding: str = "utf-8"):
        if encoding.lower().replace("-", "") != "utf8":
            raise CsvError(1, f"unsupported encoding {encoding!r}; only UTF-8 is accepted")
        try:
            self._text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CsvError(1, f"{source} is not valid UTF-8: {exc}") from exc
        if self._text.startswith("﻿"):
            raise CsvError(1, "UTF-8 BOM is not allowed")
        self._pos = 0
        self._length = len(self._text)
        self._line = 1
        self._source = source

    def _peek(self) -> str:
        return self._text[self._pos] if self._pos < self._length else ""

    def _consume_eol(self) -> None:
        ch = self._peek()
        if ch == "\r":
            if self._text[self._pos + 1 : self._pos + 2] not in ("\n", ""):
                raise CsvError(self._line, "bare CR is not a valid record terminator")
            self._pos += 1
            if self._peek() == "\n":
                self._pos += 1
            self._line += 1
        elif ch == "\n":
            self._pos += 1
            self._line += 1
        elif ch == "":
            pass
        else:
            raise AssertionError(ch)

    def __iter__(self) -> Iterator[Tuple[int, int, List[str]]]:
        record_number = 0
        while self._pos < self._length:
            record_number += 1
            record_line = self._line
            fields: List[str] = []
            while True:
                if self._peek() == '"':
                    self._pos += 1
                    buf: List[str] = []
                    while True:
                        if self._pos >= self._length:
                            raise CsvError(record_line, "unterminated quoted field")
                        ch = self._text[self._pos]
                        if ch == '"':
                            if self._text[self._pos + 1 : self._pos + 2] == '"':
                                buf.append('"')
                                self._pos += 2
                                continue
                            self._pos += 1
                            break
                        if ch == "\r":
                            if self._text[self._pos + 1 : self._pos + 2] == "\n":
                                buf.append("\r\n")
                                self._pos += 2
                            else:
                                buf.append("\r")
                                self._pos += 1
                            self._line += 1
                            continue
                        if ch == "\n":
                            buf.append("\n")
                            self._pos += 1
                            self._line += 1
                            continue
                        buf.append(ch)
                        self._pos += 1
                    if self._peek() not in (",", "\r", "\n", ""):
                        raise CsvError(
                            record_line, "unexpected character after closing quote"
                        )
                    fields.append("".join(buf))
                else:
                    start = self._pos
                    while self._pos < self._length and self._text[self._pos] not in (
                        ",",
                        "\r",
                        "\n",
                    ):
                        if self._text[self._pos] == '"':
                            raise CsvError(
                                record_line, "quote character inside an unquoted field"
                            )
                        self._pos += 1
                    fields.append(self._text[start : self._pos])
                if self._peek() == ",":
                    self._pos += 1
                    continue
                break
            self._consume_eol()
            yield record_number, record_line, fields


def parse_records(
    data: bytes,
    spec: TableSpec,
    *,
    source: str = "member",
    encoding: str = "utf-8",
) -> Iterator[Tuple[int, int, Tuple[Any, ...]]]:
    """Yield ``(physical_line, logical_record, converted_row)``.

    ``physical_line`` is the 1-based physical line where the record starts
    (the header is line 1).  ``logical_record`` is the 1-based data-record
    ordinal, which differs from the physical line when quoted fields contain
    embedded newlines.  Both are reported so an error can be located either
    way.
    """

    reader = CsvRecordReader(data, source=source, encoding=encoding)
    header: Optional[List[str]] = None
    for record_number, line, fields in reader:
        if header is None:
            header = fields
            expected = list(spec.column_names)
            if header != expected:
                raise CsvError(
                    line,
                    f"header {header!r} does not match declared columns {expected!r}",
                    record=None,
                )
            continue
        record = record_number - 1  # header was record 1
        if len(fields) != len(spec.columns):
            raise CsvError(
                line,
                f"expected {len(spec.columns)} fields, found {len(fields)}",
                record=record,
            )
        try:
            row = tuple(
                _convert(raw, column, line)
                for raw, column in zip(fields, spec.columns)
            )
        except CsvError as exc:
            exc.record = record
            raise
        yield line, record, row
    if header is None:
        raise CsvError(1, "CSV member is empty; expected a header row")
