"""SQLite persistence for raw observations, jump events and public arc products.

The store keeps three kinds of rows:

* ``observations`` -- per (station, channel, epoch) the raw rational ``phase``
  and boolean ``lock``.  This is the *only* place raw phase lives; the
  differential warehouse never touches it.
* ``jump_events`` -- explicit integer cycle-slip events
  ``(station, channel, epoch, jump)``.  Events can be corrected by superseding
  them; each arc-product version pins the exact set of event ids it used.
* ``arc_products`` / ``arc_segments`` -- immutable, versioned public products.
  A product records the correction level (max event id) and the ``G`` gap used,
  so the differential warehouse can detect when a product is stale with respect
  to the observations/events it consumed.

Rationals are stored losslessly as ``"num/den"`` text.
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from typing import Iterable, Iterator, Optional, Sequence

from .rational import Q

__all__ = ["Store"]


def _enc(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _dec(text: str) -> Fraction:
    num, den = text.split("/")
    return Fraction(int(num), int(den))


_CUT_REASONS = (
    "NO_LOCK",      # observation has lock=false, unusable
    "GAP",          # neighbouring usable epochs differ by more than G
    "JUMP",         # an explicit cycle-slip event starts here
    "NO_DATA",      # observation absent at an epoch; cannot carry arc across
    "FEED_START",   # first usable epoch after a break in the feed
    "TERMINAL",     # observation cut because next epoch required a break
    "WINDOW_END",   # next usable epoch is outside the requested window
)


class Store:
    """A thin, exact-valued wrapper around a SQLite database file (or ``:memory:``)."""

    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS observations (
                station TEXT NOT NULL,
                channel INTEGER NOT NULL,
                epoch   INTEGER NOT NULL,
                phase   TEXT NOT NULL,        -- "num/den" rational weeks
                lock    INTEGER NOT NULL,     -- 1 = usable, 0 = not usable
                PRIMARY KEY (station, channel, epoch)
            );

            CREATE TABLE IF NOT EXISTS jump_events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                station  TEXT NOT NULL,
                channel  INTEGER NOT NULL,
                epoch    INTEGER NOT NULL,
                jump     INTEGER NOT NULL,
                superseded INTEGER NOT NULL DEFAULT 0  -- 1 = replaced by a correction
            );

            CREATE INDEX IF NOT EXISTS idx_jump_event
                ON jump_events (station, channel, epoch, superseded);

            CREATE TABLE IF NOT EXISTS arc_products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                station     TEXT NOT NULL,
                channel     INTEGER NOT NULL,
                gap         INTEGER NOT NULL,
                event_level INTEGER NOT NULL,
                obs_stamp   TEXT NOT NULL DEFAULT '',
                jump_stamp  TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS arc_segments (
                product_id   INTEGER NOT NULL,
                seg_order    INTEGER NOT NULL,
                arc_id       INTEGER NOT NULL,
                start_reason TEXT NOT NULL,
                end_reason   TEXT NOT NULL DEFAULT '',
                source_event INTEGER,
                start_epoch  INTEGER NOT NULL,
                end_epoch    INTEGER NOT NULL,
                n_epochs     INTEGER NOT NULL,
                PRIMARY KEY (product_id, seg_order)
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                rover      TEXT NOT NULL,
                base       TEXT NOT NULL,
                channel1   INTEGER NOT NULL,
                channel2   INTEGER NOT NULL,
                lambda1    TEXT NOT NULL,
                lambda2    TEXT NOT NULL,
                win_lo     INTEGER NOT NULL,
                win_hi     INTEGER NOT NULL,
                dlo        TEXT NOT NULL,
                dhi        TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS arc_epochs (
                product_id      INTEGER NOT NULL,
                epoch           INTEGER NOT NULL,
                raw_phase       TEXT,
                lock            INTEGER NOT NULL,
                cumulative_jump INTEGER NOT NULL,
                corrected_phase TEXT,
                usable          INTEGER NOT NULL,
                arc_id          INTEGER,
                start_reason    TEXT,
                source_event    INTEGER,
                PRIMARY KEY (product_id, epoch)
            );

            CREATE TABLE IF NOT EXISTS task_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id      INTEGER NOT NULL,
                provenance   TEXT NOT NULL,    -- JSON: product ids + stamps consumed
                status       TEXT NOT NULL,    -- UNIQUE / AMBIGUOUS / INCONSISTENT / INCOMPLETE
                n1           INTEGER,
                n2           INTEGER,
                solution_count INTEGER,        -- exact count when fully enumerable
                truncated    INTEGER NOT NULL DEFAULT 0,
                detail       TEXT NOT NULL,    -- human/JSON explanation
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_task_results
                ON task_results (task_id);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ observations
    def put_observation(
        self, station: str, channel: int, epoch: int, phase: object, lock: bool
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO observations (station, channel, epoch, phase, lock)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(station, channel, epoch) DO UPDATE SET
                phase = excluded.phase,
                lock  = excluded.lock
            """,
            (station, int(channel), int(epoch), _enc(Q(phase)), 1 if lock else 0),
        )
        self.conn.commit()

    def put_observations(
        self, rows: Iterable[tuple[str, int, int, object, bool]]
    ) -> None:
        cur = self.conn.cursor()
        cur.executemany(
            """
            INSERT INTO observations (station, channel, epoch, phase, lock)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(station, channel, epoch) DO UPDATE SET
                phase = excluded.phase,
                lock  = excluded.lock
            """,
            [
                (s, int(c), int(e), _enc(Q(p)), 1 if lk else 0)
                for (s, c, e, p, lk) in rows
            ],
        )
        self.conn.commit()

    def get_observation(
        self, station: str, channel: int, epoch: int
    ) -> Optional[tuple[Fraction, bool]]:
        row = self.conn.execute(
            "SELECT phase, lock FROM observations "
            "WHERE station=? AND channel=? AND epoch=?",
            (station, int(channel), int(epoch)),
        ).fetchone()
        if row is None:
            return None
        return _dec(row["phase"]), bool(row["lock"])

    def iter_observations(
        self, station: Optional[str] = None, channel: Optional[int] = None
    ) -> Iterator[tuple[str, int, int, Fraction, bool]]:
        clauses, params = [], []
        if station is not None:
            clauses.append("station=?")
            params.append(station)
        if channel is not None:
            clauses.append("channel=?")
            params.append(int(channel))
        sql = "SELECT station, channel, epoch, phase, lock FROM observations"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY station, channel, epoch"
        for row in self.conn.execute(sql, params):
            yield (
                row["station"],
                row["channel"],
                row["epoch"],
                _dec(row["phase"]),
                bool(row["lock"]),
            )

    # ------------------------------------------------------------- jump events
    def add_jump(
        self, station: str, channel: int, epoch: int, jump: int
    ) -> int:
        """Record an explicit cycle-slip event; returns its event id."""
        if not isinstance(jump, int) or isinstance(jump, bool):
            raise TypeError("jump must be an int")
        cur = self.conn.execute(
            "INSERT INTO jump_events (station, channel, epoch, jump, superseded) "
            "VALUES (?, ?, ?, ?, 0)",
            (station, int(channel), int(epoch), int(jump)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def supersede_jump(self, event_id: int) -> None:
        """Mark an existing event as corrected away (kept for audit)."""
        self.conn.execute(
            "UPDATE jump_events SET superseded=1 WHERE id=?", (int(event_id),)
        )
        self.conn.commit()

    def get_jump(self, event_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jump_events WHERE id=?", (int(event_id),)
        ).fetchone()

    def active_jumps(
        self, station: str, channel: int
    ) -> list[tuple[int, int, int]]:
        """Active (non-superseded) events as ``(event_id, epoch, jump)`` sorted."""
        return [
            (int(r["id"]), int(r["epoch"]), int(r["jump"]))
            for r in self.conn.execute(
                "SELECT id, epoch, jump FROM jump_events "
                "WHERE station=? AND channel=? AND superseded=0 "
                "ORDER BY epoch, id",
                (station, int(channel)),
            )
        ]

    def max_event_id(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM jump_events").fetchone()
        return int(row["m"])

    def jump_version(self) -> tuple[int, int]:
        """(max active event id, number of active events) -- a cheap stamp."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(id),0) AS m, COUNT(*) AS c "
            "FROM jump_events WHERE superseded=0"
        ).fetchone()
        return int(row["m"]), int(row["c"])

    # ----------------------------------------------------------- arc products
    def save_arc_product(
        self,
        *,
        station: str,
        channel: int,
        gap: int,
        event_level: int,
        obs_stamp: str,
        segments: Sequence[dict],
        epochs: Sequence[dict],
        jump_stamp: str = "",
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO arc_products "
            "(station, channel, gap, event_level, obs_stamp, jump_stamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (station, int(channel), int(gap), int(event_level), obs_stamp, jump_stamp),
        )
        pid = int(cur.lastrowid)
        self.conn.executemany(
            """
            INSERT INTO arc_segments
                (product_id, seg_order, arc_id, start_reason, end_reason,
                 source_event, start_epoch, end_epoch, n_epochs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    pid,
                    seg["seg_order"],
                    seg["arc_id"],
                    seg["start_reason"],
                    seg["end_reason"],
                    seg["source_event"],
                    seg["start_epoch"],
                    seg["end_epoch"],
                    seg["n_epochs"],
                )
                for seg in segments
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO arc_epochs
                (product_id, epoch, raw_phase, lock, cumulative_jump,
                 corrected_phase, usable, arc_id, start_reason, source_event)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    pid,
                    ep["epoch"],
                    ep["raw_phase"],
                    ep["lock"],
                    ep["cumulative_jump"],
                    ep["corrected_phase"],
                    ep["usable"],
                    ep["arc_id"],
                    ep["start_reason"],
                    ep["source_event"],
                )
                for ep in epochs
            ],
        )
        self.conn.commit()
        return pid

    def load_arc_product(self, product_id: int) -> Optional[dict]:
        """Reconstruct a fully materialised product snapshot from storage."""
        head = self.conn.execute(
            "SELECT * FROM arc_products WHERE id=?", (int(product_id),)
        ).fetchone()
        if head is None:
            return None
        epochs = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM arc_epochs WHERE product_id=? ORDER BY epoch",
                (int(product_id),),
            )
        ]
        segments = [dict(r) for r in self.arc_segments(product_id)]
        return {
            "station": head["station"],
            "channel": head["channel"],
            "gap": head["gap"],
            "event_level": head["event_level"],
            "obs_stamp": head["obs_stamp"],
            "jump_stamp": head["jump_stamp"],
            "product_id": head["id"],
            "external": False,
            "epochs": epochs,
            "segments": segments,
        }

    def latest_arc_product(self, station: str, channel: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT MAX(id) AS pid FROM arc_products WHERE station=? AND channel=?",
            (station, int(channel)),
        ).fetchone()
        return None if row is None or row["pid"] is None else int(row["pid"])

    def list_arc_products(self, station: str, channel: int) -> list[int]:
        return [
            int(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM arc_products WHERE station=? AND channel=? ORDER BY id",
                (station, int(channel)),
            )
        ]

    def get_arc_product(self, product_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM arc_products WHERE id=?", (int(product_id),)
        ).fetchone()

    def arc_segments(self, product_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM arc_segments WHERE product_id=? ORDER BY seg_order",
                (int(product_id),),
            )
        )

    # ------------------------------------------------------------------ tasks
    def create_task(
        self,
        *,
        rover: str,
        base: str,
        channel1: int,
        channel2: int,
        lambda1: object,
        lambda2: object,
        win_lo: int,
        win_hi: int,
        dlo: object,
        dhi: object,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO tasks
                (rover, base, channel1, channel2, lambda1, lambda2,
                 win_lo, win_hi, dlo, dhi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rover,
                base,
                int(channel1),
                int(channel2),
                _enc(Q(lambda1)),
                _enc(Q(lambda2)),
                int(win_lo),
                int(win_hi),
                _enc(Q(dlo)),
                _enc(Q(dhi)),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_task(self, task_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (int(task_id),)
        ).fetchone()

    def save_task_result(
        self,
        *,
        task_id: int,
        provenance: str,
        status: str,
        n1: Optional[int],
        n2: Optional[int],
        solution_count: Optional[int],
        truncated: bool,
        detail: str,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO task_results
                (task_id, provenance, status, n1, n2, solution_count,
                 truncated, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(task_id),
                provenance,
                status,
                n1,
                n2,
                solution_count,
                1 if truncated else 0,
                detail,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def task_results(self, task_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM task_results WHERE task_id=? ORDER BY id",
                (int(task_id),),
            )
        )
