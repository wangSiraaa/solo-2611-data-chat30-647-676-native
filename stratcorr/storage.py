"""SQLite helpers shared by the catalogue and repository services."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class StorageError(RuntimeError):
    pass


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a database with constraints and foreign keys enabled."""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def require_schema(conn: sqlite3.Connection, marker_table: str) -> None:
    if not table_exists(conn, marker_table):
        raise StorageError(
            "database is not initialised (missing table "
            f"{marker_table!r}); run the init command first"
        )


def fetch_all_dicts(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]
