"""SQLite-backed, updateable document search repository.

Guarantees
----------
* Stable ``doc_id`` with a monotonically increasing per-document ``revision``.
* ``replace`` and ``delete`` run in one ``BEGIN IMMEDIATE`` transaction.
  ``expected_revision`` is an optimistic guard: a stale snapshot raises
  :class:`StaleRevision` and changes nothing, so an old revision can never
  overwrite a newer value.  A missing document counts as revision 0.
* Named queries and named schemas persist across close/reopen.
* A *targeted* scalar equality index backs safe ``eq`` conjuncts of registered
  queries.  The index is only a candidate generator: ``search`` re-runs the
  exact same :mod:`jsonpath_service` engine used by :meth:`scan_all` on every
  candidate and keeps only documents whose hits match.  Stale or missing
  index rows can therefore cost time, never change the result.
* Index rows are rebuilt from scratch for a document on every write and are
  removed on delete.  A removed field can never leave an old index label
  behind, and a value that moves to a different array position does not
  inherit the old position's evidence because evidence is always recomputed
  from the current payload.

Why targeted atoms?
-------------------
Indexing every scalar leaf of every 100 KiB document with wildcard variants is
expensive and mostly useless.  Only equality atoms that actually appear in
registered queries are indexed.  Registering a query marks its atoms pending;
the first search (or an explicit ``ensure_indexes``) builds all pending atoms
in one document pass.  Writes after that maintain every known atom.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

from jsonpath_service.codec import path_from_json, path_to_json
from jsonpath_service.engine import (
    Hit,
    PathError,
    evaluate,
)

from .common import (
    ANY_VALUE,
    index_atoms_for_path,
    scalar_key,
)
from .indexing import AtomTrie

__all__ = [
    "Repository",
    "StaleRevision",
    "QueryConflict",
    "DocNotFound",
    "RepoError",
    "QueryDef",
    "SearchResult",
    "Explanation",
]

SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queries (
    name TEXT PRIMARY KEY,
    path_json TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS schemas (
    name TEXT PRIMARY KEY,
    definition_json TEXT NOT NULL,
    created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS index_atoms (
    atom TEXT PRIMARY KEY,
    path_json TEXT NOT NULL,
    ready INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS index_equal (
    atom TEXT NOT NULL,
    scalar TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    PRIMARY KEY (atom, scalar, doc_id)
);
CREATE INDEX IF NOT EXISTS index_equal_lookup ON index_equal(atom, scalar);
CREATE INDEX IF NOT EXISTS index_equal_doc ON index_equal(doc_id);
CREATE TABLE IF NOT EXISTS history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    op TEXT NOT NULL,
    payload TEXT
);
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RepoError(Exception):
    pass


class StaleRevision(RepoError):
    def __init__(self, doc_id: str, expected: int, actual: int) -> None:
        super().__init__(f"doc {doc_id!r}: expected revision {expected}, found {actual}")
        self.doc_id = doc_id
        self.expected = expected
        self.actual = actual


class QueryConflict(RepoError):
    pass


class DocNotFound(RepoError):
    pass


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(text: str) -> Any:
    return json.loads(text)


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryDef:
    name: str
    path: tuple[Any, ...]
    mode: str
    created_revision: int


@dataclass(frozen=True)
class SearchResult:
    doc_id: str
    revision: int
    hits: tuple[Hit, ...]

    def evidence(self) -> list[dict[str, Any]]:
        return [{"path": list(h.path), "value": h.value} for h in self.hits]


@dataclass(frozen=True)
class Explanation:
    query: str
    total_documents: int
    candidate_documents: int
    matched_documents: int
    used_index: bool
    index_plan: tuple[dict[str, Any], ...]
    matches: tuple[SearchResult, ...]
    errors: tuple[tuple[str, str], ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "total_documents": self.total_documents,
            "candidate_documents": self.candidate_documents,
            "matched_documents": self.matched_documents,
            "used_index": self.used_index,
            "index_plan": list(self.index_plan),
            "matches": [
                {"doc_id": m.doc_id, "revision": m.revision, "hits": m.evidence()}
                for m in self.matches
            ],
            "errors": [{"doc_id": d, "error": e} for d, e in self.errors],
        }


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class Repository:
    def __init__(self, path: str, *, busy_timeout_ms: int = 60000) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(
            path,
            isolation_level=None,
            timeout=busy_timeout_ms / 1000,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_DDL)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RepoError(
                    f"unsupported schema version {row['value']} (expected {SCHEMA_VERSION})"
                )
        self._trie: AtomTrie | None = None
        self._trie_signature: tuple[str, ...] | None = None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    @contextmanager
    def _read_tx(self) -> Iterator[sqlite3.Connection]:
        """Consistent read snapshot; WAL readers do not block writers."""

        conn = self._conn
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # -- writes ------------------------------------------------------------

    def replace(
        self,
        doc_id: str,
        document: Any,
        *,
        expected_revision: int | None = None,
        history_payload: bool = True,
    ) -> int:
        """Insert or replace ``doc_id``; return the new revision."""

        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError("doc_id must be a non-empty string")
        payload = _dumps(document)
        with self._tx() as conn:
            actual = self._current_revision_of(conn, doc_id)
            if expected_revision is not None and expected_revision != actual:
                raise StaleRevision(doc_id, expected_revision, actual)
            revision = actual + 1
            conn.execute(
                "INSERT INTO documents(doc_id, revision, payload) VALUES(?,?,?) "
                "ON CONFLICT(doc_id) DO UPDATE SET "
                "revision=excluded.revision, payload=excluded.payload",
                (doc_id, revision, payload),
            )
            self._reindex_document(conn, doc_id, document)
            conn.execute(
                "INSERT INTO history(doc_id, revision, op, payload) VALUES(?,?,?,?)",
                (doc_id, revision, "replace", payload if history_payload else None),
            )
            return revision

    def delete(self, doc_id: str, *, expected_revision: int | None = None) -> int:
        """Delete ``doc_id``; return the removed revision."""

        with self._tx() as conn:
            row = conn.execute(
                "SELECT revision FROM documents WHERE doc_id=?", (doc_id,)
            ).fetchone()
            if row is None:
                if expected_revision not in (None, 0):
                    raise StaleRevision(doc_id, expected_revision, 0)
                raise DocNotFound(doc_id)
            actual = int(row["revision"])
            if expected_revision is not None and expected_revision != actual:
                raise StaleRevision(doc_id, expected_revision, actual)
            conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM index_equal WHERE doc_id=?", (doc_id,))
            conn.execute(
                "INSERT INTO history(doc_id, revision, op, payload) VALUES(?,?,?,NULL)",
                (doc_id, actual + 1, "delete"),
            )
            return actual

    def bulk_load(
        self,
        documents: Iterable[tuple[str, Any]],
        *,
        chunk_size: int = 250,
        history_payload: bool = False,
    ) -> int:
        """Load many documents in chunked transactions with the same invariants."""

        batch: list[tuple[str, Any, str]] = []
        loaded = 0
        for doc_id, document in documents:
            if not isinstance(doc_id, str) or not doc_id:
                raise ValueError("doc_id must be a non-empty string")
            batch.append((doc_id, document, _dumps(document)))
            if len(batch) >= chunk_size:
                loaded += self._bulk_chunk(batch, history_payload)
                batch.clear()
        if batch:
            loaded += self._bulk_chunk(batch, history_payload)
        return loaded

    def _bulk_chunk(
        self, batch: list[tuple[str, Any, str]], history_payload: bool
    ) -> int:
        with self._tx() as conn:
            for doc_id, document, payload in batch:
                actual = self._current_revision_of(conn, doc_id)
                revision = actual + 1
                conn.execute(
                    "INSERT INTO documents(doc_id, revision, payload) VALUES(?,?,?) "
                    "ON CONFLICT(doc_id) DO UPDATE SET "
                    "revision=excluded.revision, payload=excluded.payload",
                    (doc_id, revision, payload),
                )
                self._reindex_document(conn, doc_id, document)
                conn.execute(
                    "INSERT INTO history(doc_id, revision, op, payload) VALUES(?,?,?,?)",
                    (doc_id, revision, "replace", payload if history_payload else None),
                )
            return len(batch)

    @staticmethod
    def _current_revision_of(conn: sqlite3.Connection, doc_id: str) -> int:
        row = conn.execute(
            "SELECT revision FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return 0 if row is None else int(row["revision"])

    # -- index maintenance -------------------------------------------------

    def _atom_paths(
        self, conn: sqlite3.Connection
    ) -> list[tuple[str, tuple[Any, ...]]]:
        return [
            (r["atom"], tuple(path_from_json(_loads(r["path_json"]))))
            for r in conn.execute("SELECT atom, path_json FROM index_atoms ORDER BY atom")
        ]

    def _trie_for(self, atoms: list[tuple[str, tuple[Any, ...]]]) -> AtomTrie:
        signature = tuple(atom for atom, _path in atoms)
        if self._trie is None or self._trie_signature != signature:
            self._trie = AtomTrie((atom, path) for atom, path in atoms)
            self._trie_signature = signature
        return self._trie

    def _reindex_document(
        self, conn: sqlite3.Connection, doc_id: str, document: Any
    ) -> None:
        """Rebuild every index row for one document from its current value."""

        conn.execute("DELETE FROM index_equal WHERE doc_id=?", (doc_id,))
        atoms = self._atom_paths(conn)
        if not atoms:
            return
        trie = self._trie_for(atoms)
        extracted = trie.extract(document)
        rows: list[tuple[str, str, str]] = []
        for atom, _path in atoms:
            values = extracted.get(atom)
            if atom not in extracted:
                continue
            if values is None:
                rows.append((atom, ANY_VALUE, doc_id))
            else:
                for value in values:
                    rows.append((atom, value, doc_id))
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO index_equal(atom, scalar, doc_id) VALUES(?,?,?)",
                rows,
            )

    def ensure_indexes(self) -> int:
        """Build any pending atoms in one document pass; returns docs scanned.

        Runs in a single write transaction so the document snapshot and the
        index rows derived from it are consistent even with concurrent writers.
        """

        with self._tx() as conn:
            atoms = self._atom_paths(conn)
            if not atoms:
                return 0
            pending = {
                r["atom"]
                for r in conn.execute(
                    "SELECT atom FROM index_atoms WHERE ready=0"
                ).fetchall()
            }
            if not pending:
                return 0
            trie = self._trie_for(atoms)
            conn.executemany(
                "DELETE FROM index_equal WHERE atom=?", [(a,) for a in pending]
            )
            docs = conn.execute(
                "SELECT doc_id, payload FROM documents ORDER BY doc_id"
            ).fetchall()
            rows: list[tuple[str, str, str]] = []
            for doc in docs:
                doc_id = doc["doc_id"]
                extracted = trie.extract(_loads(doc["payload"]))
                for atom in pending:
                    if atom not in extracted:
                        continue
                    values = extracted[atom]
                    if values is None:
                        rows.append((atom, ANY_VALUE, doc_id))
                    else:
                        for value in values:
                            rows.append((atom, value, doc_id))
                if len(rows) >= 50_000:
                    conn.executemany(
                        "INSERT OR IGNORE INTO index_equal(atom, scalar, doc_id) "
                        "VALUES(?,?,?)",
                        rows,
                    )
                    rows.clear()
            if rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO index_equal(atom, scalar, doc_id) VALUES(?,?,?)",
                    rows,
                )
            conn.executemany(
                "UPDATE index_atoms SET ready=1 WHERE atom=?", [(a,) for a in pending]
            )
            return len(docs)

    # -- reads -------------------------------------------------------------

    def get(self, doc_id: str) -> tuple[int, Any] | None:
        row = self._conn.execute(
            "SELECT revision, payload FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        if row is None:
            return None
        return int(row["revision"]), _loads(row["payload"])

    def document_ids(self) -> list[str]:
        return [
            r["doc_id"]
            for r in self._conn.execute("SELECT doc_id FROM documents ORDER BY doc_id")
        ]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def iter_documents(self) -> Iterator[tuple[str, int, Any]]:
        for row in self._conn.execute(
            "SELECT doc_id, revision, payload FROM documents ORDER BY doc_id"
        ):
            yield row["doc_id"], int(row["revision"]), _loads(row["payload"])

    def iter_raw_documents(self) -> Iterator[tuple[str, int, str]]:
        """Stream ``(doc_id, revision, payload_json)`` without parsing."""

        for row in self._conn.execute(
            "SELECT doc_id, revision, payload FROM documents ORDER BY doc_id"
        ):
            yield row["doc_id"], int(row["revision"]), row["payload"]

    def history(self, doc_id: str | None = None) -> list[dict[str, Any]]:
        if doc_id is None:
            rows = self._conn.execute("SELECT * FROM history ORDER BY seq").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM history WHERE doc_id=? ORDER BY seq", (doc_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- query / schema registration --------------------------------------

    def register_query(
        self, name: str, path: Sequence[Any], *, mode: str = "lax"
    ) -> QueryDef:
        if not isinstance(name, str) or not name:
            raise ValueError("query name must be a non-empty string")
        if mode not in ("lax", "strict"):
            raise ValueError("mode must be 'lax' or 'strict'")
        path_json = _dumps(path_to_json(path))
        atoms = index_atoms_for_path(path)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT path_json, mode, created_revision FROM queries WHERE name=?",
                (name,),
            ).fetchone()
            if row is not None:
                if row["path_json"] != path_json or row["mode"] != mode:
                    raise QueryConflict(
                        f"query {name!r} already registered with a different definition"
                    )
                return QueryDef(name, tuple(path), mode, int(row["created_revision"]))
            for atom, atom_path, _scalar in atoms:
                conn.execute(
                    "INSERT OR IGNORE INTO index_atoms(atom, path_json, ready) "
                    "VALUES(?,?,0)",
                    (atom, _dumps(path_to_json(atom_path))),
                )
            revision = self._global_document_revision(conn)
            conn.execute(
                "INSERT INTO queries(name, path_json, mode, created_revision) "
                "VALUES(?,?,?,?)",
                (name, path_json, mode, revision),
            )
            self._trie = None
            return QueryDef(name, tuple(path), mode, revision)

    def get_query(self, name: str) -> QueryDef:
        row = self._conn.execute(
            "SELECT * FROM queries WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise QueryConflict(f"unknown query {name!r}")
        path = tuple(path_from_json(_loads(row["path_json"])))
        return QueryDef(name, path, row["mode"], int(row["created_revision"]))

    def list_queries(self) -> list[str]:
        return [
            r["name"]
            for r in self._conn.execute("SELECT name FROM queries ORDER BY name")
        ]

    def register_schema(self, name: str, definition: Any) -> int:
        definition_json = _dumps(definition)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT definition_json, created_revision FROM schemas WHERE name=?",
                (name,),
            ).fetchone()
            if row is not None:
                if row["definition_json"] != definition_json:
                    raise QueryConflict(
                        f"schema {name!r} already registered with a different definition"
                    )
                return int(row["created_revision"])
            revision = self._global_document_revision(conn)
            conn.execute(
                "INSERT INTO schemas(name, definition_json, created_revision) "
                "VALUES(?,?,?)",
                (name, definition_json, revision),
            )
            return revision

    def get_schema(self, name: str) -> tuple[Any, int] | None:
        row = self._conn.execute(
            "SELECT definition_json, created_revision FROM schemas WHERE name=?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return _loads(row["definition_json"]), int(row["created_revision"])

    def list_schemas(self) -> list[str]:
        return [
            r["name"]
            for r in self._conn.execute("SELECT name FROM schemas ORDER BY name")
        ]

    @staticmethod
    def _global_document_revision(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS r FROM documents"
        ).fetchone()
        return int(row["r"])

    # -- candidates --------------------------------------------------------

    def _candidate_set(
        self, conn: sqlite3.Connection, query: QueryDef
    ) -> tuple[list[str] | None, list[dict[str, Any]], str | None]:
        """Candidate doc ids (``None`` = full scan), plan, and a skip reason."""

        if query.mode == "strict":
            # A strict query reports one locatable error per offending document,
            # so narrowing the scan would hide errors.  The engine defines the
            # result; the index never does.
            return None, [], "strict queries always full-scan to report all errors"

        atoms = index_atoms_for_path(query.path)
        if not atoms:
            return None, [], "no safe scalar equality atom in this query"

        unique: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for atom, _atom_path, scalar in atoms:
            key = (atom, scalar_key(scalar))
            if key in seen:
                continue
            seen.add(key)
            unique.append(key)

        plan: list[dict[str, Any]] = []
        per_atom: list[set[str]] = []
        for atom, scalar in unique:
            ready_row = conn.execute(
                "SELECT ready FROM index_atoms WHERE atom=?", (atom,)
            ).fetchone()
            if ready_row is None or int(ready_row["ready"]) == 0:
                return None, [], "index atom is pending; call ensure_indexes()"
            rows = conn.execute(
                "SELECT doc_id FROM index_equal WHERE atom=? AND scalar IN (?, ?)",
                (atom, scalar, ANY_VALUE),
            ).fetchall()
            ids = {r["doc_id"] for r in rows}
            per_atom.append(ids)
            plan.append(
                {
                    "kind": "index_equal",
                    "atom": _loads(atom),
                    "scalar": scalar,
                    "documents": len(ids),
                }
            )
        candidates = set(per_atom[0])
        for other in per_atom[1:]:
            candidates &= other
        return sorted(candidates), plan, None

    # -- search ------------------------------------------------------------

    def search(self, name: str) -> list[SearchResult]:
        return list(self.iter_search(name))

    def iter_search(self, name: str) -> Iterator[SearchResult]:
        """Yield exact matches for a registered query, doc_id order.

        Streaming variant of :meth:`search`; the index is still only used to
        choose candidates and every candidate is re-evaluated by the engine.
        Strict-mode errors are skipped here; use :meth:`search_with_errors`.
        """

        for kind, payload in self.iter_search_events(name):
            if kind == "match":
                yield payload

    def iter_search_events(self, name: str) -> Iterator[tuple[str, Any]]:
        """Stream ``("match", SearchResult)`` and ``("error", (doc_id, msg))``.

        One pass over the candidates yields both outcomes, so strict-mode error
        evidence and matches always come from the same snapshot.
        """

        query = self.get_query(name)
        with self._read_tx() as conn:
            candidates, _plan, _reason = self._candidate_set(conn, query)
            yield from _iter_events(_iter_rows(conn, candidates), query)

    def iter_scan_all(
        self, path: Sequence[Any], *, mode: str = "lax"
    ) -> Iterator[SearchResult]:
        """Streaming index-free reference; uses the same engine as search."""

        query = QueryDef("<scan>", tuple(path), mode, 0)
        with self._read_tx() as conn:
            yield from _iter_exact(_iter_rows(conn, None), query)

    def search_with_errors(
        self, name: str
    ) -> tuple[list[SearchResult], list[tuple[str, str]]]:
        query = self.get_query(name)
        with self._read_tx() as conn:
            candidates, _plan, _reason = self._candidate_set(conn, query)
            return _exact_rows(_iter_rows(conn, candidates), query)

    def scan_all(
        self, path: Sequence[Any], *, mode: str = "lax"
    ) -> tuple[list[SearchResult], list[tuple[str, str]]]:
        """Index-free reference.  Uses the same engine as :meth:`search`."""

        query = QueryDef("<scan>", tuple(path), mode, 0)
        with self._read_tx() as conn:
            return _exact_rows(_iter_rows(conn, None), query)

    def explain(self, name: str) -> Explanation:
        query = self.get_query(name)
        with self._read_tx() as conn:
            total = int(
                conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            )
            candidates, plan, reason = self._candidate_set(conn, query)
            results, errors = _exact_rows(_iter_rows(conn, candidates), query)
        if reason is not None:
            if plan:
                plan = plan + [{"kind": "note", "detail": reason}]
            else:
                plan = [{"kind": "scan", "reason": reason}]
        return Explanation(
            query=name,
            total_documents=total,
            candidate_documents=total if candidates is None else len(candidates),
            matched_documents=len(results),
            used_index=candidates is not None,
            index_plan=tuple(plan),
            matches=tuple(results),
            errors=tuple(errors),
        )

    # -- maintenance -------------------------------------------------------

    def rebuild_indexes(self) -> int:
        """Drop and rebuild every index row and mark all atoms pending."""

        with self._tx() as conn:
            conn.execute("DELETE FROM index_equal")
            conn.execute("UPDATE index_atoms SET ready=0")
            self._trie = None
            self._trie_signature = None
        return self.ensure_indexes()

    def index_stats(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM index_equal").fetchone()
        atoms = self._conn.execute(
            "SELECT atom, ready FROM index_atoms ORDER BY atom"
        ).fetchall()
        return {
            "documents": self.count(),
            "rows": int(row["n"]),
            "atoms": [
                {"atom": _loads(r["atom"]), "ready": bool(r["ready"])}
                for r in atoms
            ],
        }


# ---------------------------------------------------------------------------
# Helpers used inside read snapshots
# ---------------------------------------------------------------------------


def _iter_rows(
    conn: sqlite3.Connection,
    candidates: list[str] | None,
    *,
    chunk_size: int = 500,
) -> Iterator[sqlite3.Row]:
    """Stream document rows in ``doc_id`` order without materialising payloads.

    Candidate lists are chunked so the SQLite host-parameter limit is never
    approached; full scans simply stream the cursor.
    """

    if candidates is None:
        yield from conn.execute(
            "SELECT doc_id, revision, payload FROM documents ORDER BY doc_id"
        )
        return
    for start in range(0, len(candidates), chunk_size):
        chunk = candidates[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        yield from conn.execute(
            f"SELECT doc_id, revision, payload FROM documents "
            f"WHERE doc_id IN ({placeholders}) ORDER BY doc_id",
            chunk,
        )


def _iter_events(
    rows: Iterable[sqlite3.Row], query: QueryDef
) -> Iterator[tuple[str, Any]]:
    """Single-pass evaluation yielding matches and strict-mode errors."""

    strict = query.mode == "strict"
    for row in rows:
        doc_id = row["doc_id"]
        document = _loads(row["payload"])
        try:
            hits = evaluate(document, query.path, strict=strict)
        except PathError as exc:
            yield "error", (doc_id, str(exc))
            continue
        if hits:
            yield "match", SearchResult(doc_id, int(row["revision"]), tuple(hits))


def _iter_exact(
    rows: Iterable[sqlite3.Row], query: QueryDef
) -> Iterator[SearchResult]:
    for kind, payload in _iter_events(rows, query):
        if kind == "match":
            yield payload


def _exact_rows(
    rows: Iterable[sqlite3.Row], query: QueryDef
) -> tuple[list[SearchResult], list[tuple[str, str]]]:
    results: list[SearchResult] = []
    errors: list[tuple[str, str]] = []
    for kind, payload in _iter_events(rows, query):
        if kind == "match":
            results.append(payload)
        else:
            errors.append(payload)
    return results, errors
