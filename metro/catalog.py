"""Timetable timetable registry: stations, trains, validated corrections.

This module is one of the two substantive sources modules.  It is usable with
no gate data at all: it imports stop sequences, rejects structurally invalid
timetables/corrections as a whole batch, freezes each accepted version as an
immutable snapshot, and answers station-time lookups.

Stop identity is ``(train_id, seq)``.  A train may visit the same station more
than once; we never collapse a stop to its station id.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Mapping

from .db import utcnow
from .model import Snapshot, StationInfo, Stop


class TimetableError(ValueError):
    """Raised when an import/correction batch is rejected in full."""

    def __init__(self, message: str, problems: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.problems = problems or []


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise TimetableError(f"{field_name} must be an integer, got bool")
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise TimetableError(f"{field_name} must be an integer: {value!r}") from exc
    if isinstance(value, float) and value != out:
        raise TimetableError(f"{field_name} must be an integer: {value!r}")
    return out


def normalize_station_rows(rows: Iterable[Mapping[str, Any]]) -> list[StationInfo]:
    stations: dict[str, StationInfo] = {}
    problems: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        try:
            station = str(row["station"]).strip()
            if not station:
                raise TimetableError("empty station id")
            info = StationInfo(
                station=station,
                name=str(row.get("name") or station),
                entry_walk=_as_int(row.get("entry_walk", 0), "entry_walk"),
                exit_walk=_as_int(row.get("exit_walk", 0), "exit_walk"),
                min_transfer=_as_int(row.get("min_transfer", 0), "min_transfer"),
            )
            if min(info.entry_walk, info.exit_walk, info.min_transfer) < 0:
                raise TimetableError("walk/transfer times must be >= 0")
            if station in stations:
                raise TimetableError(f"duplicate station id {station!r}")
            stations[station] = info
        except KeyError as exc:
            problems.append({"row": i, "error": f"missing field {exc}"})
        except TimetableError as exc:
            problems.append({"row": i, "error": str(exc)})
    if problems:
        raise TimetableError("station batch rejected", problems)
    return list(stations.values())


def normalize_stop_rows(
    rows: Iterable[Mapping[str, Any]],
    station_ids: set[str] | None = None,
) -> dict[str, tuple[Stop, ...]]:
    """Group raw stop rows into trains and validate the whole structure.

    Validation is global: one bad train rejects the entire batch.
    """
    grouped: dict[str, list[Stop]] = {}
    seen: set[tuple[str, int]] = set()
    problems: list[dict[str, Any]] = []

    for i, row in enumerate(rows):
        try:
            train_id = str(row["train_id"]).strip()
            if not train_id:
                raise TimetableError("empty train_id")
            station = str(row["station"]).strip()
            if not station:
                raise TimetableError("empty station")
            if station_ids is not None and station not in station_ids:
                raise TimetableError(f"unknown station {station!r}")
            seq = _as_int(row["seq"], "seq")
            arr_ts = _as_int(row["arr_ts"], "arr_ts")
            dep_ts = _as_int(row["dep_ts"], "dep_ts")
            if seq < 1:
                raise TimetableError(f"seq must be >= 1, got {seq}")
            if arr_ts > dep_ts:
                raise TimetableError(
                    f"arrival {arr_ts} later than departure {dep_ts} at seq {seq}"
                )
            key = (train_id, seq)
            if key in seen:
                raise TimetableError(f"duplicate stop identity {train_id}/{seq}")
            seen.add(key)
            grouped.setdefault(train_id, []).append(Stop(train_id, seq, station, arr_ts, dep_ts))
        except KeyError as exc:
            problems.append({"row": i, "error": f"missing field {exc}"})
        except TimetableError as exc:
            problems.append({"row": i, "error": str(exc)})

    trains: dict[str, tuple[Stop, ...]] = {}
    for train_id, stops in grouped.items():
        stops.sort(key=lambda s: s.seq)
        for expected, stop in enumerate(stops, start=1):
            if stop.seq != expected:
                problems.append(
                    {
                        "train_id": train_id,
                        "error": f"stop sequences must be contiguous from 1; "
                        f"expected {expected}, got {stop.seq}",
                    }
                )
                break
        for prev, cur in zip(stops, stops[1:]):
            if prev.dep_ts > cur.arr_ts:
                problems.append(
                    {
                        "train_id": train_id,
                        "error": f"departure {prev.dep_ts} at seq {prev.seq} later "
                        f"than arrival {cur.arr_ts} at seq {cur.seq}",
                    }
                )
        trains[train_id] = tuple(stops)

    if problems:
        raise TimetableError("timetable batch rejected", problems)
    if not trains:
        raise TimetableError("timetable batch rejected: no stops")
    return trains


# ---------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------

def upsert_stations(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> int:
    stations = normalize_station_rows(rows)
    with conn:
        conn.executemany(
            "INSERT INTO stations(station, name, entry_walk, exit_walk, min_transfer) "
            "VALUES(?,?,?,?,?) ON CONFLICT(station) DO UPDATE SET "
            "name=excluded.name, entry_walk=excluded.entry_walk, "
            "exit_walk=excluded.exit_walk, min_transfer=excluded.min_transfer",
            [(s.station, s.name, s.entry_walk, s.exit_walk, s.min_transfer) for s in stations],
        )
    return len(stations)


def load_stations(conn: sqlite3.Connection) -> dict[str, StationInfo]:
    rows = conn.execute(
        "SELECT station, name, entry_walk, exit_walk, min_transfer FROM stations"
    ).fetchall()
    return {
        r["station"]: StationInfo(
            r["station"], r["name"], r["entry_walk"], r["exit_walk"], r["min_transfer"]
        )
        for r in rows
    }


def get_station(conn: sqlite3.Connection, station: str) -> StationInfo:
    row = conn.execute(
        "SELECT station, name, entry_walk, exit_walk, min_transfer FROM stations "
        "WHERE station=?",
        (station,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown station {station!r}")
    return StationInfo(
        row["station"], row["name"], row["entry_walk"], row["exit_walk"], row["min_transfer"]
    )


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def import_timetable(
    conn: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]],
    name: str,
    note: str = "",
    base_snapshot_id: int | None = None,
) -> int:
    """Validate a whole timetable batch and freeze it as a new snapshot."""
    stations = load_stations(conn)
    if not stations:
        raise TimetableError("no stations registered; import stations first")
    trains = normalize_stop_rows(rows, set(stations))
    with conn:
        snapshot_id = _write_snapshot(
            conn, name=name, note=note, base_snapshot_id=base_snapshot_id,
            trains=trains, replace=False,
        )
    return snapshot_id


def _write_snapshot(
    conn: sqlite3.Connection,
    *,
    name: str,
    note: str,
    base_snapshot_id: int | None,
    trains: dict[str, tuple[Stop, ...]],
    replace: bool,
) -> int:
    cur = conn.execute(
        "INSERT INTO timetable_snapshots(name, created_at, base_snapshot_id, note) "
        "VALUES(?,?,?,?)",
        (name, utcnow(), base_snapshot_id, note),
    )
    snapshot_id = int(cur.lastrowid)
    if replace:
        conn.execute(
            "DELETE FROM snapshot_stops WHERE snapshot_id=?", (snapshot_id,)
        )
    payload = [
        (snapshot_id, s.train_id, s.seq, s.station, s.arr_ts, s.dep_ts)
        for stops in trains.values()
        for s in stops
    ]
    conn.executemany(
        "INSERT INTO snapshot_stops(snapshot_id, train_id, seq, station, arr_ts, dep_ts) "
        "VALUES(?,?,?,?,?,?)",
        payload,
    )
    return snapshot_id


def list_snapshots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.snapshot_id, s.name, s.created_at, s.base_snapshot_id, s.note,
               COUNT(st.train_id) AS n_stops,
               COUNT(DISTINCT st.train_id) AS n_trains
        FROM timetable_snapshots s
        LEFT JOIN snapshot_stops st ON st.snapshot_id = s.snapshot_id
        GROUP BY s.snapshot_id ORDER BY s.snapshot_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def load_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> Snapshot:
    head = conn.execute(
        "SELECT snapshot_id, name FROM timetable_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if head is None:
        raise KeyError(f"unknown snapshot {snapshot_id}")
    stations = load_stations(conn)
    trains: dict[str, list[Stop]] = {}
    rows = conn.execute(
        "SELECT train_id, seq, station, arr_ts, dep_ts FROM snapshot_stops "
        "WHERE snapshot_id=? ORDER BY train_id, seq",
        (snapshot_id,),
    )
    for r in rows:
        trains.setdefault(r["train_id"], []).append(
            Stop(r["train_id"], r["seq"], r["station"], r["arr_ts"], r["dep_ts"])
        )
    return Snapshot(
        snapshot_id=head["snapshot_id"],
        name=head["name"],
        stations=stations,
        trains={k: tuple(v) for k, v in trains.items()},
    )


def correct_train_times(
    conn: sqlite3.Connection,
    snapshot_id: int,
    train_id: str,
    changes: Iterable[Mapping[str, Any]],
    name: str,
    note: str = "",
) -> int:
    """Apply arrival/departure corrections to one train and freeze a new snapshot.

    All changes are validated together with the resulting train; any error
    rejects the whole batch and leaves the base snapshot untouched.
    """
    base = load_snapshot(conn, snapshot_id)
    if train_id not in base.trains:
        raise TimetableError(
            "correction batch rejected",
            [{"train_id": train_id, "error": "unknown train in snapshot"}],
        )
    stops = [Stop(s.train_id, s.seq, s.station, s.arr_ts, s.dep_ts) for s in base.trains[train_id]]
    by_seq = {s.seq: s for s in stops}
    problems: list[dict[str, Any]] = []
    applied = 0
    for i, change in enumerate(changes):
        try:
            seq = _as_int(change["seq"], "seq")
            if seq not in by_seq:
                raise TimetableError(f"unknown stop seq {seq}")
            old = by_seq[seq]
            new_station = str(change.get("station", old.station)).strip()
            if new_station not in base.stations:
                raise TimetableError(f"unknown station {new_station!r}")
            new_arr = _as_int(change.get("arr_ts", old.arr_ts), "arr_ts")
            new_dep = _as_int(change.get("dep_ts", old.dep_ts), "dep_ts")
            if new_arr > new_dep:
                raise TimetableError(
                    f"arrival {new_arr} later than departure {new_dep} at seq {seq}"
                )
            new_stop = Stop(train_id, seq, new_station, new_arr, new_dep)
            by_seq[seq] = new_stop
            applied += 1
        except KeyError as exc:
            problems.append({"change": i, "error": f"missing field {exc}"})
        except TimetableError as exc:
            problems.append({"change": i, "error": str(exc)})

    new_stops = tuple(by_seq[s.seq] for s in stops)
    for prev, cur in zip(new_stops, new_stops[1:]):
        if prev.dep_ts > cur.arr_ts:
            problems.append(
                {
                    "train_id": train_id,
                    "error": f"departure {prev.dep_ts} at seq {prev.seq} later "
                    f"than arrival {cur.arr_ts} at seq {cur.seq}",
                }
            )
    for stop in new_stops:
        if stop.arr_ts > stop.dep_ts:
            problems.append(
                {"train_id": train_id, "seq": stop.seq, "error": "arrival later than departure"}
            )
    if problems:
        raise TimetableError("correction batch rejected", problems)
    if applied == 0:
        raise TimetableError("correction batch rejected: no changes supplied")

    trains = dict(base.trains)
    trains[train_id] = new_stops
    with conn:
        new_id = _write_snapshot(
            conn, name=name, note=note or f"corrected {train_id}",
            base_snapshot_id=snapshot_id, trains=trains, replace=False,
        )
    return new_id


# ---------------------------------------------------------------------------
# Queries that work without any gate data
# ---------------------------------------------------------------------------

def station_times(
    conn: sqlite3.Connection, snapshot_id: int, station: str, *, limit: int = 1000
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT train_id, seq, station, arr_ts, dep_ts FROM snapshot_stops "
        "WHERE snapshot_id=? AND station=? ORDER BY arr_ts, train_id, seq LIMIT ?",
        (snapshot_id, station, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def train_stops(
    conn: sqlite3.Connection, snapshot_id: int, train_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT train_id, seq, station, arr_ts, dep_ts FROM snapshot_stops "
        "WHERE snapshot_id=? AND train_id=? ORDER BY seq",
        (snapshot_id, train_id),
    ).fetchall()
    return [dict(r) for r in rows]
