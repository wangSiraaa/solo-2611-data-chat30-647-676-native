"""Transactional CSV-table ingestion from a validated ZIP archive.

A load manifest binds an archive SHA-256 and explicit entry ids.  Every table
lists the member entry ids that form it, in order.  Each table is one SQLite
transaction: either the whole table (all of its members) commits, or nothing
from that table remains.  Other tables that committed earlier survive.

Bodies are read through :class:`~archive_service.zip_service.ArchiveReader`
*by entry id*; the reader verifies the range, explicit lengths and CRC32 on
every read.  A duplicate member name in the archive therefore cannot cause the
wrong body to be loaded.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .csv_reader import CsvError, TableSpec, make_table_spec, parse_records
from .zip_service import (
    ArchiveReader,
    EntryReadError,
    EntryNotFound,
)

_INSERT_BATCH = 1000

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS _zs_ingest (
    archive_sha256 TEXT NOT NULL,
    table_name     TEXT NOT NULL,
    entry_ids_json TEXT NOT NULL,
    row_count      INTEGER NOT NULL,
    member_count   INTEGER NOT NULL,
    loaded_at      TEXT NOT NULL,
    PRIMARY KEY (archive_sha256, table_name)
);
CREATE TABLE IF NOT EXISTS _zs_row_source (
    archive_sha256 TEXT NOT NULL,
    table_name     TEXT NOT NULL,
    rowid_value    INTEGER NOT NULL,
    entry_id       TEXT NOT NULL,
    source_line    INTEGER NOT NULL,
    PRIMARY KEY (archive_sha256, table_name, rowid_value)
);
"""


class ManifestError(Exception):
    """The load manifest is malformed or does not bind to the archive."""


class TableLoadError(Exception):
    """One table failed; the message carries the entry id and line number."""

    def __init__(
        self,
        table_name: str,
        entry_id: Optional[str],
        line: Optional[int],
        message: str,
        record: Optional[int] = None,
    ):
        self.table_name = table_name
        self.entry_id = entry_id
        self.line = line
        self.record = record
        self.message = message
        location = f"{table_name}"
        if entry_id is not None:
            location += f" entry {entry_id}"
        if line is not None:
            location += f" line {line}"
        if record is not None:
            location += f" record {record}"
        super().__init__(f"{location}: {message}")

    def as_dict(self) -> dict:
        return {
            "table": self.table_name,
            "entry_id": self.entry_id,
            "line": self.line,
            "record": self.record,
            "message": self.message,
        }


@dataclass(frozen=True)
class TableLoad:
    spec: TableSpec
    entry_ids: Tuple[str, ...]
    encoding: str = "utf-8"


@dataclass(frozen=True)
class LoadManifest:
    archive_sha256: str
    tables: Tuple[TableLoad, ...]


@dataclass
class IngestReport:
    archive_sha256: str
    committed_tables: List[str] = field(default_factory=list)
    failed_tables: List[dict] = field(default_factory=list)
    rows_by_table: Dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed_tables

    def as_dict(self) -> dict:
        return {
            "archive_sha256": self.archive_sha256,
            "ok": self.ok,
            "committed_tables": list(self.committed_tables),
            "failed_tables": list(self.failed_tables),
            "rows_by_table": dict(self.rows_by_table),
        }


