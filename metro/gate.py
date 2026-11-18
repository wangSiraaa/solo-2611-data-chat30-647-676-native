"""Gate-event import and trip pairing.

Pairing rules (per card, ordered by ``(ts, event_id)``):

* an ``IN`` opens a pending entry;
* the next ``OUT`` closes it into a trip;
* a new ``IN`` while one is pending marks the old entry *unclosed* and takes
  over the pending slot;
* an ``OUT`` with no pending entry is preserved as an *orphan exit*.

No anomalous record is dropped to make pairing easier.  Within one import a
repeated ``event_id`` with identical content is de-duplicated; the same id
with different content rejects the entire batch before anything is written.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any, Iterable, Mapping

from .db import utcnow
from .model import GateEvent, Trip

DIRECTIONS = ("IN", "OUT")


class GateDataError(ValueError):
    def __init__(self, message: str, problems: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.problems = problems or []


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise GateDataError(f"{field_name} must be an integer")
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise GateDataError(f"{field_name} must be an integer: {value!r}") from exc
    if isinstance(value, float) and value != out:
        raise GateDataError(f"{field_name} must be an integer: {value!r}")
    return out


def validate_events(
    rows: Iterable[Mapping[str, Any]],
    station_ids: set[str],
) -> tuple[list[GateEvent], int, int]:
    """Return ``(unique_events, raw_count, duplicate_count)`` or raise.

    Exact duplicate ids are counted and collapsed.  Conflicting ids abort the
    batch with every conflict reported, so an operator can fix the source
    rather than silently losing evidence.
    """
    raw = 0
    by_id: dict[str, GateEvent] = {}
    content_by_id: dict[str, tuple[str, str, int, str]] = {}
    conflicts: list[dict[str, Any]] = []
    duplicates = 0
    problems: list[dict[str, Any]] = []

    for i, row in enumerate(rows):
        raw += 1
        try:
            event_id = str(row["event_id"]).strip()
            card_id = str(row["card_id"]).strip()
            station = str(row["station"]).strip()
            direction = str(row["direction"]).strip().upper()
            ts = _as_int(row["ts"], "ts")
            if not event_id:
                raise GateDataError("empty event_id")
            if not card_id:
                raise GateDataError("empty card_id")
            if station not in station_ids:
                raise GateDataError(f"unknown station {station!r}")
            if direction not in DIRECTIONS:
                raise GateDataError(f"unknown direction {direction!r}")
        except KeyError as exc:
            problems.append({"row": i, "error": f"missing field {exc}"})
            continue
        except GateDataError as exc:
            problems.append({"row": i, "error": str(exc)})
            continue

        content = (card_id, station, ts, direction)
        if event_id in by_id:
            if content_by_id[event_id] != content:
                conflicts.append(
                    {
                        "event_id": event_id,
                        "first": list(content_by_id[event_id]),
                        "later": list(content),
                        "row": i,
                    }
                )
            else:
                duplicates += 1
            continue
        by_id[event_id] = GateEvent(event_id, card_id, station, ts, direction)
        content_by_id[event_id] = content

    if conflicts:
        raise GateDataError(
            "import rejected: event_id reused with different content",
            conflicts,
        )
    if problems:
        raise GateDataError("import rejected: invalid rows", problems)
    return list(by_id.values()), raw, duplicates


def store_import(
    conn: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]],
    name: str,
) -> dict[str, Any]:
    station_ids = {
        r["station"] for r in conn.execute("SELECT station FROM stations").fetchall()
    }
    if not station_ids:
        raise GateDataError("no stations registered; import stations first")
    events, raw, duplicates = validate_events(rows, station_ids)
    with conn:
        cur = conn.execute(
            "INSERT INTO gate_imports(name, created_at, raw_rows, unique_events, "
            "duplicate_rows, status) VALUES(?,?,?,?,?,?)",
            (name, utcnow(), raw, len(events), duplicates, "OK"),
        )
        import_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO gate_events(import_id, card_id, event_id, station, ts, direction) "
            "VALUES(?,?,?,?,?,?)",
            [
                (import_id, e.card_id, e.event_id, e.station, e.ts, e.direction)
                for e in events
            ],
        )
    return {
        "import_id": import_id,
        "raw_rows": raw,
        "unique_events": len(events),
        "duplicate_rows": duplicates,
        "n_cards": len({e.card_id for e in events}),
    }


def load_events(conn: sqlite3.Connection, import_id: int) -> list[GateEvent]:
    rows = conn.execute(
        "SELECT event_id, card_id, station, ts, direction FROM gate_events "
        "WHERE import_id=?",
        (import_id,),
    ).fetchall()
    return [
        GateEvent(r["event_id"], r["card_id"], r["station"], r["ts"], r["direction"])
        for r in rows
    ]


def pair_events(
    events: Iterable[GateEvent],
) -> tuple[list[Trip], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair events into trips, returning ``(trips, unclosed_entries, orphan_exits)``."""
    by_card: dict[str, list[GateEvent]] = {}
    for event in events:
        by_card.setdefault(event.card_id, []).append(event)

    trips: list[Trip] = []
    unclosed: list[dict[str, Any]] = []
    orphan_exits: list[dict[str, Any]] = []

    for card_id in sorted(by_card):
        ordered = sorted(by_card[card_id], key=lambda e: (e.ts, e.event_id))
        pending: GateEvent | None = None
        for event in ordered:
            if event.direction == "IN":
                if pending is not None:
                    unclosed.append(
                        {
                            "card_id": card_id,
                            "event_id": pending.event_id,
                            "station": pending.station,
                            "ts": pending.ts,
                            "reason": "superseded by later entry before an exit",
                        }
                    )
                pending = event
            else:  # OUT
                if pending is None:
                    orphan_exits.append(
                        {
                            "card_id": card_id,
                            "event_id": event.event_id,
                            "station": event.station,
                            "ts": event.ts,
                            "reason": "exit with no preceding entry",
                        }
                    )
                    continue
                trips.append(
                    Trip(
                        trip_id=f"TRIP-{pending.event_id}-{event.event_id}",
                        card_id=card_id,
                        entry_event_id=pending.event_id,
                        exit_event_id=event.event_id,
                        entry_station=pending.station,
                        entry_ts=pending.ts,
                        exit_station=event.station,
                        exit_ts=event.ts,
                    )
                )
                pending = None
        if pending is not None:
            unclosed.append(
                {
                    "card_id": card_id,
                    "event_id": pending.event_id,
                    "station": pending.station,
                    "ts": pending.ts,
                    "reason": "no exit recorded",
                }
            )

    trips.sort(key=lambda t: (t.entry_ts, t.entry_event_id))
    return trips, unclosed, orphan_exits


def pairing_for_import(
    conn: sqlite3.Connection, import_id: int
) -> tuple[list[Trip], list[dict[str, Any]], list[dict[str, Any]]]:
    return pair_events(load_events(conn, import_id))


def import_summary(conn: sqlite3.Connection, import_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM gate_imports WHERE import_id=?", (import_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown import {import_id}")
    trips, unclosed, orphans = pairing_for_import(conn, import_id)
    counts = Counter(e["card_id"] for e in load_events(conn, import_id))
    return {
        **dict(row),
        "n_cards": len(counts),
        "n_trips": len(trips),
        "n_unclosed": len(unclosed),
        "n_orphans": len(orphans),
    }