def load_manifest_from_dict(payload: dict) -> LoadManifest:
    if not isinstance(payload, dict):
        raise ManifestError("manifest must be a JSON object")
    archive_sha = payload.get("archive_sha256")
    if not isinstance(archive_sha, str) or len(archive_sha) != 64:
        raise ManifestError("manifest needs a 64-hex archive_sha256")
    try:
        int(archive_sha, 16)
    except ValueError:
        raise ManifestError("archive_sha256 is not hexadecimal") from None
    tables_payload = payload.get("tables")
    if not isinstance(tables_payload, list) or not tables_payload:
        raise ManifestError("manifest needs a non-empty tables list")
    seen_tables = set()
    tables: List[TableLoad] = []
    for raw in tables_payload:
        if not isinstance(raw, dict):
            raise ManifestError("each table entry must be an object")
        name = raw.get("table")
        if not isinstance(name, str):
            raise ManifestError("table name must be a string")
        folded = name.casefold()
        if folded in seen_tables:
            raise ManifestError(f"duplicate table {name!r} in manifest")
        if name.startswith("_zs_") or folded in {"sqlite_master", "sqlite_schema"}:
            raise ManifestError(f"table name {name!r} is reserved")
        seen_tables.add(folded)
        columns_raw = raw.get("columns")
        if not isinstance(columns_raw, list):
            raise ManifestError(f"table {name!r} needs a columns list")
        columns = []
        for col in columns_raw:
            if (
                not isinstance(col, list)
                or len(col) != 3
                or not isinstance(col[0], str)
                or not isinstance(col[1], str)
            ):
                raise ManifestError(
                    f"table {name!r} columns must be [name, type, primary_key]"
                )
            columns.append((col[0], col[1], bool(col[2])))
        try:
            spec = make_table_spec(name, columns)
        except ValueError as exc:
            raise ManifestError(str(exc)) from exc
        entry_ids = raw.get("entry_ids")
        if (
            not isinstance(entry_ids, list)
            or not entry_ids
            or not all(isinstance(e, str) and e for e in entry_ids)
        ):
            raise ManifestError(f"table {name!r} needs a non-empty entry_ids list")
        if len(set(entry_ids)) != len(entry_ids):
            raise ManifestError(f"table {name!r} lists an entry id twice")
        encoding = raw.get("encoding", "utf-8")
        if not isinstance(encoding, str):
            raise ManifestError(f"table {name!r} encoding must be a string")
        if encoding.replace("-", "").replace("_", "").lower() != "utf8":
            raise ManifestError(
                f"table {name!r} declares {encoding!r}; only UTF-8 is supported"
            )
        tables.append(
            TableLoad(spec=spec, entry_ids=tuple(entry_ids), encoding="utf-8")
        )
    return LoadManifest(archive_sha256=archive_sha, tables=tuple(tables))


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _create_table(conn: sqlite3.Connection, spec: TableSpec) -> None:
    names = {c.name.casefold() for c in spec.columns}
    if any(n.startswith("_zs_") for n in names):
        raise TableLoadError(
            spec.table_name, None, None, "column names may not start with _zs_"
        )
    pk = [c.name for c in spec.primary_key]
    definitions = ", ".join(
        f"{_quote_identifier(c.name)} {c.sql_type}" for c in spec.columns
    )
    # STRICT keeps SQLite from silently coercing a value into another type.
    try:
        conn.execute(
            f"CREATE TABLE {_quote_identifier(spec.table_name)} ({definitions}) STRICT"
        )
    except sqlite3.OperationalError:
        # Older SQLite builds lack STRICT; the declared affinity plus the
        # strict CSV converter still enforce the declared types.
        conn.execute(
            f"CREATE TABLE {_quote_identifier(spec.table_name)} ({definitions})"
        )
    unique_name = f"_zs_pk_{spec.table_name}"
    conn.execute(
        f"CREATE UNIQUE INDEX {_quote_identifier(unique_name)} ON "
        f"{_quote_identifier(spec.table_name)} "
        f"({', '.join(_quote_identifier(n) for n in pk)})"
    )


class Warehouse:
    """SQLite-backed ingestion with per-table transactional batches."""

    def __init__(self, db_path: os.PathLike | str):
        self.db_path = os.fspath(db_path)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_LEDGER_DDL)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def ingest(self, reader: ArchiveReader, manifest: LoadManifest) -> IngestReport:
        if reader.archive_sha256 != manifest.archive_sha256:
            raise ManifestError(
                "archive sha256 does not match the manifest; refusing to load"
            )
        report = IngestReport(archive_sha256=manifest.archive_sha256)

        # Resolve every listed entry id before touching the database.  A bad
        # id fails only that table, but it is detected up front.
        usable: List[TableLoad] = []
        for table in manifest.tables:
            name = table.spec.table_name
            missing = []
            for entry_id in table.entry_ids:
                try:
                    info = reader.get(entry_id)
                except EntryNotFound:
                    missing.append(entry_id)
                    continue
                if info.archive_sha256 != manifest.archive_sha256:
                    missing.append(entry_id)
            if missing:
                report.failed_tables.append(
                    TableLoadError(
                        name, missing[0], None, "entry id is absent from this archive"
                    ).as_dict()
                )
            else:
                usable.append(table)

        # Verify every member body once, up front.  CRC corruption is charged
        # to the affected table; any structural/length/limit contradiction
        # rejects the entire archive before anything is written.  When the
        # archive view was already fully verified at open time this reuses the
        # recorded results (and the central-directory fingerprint still guards
        # against the file changing in between).
        crc_failures = reader.preflight()
        for table in usable:
            name = table.spec.table_name
            bad = next(
                (e for e in table.entry_ids if e in crc_failures), None
            )
            if bad is not None:
                exc = crc_failures[bad]
                report.failed_tables.append(
                    TableLoadError(name, bad, None, exc.message).as_dict()
                )
                continue
            try:
                rows = self._load_table(reader, table, manifest.archive_sha256)
            except TableLoadError as exc:
                report.failed_tables.append(exc.as_dict())
            else:
                report.committed_tables.append(name)
                report.rows_by_table[name] = rows
        return report

    def _load_table(
        self, reader: ArchiveReader, table: TableLoad, archive_sha: str
    ) -> int:
        spec = table.spec
        name = spec.table_name
        if self._table_exists(name):
            raise TableLoadError(name, None, None, "table already exists; refusing to replace it")

        column_list = ", ".join(_quote_identifier(c.name) for c in spec.columns)
        placeholders = ", ".join("?" for _ in spec.columns)
        insert_sql = (
            f"INSERT INTO {_quote_identifier(name)} ({column_list}) "
            f"VALUES ({placeholders})"
        )
        pk_indexes = [spec.columns.index(column) for column in spec.primary_key]
        seen_pk: Dict[Tuple[Any, ...], Tuple[str, int]] = {}
        committed_rows: List[Tuple[str, int]] = []
        row_count = 0

        # The table and its rows form one SQLite transaction.  Each member is
        # decompressed exactly once (after the archive-level preflight) and its
        # rows are inserted inside the open transaction; any bad row, bad
        # checksum or duplicate key rolls the whole table back.
        #
        # Each member is fully parsed before its *single* insert statement is
        # issued.  SQLite's INSERT ... VALUES (...) has a 32766-expression
        # limit, so a large member is split into executemany-sized batches
        # without ever opening a committed intermediate state.
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            _create_table(self.conn, spec)
            for entry_id in table.entry_ids:
                # collect the member's verified body, never more than 8 MiB
                member = bytearray()
                try:
                    for chunk in reader.iter_entry_chunks(entry_id):
                        member.extend(chunk)
                except EntryReadError as exc:
                    raise TableLoadError(name, entry_id, None, exc.message) from exc
                try:
                    records = list(
                        parse_records(
                            bytes(member), spec, source=entry_id, encoding=table.encoding
                        )
                    )
                except CsvError as exc:
                    raise TableLoadError(
                        name, entry_id, exc.line, exc.message, record=exc.record
                    ) from exc
                batch: List[Tuple[Any, ...]] = []
                for line, record_number, row in records:
                    key = tuple(row[i] for i in pk_indexes)
                    if key in seen_pk:
                        first_entry, first_line = seen_pk[key]
                        raise TableLoadError(
                            name,
                            entry_id,
                            line,
                            f"duplicate primary key {key!r} "
                            f"(first seen at {first_entry} line {first_line})",
                            record=record_number,
                        )
                    seen_pk[key] = (entry_id, line)
                    batch.append(row)
                    committed_rows.append((entry_id, line))
                for offset in range(0, len(batch), _INSERT_BATCH):
                    self.conn.executemany(insert_sql, batch[offset : offset + _INSERT_BATCH])
                row_count += len(batch)
            self.conn.executemany(
                "INSERT INTO _zs_ingest "
                "(archive_sha256, table_name, entry_ids_json, row_count, member_count, loaded_at) "
                "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                [
                    (
                        archive_sha,
                        name,
                        json.dumps(list(table.entry_ids), ensure_ascii=False),
                        row_count,
                        len(table.entry_ids),
                    )
                ],
            )
            # Member tables have no declared INTEGER PRIMARY KEY, so rowid is
            # assigned in insertion order; map provenance accordingly.
            rowids = [
                r[0]
                for r in self.conn.execute(
                    f"SELECT rowid FROM {_quote_identifier(name)} ORDER BY rowid"
                )
            ]
            if len(rowids) != len(committed_rows):
                raise TableLoadError(name, None, None, "internal rowid/source count mismatch")
            self.conn.executemany(
                "INSERT INTO _zs_row_source "
                "(archive_sha256, table_name, rowid_value, entry_id, source_line) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (archive_sha, name, rowid, entry_id, line)
                    for rowid, (entry_id, line) in zip(rowids, committed_rows)
                ],
            )
            self.conn.execute("COMMIT")
        except BaseException as exc:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, TableLoadError):
                raise
            raise TableLoadError(name, None, None, f"database error: {exc}") from exc
        return row_count

    def table_rows(self, table_name: str) -> List[Tuple[Any, ...]]:
        info = self.table_info(table_name)
        columns = ", ".join(_quote_identifier(c) for c in info["columns"])
        cursor = self.conn.execute(
            f"SELECT {columns} FROM {_quote_identifier(table_name)} ORDER BY rowid"
        )
        return [tuple(row) for row in cursor]

    def table_info(self, table_name: str) -> dict:
        row = self.conn.execute(
            "SELECT archive_sha256, entry_ids_json, row_count, member_count, loaded_at "
            "FROM _zs_ingest WHERE table_name=?",
            (table_name,),
        ).fetchone()
        if row is None:
            raise KeyError(f"table {table_name!r} was not ingested")
        cursor = self.conn.execute(
            f"PRAGMA table_info({_quote_identifier(table_name)})"
        )
        columns = [r[1] for r in cursor]
        return {
            "table": table_name,
            "archive_sha256": row[0],
            "entry_ids": json.loads(row[1]),
            "row_count": row[2],
            "member_count": row[3],
            "loaded_at": row[4],
            "columns": columns,
        }

    def provenance(self, table_name: str, rowid_value: int) -> Tuple[str, int]:
        row = self.conn.execute(
            "SELECT entry_id, source_line FROM _zs_row_source "
            "WHERE table_name=? AND rowid_value=?",
            (table_name, rowid_value),
        ).fetchone()
        if row is None:
            raise KeyError("no provenance for that row")
        return row[0], row[1]

    def all_provenance(self, table_name: str) -> List[Tuple[int, str, int]]:
        cursor = self.conn.execute(
            "SELECT rowid_value, entry_id, source_line FROM _zs_row_source "
            "WHERE table_name=? ORDER BY rowid_value",
            (table_name,),
        )
        return [tuple(r) for r in cursor]

    def tables(self) -> List[str]:
        cursor = self.conn.execute(
            "SELECT table_name FROM _zs_ingest ORDER BY table_name"
        )
        return [r[0] for r in cursor]
