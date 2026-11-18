"""Synthetic input generation for the explainable flow warehouse.

Generates, from an empty project, a 20-station / 100-train / 30-stop network
with 1000 ordinary trips plus hand-crafted cases for direct rides, transfers,
loop-line repeated stations, missed last trains, duplicate swipes, stale
entries and missing exits.

Also generates a tiny, independent dataset used for exhaustive cross-checking.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from typing import Any

BASE = 6 * 3600  # 06:00
DWELL = 60
RUN = 120


# ---------------------------------------------------------------------------
# Network definitions
# ---------------------------------------------------------------------------

def full_stations() -> list[dict[str, Any]]:
    rows = []
    for i in range(1, 21):
        station = f"S{i:02d}"
        is_interchange = station in {
            "S05", "S06", "S09", "S10", "S11", "S12", "S13",
            "S14", "S15", "S16",
        }
        rows.append(
            {
                "station": station,
                "name": f"Station {i:02d}",
                "entry_walk": 120,
                "exit_walk": 120,
                "min_transfer": 240 if is_interchange else 120,
            }
        )
    return rows


A_PATH = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1,
          2, 3, 4, 5, 6, 7, 8, 9, 10, 9, 10]
B_PATH = [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 19, 18, 17, 16, 15, 14,
          13, 12, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 19, 20]
X_PATH = [5, 6, 10, 11, 12, 16, 15, 14, 13, 12, 11, 10, 9, 5, 6, 10,
          11, 12, 13, 14, 15, 16, 15, 14, 13, 12, 11, 10, 9, 5]
LOOP_PATH = [16, 17, 18, 19, 16, 17, 18, 19, 16, 17, 18, 19, 16, 17, 18,
             19, 16, 17, 18, 19, 16, 17, 18, 19, 16, 17, 18, 19, 16, 17]

TEMPLATES = {
    "A": ([f"S{n:02d}" for n in A_PATH], 40, 480, 0),
    "B": ([f"S{n:02d}" for n in B_PATH], 30, 600, 120),
    "X": ([f"S{n:02d}" for n in X_PATH], 20, 900, 300),
    "L": ([f"S{n:02d}" for n in LOOP_PATH], 10, 1200, 600),
}


def build_timetable(rng: random.Random | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for prefix, (path, count, headway, offset) in TEMPLATES.items():
        assert len(path) == 30
        for i in range(1, count + 1):
            train_id = f"{prefix}{i:02d}"
            start = BASE + offset + (i - 1) * headway
            t = start
            for seq, station in enumerate(path, start=1):
                arr = t
                dep = t + DWELL
                rows.append(
                    {"train_id": train_id, "seq": seq, "station": station,
                     "arr_ts": arr, "dep_ts": dep}
                )
                t = dep + RUN
    assert len(rows) == 3000
    return rows


# ---------------------------------------------------------------------------
# Helpers to derive gate timings from an actual train ride
# ---------------------------------------------------------------------------

def _stops_by_train(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row["train_id"], []).append(row)
    for stops in out.values():
        stops.sort(key=lambda r: r["seq"])
    return out


def _direct_trip(
    trains: dict[str, list[dict[str, Any]]],
    train_id: str,
    board_seq: int,
    alight_seq: int,
    walk: dict[str, tuple[int, int]],
    rng: random.Random,
) -> tuple[str, int, str, int]:
    stops = trains[train_id]
    board = stops[board_seq - 1]
    alight = stops[alight_seq - 1]
    ew = walk[board["station"]][0]
    xw = walk[alight["station"]][1]
    entry_ts = board["dep_ts"] - ew - rng.randint(0, 45)
    exit_ts = alight["arr_ts"] + xw + rng.randint(0, 45)
    return board["station"], entry_ts, alight["station"], exit_ts


def _transfer_trip(
    trains: dict[str, list[dict[str, Any]]],
    t1: str, bseq1: int, aseq1: int,
    t2: str, bseq2: int, aseq2: int,
    walk: dict[str, tuple[int, int]],
    rng: random.Random,
) -> tuple[str, int, str, int]:
    s1 = trains[t1]
    s2 = trains[t2]
    board1 = s1[bseq1 - 1]
    alight1 = s1[aseq1 - 1]
    board2 = s2[bseq2 - 1]
    alight2 = s2[aseq2 - 1]
    assert alight1["station"] == board2["station"]
    entry_ts = board1["dep_ts"] - walk[board1["station"]][0] - rng.randint(0, 45)
    exit_ts = alight2["arr_ts"] + walk[alight2["station"]][1] + rng.randint(0, 45)
    return board1["station"], entry_ts, alight2["station"], exit_ts


def _find_transfer_trips(
    trains: dict[str, list[dict[str, Any]]],
    station_min_transfer: dict[str, int],
    rng: random.Random,
    limit: int,
) -> list[tuple[str, int, int, str, int, int]]:
    """Find realistic two-leg rides with a *tight* connection.

    For each interchange and each arrival on train 1 we take the first feasible
    departure on a different line (train 2), then pick a varied boarding stop
    before the interchange and alighting stop after it.  Tight connections keep
    gate windows and plan spaces realistic while still leaving genuine choices.
    """
    by_station: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}
    for train_id, stops in trains.items():
        for stop in stops:
            by_station.setdefault(stop["station"], []).append((train_id, stop["seq"], stop))

    candidates: list[tuple[str, int, int, str, int, int]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for m in sorted(by_station):
        if station_min_transfer.get(m, 120) < 200:
            continue  # only real interchanges
        visits = by_station[m]
        for t1, a1, st1 in visits:
            feasible = []
            for t2, b2, st2 in visits:
                if t1 == t2 or t1[0] == t2[0]:
                    continue
                wait = st2["dep_ts"] - st1["arr_ts"]
                if wait < station_min_transfer[m] or wait > 900:
                    continue
                feasible.append((wait, t2, b2, st2))
            if not feasible:
                continue
            feasible.sort(key=lambda x: (x[0], x[1], x[2]))
            wait, t2, b2, st2 = feasible[0]
            stops1 = trains[t1]
            stops2 = trains[t2]
            boards = [s for s in stops1[: a1 - 1] if s["station"] != m]
            alights = [s for s in stops2[b2:] if s["station"] != m]
            if not boards or not alights:
                continue
            # sample a boarding stop and an alighting stop for diversity
            board = boards[rng.randrange(len(boards))]
            alight = alights[rng.randrange(len(alights))]
            pair = (board["station"], alight["station"])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            candidates.append((t1, board["seq"], a1, t2, b2, alight["seq"]))
            if len(candidates) >= limit * 8:
                return candidates
    return candidates


# ---------------------------------------------------------------------------
# Full dataset
# ---------------------------------------------------------------------------

ANOMALY_CARDS = {
    "CARD-MISSED-LAST": "missed last train",
    "CARD-LOOP": "loop repeated station",
    "CARD-STALE": "stale entry",
    "CARD-ORPHAN": "orphan exit",
    "CARD-NOEXIT": "missing exit",
    "CARD-DUP": "duplicate identical swipes",
    "CARD-DIRECT": "direct showcase",
    "CARD-TRANSFER": "transfer showcase",
}


def build_full(out_dir: str, seed: int = 20260101) -> dict[str, Any]:
    rng = random.Random(seed)
    stations = full_stations()
    walk = {
        s["station"]: (s["entry_walk"], s["exit_walk"]) for s in stations
    }
    min_transfer = {s["station"]: s["min_transfer"] for s in stations}
    timetable = build_timetable(rng)
    trains = _stops_by_train(timetable)

    events: list[dict[str, Any]] = []
    seq_no = 0

    def add_event(card: str, station: str, ts: int, direction: str) -> str:
        nonlocal seq_no
        seq_no += 1
        event_id = f"E{seq_no:06d}"
        events.append(
            {"event_id": event_id, "card_id": card, "station": station,
             "ts": int(ts), "direction": direction}
        )
        return event_id

    # ---- hand-crafted showcase cases (excluded from the 1000 ordinary trips) --
    # direct on A
    st, ets, xs, xts = _direct_trip(trains, "A01", 1, 5, walk, rng)
    add_event("CARD-DIRECT", st, ets, "IN")
    add_event("CARD-DIRECT", xs, xts, "OUT")

    # transfer: a real two-line connection at an interchange (one transfer)
    showcase_transfers = _find_transfer_trips(trains, min_transfer, rng, 8)
    if showcase_transfers:
        t1, b1, a1, t2, b2, a2 = showcase_transfers[0]
        st, ets, xs, xts = _transfer_trip(
            trains, t1, b1, a1, t2, b2, a2, walk, rng
        )
        add_event("CARD-TRANSFER", st, ets, "IN")
        add_event("CARD-TRANSFER", xs, xts, "OUT")

    # loop line repeated station: board and alight both at S16 on L01
    st, ets, xs, xts = _direct_trip(trains, "L01", 1, 5, walk, rng)
    add_event("CARD-LOOP", st, ets, "IN")
    add_event("CARD-LOOP", xs, xts, "OUT")

    # missed last train: entry after the last A train has left
    last_dep = max(s[-1]["dep_ts"] for s in trains.values())
    add_event("CARD-MISSED-LAST", "S01", last_dep + 3600, "IN")
    add_event("CARD-MISSED-LAST", "S05", last_dep + 7200, "OUT")

    # stale entry: IN, IN, OUT -> first IN unclosed, second pairs
    add_event("CARD-STALE", "S02", BASE + 400, "IN")
    add_event("CARD-STALE", "S03", BASE + 700, "IN")
    add_event("CARD-STALE", "S05", BASE + 2000, "OUT")

    # orphan exit without an entry
    add_event("CARD-ORPHAN", "S07", BASE + 1000, "OUT")

    # missing exit: IN only
    add_event("CARD-NOEXIT", "S04", BASE + 900, "IN")

    # duplicate identical swipes (exact same event id and content written twice)
    dup_events = [
        {"event_id": "E-DUP-1", "card_id": "CARD-DUP", "station": "S02",
         "ts": BASE + 500, "direction": "IN"},
        {"event_id": "E-DUP-1", "card_id": "CARD-DUP", "station": "S02",
         "ts": BASE + 500, "direction": "IN"},
        {"event_id": "E-DUP-2", "card_id": "CARD-DUP", "station": "S06",
         "ts": BASE + 3000, "direction": "OUT"},
        {"event_id": "E-DUP-2", "card_id": "CARD-DUP", "station": "S06",
         "ts": BASE + 3000, "direction": "OUT"},
    ]
    events.extend(dup_events)

    # ---- 1000 ordinary trips ---------------------------------------------
    # 880 single-train trips, 120 explicit one-transfer trips.  Gate windows are
    # kept tight around the intended ride so the plan space stays realistic
    # (a 5-hour open window would legitimately admit thousands of rides).
    ordinary = 0
    train_ids = sorted(trains)

    def pick_direct() -> tuple[str, int, str, int]:
        train_id = rng.choice(train_ids)
        stops = trains[train_id]
        b = rng.randint(1, len(stops) - 3)
        span = rng.randint(2, min(8, len(stops) - b))
        a = b + span
        return _direct_trip(trains, train_id, b, a, walk, rng)

    while ordinary < 880:
        st, ets, xs, xts = pick_direct()
        if ets >= last_dep or not (BASE <= ets <= BASE + 5 * 3600):
            continue
        card = f"CARD-R{ordinary:04d}"
        add_event(card, st, ets, "IN")
        add_event(card, xs, xts, "OUT")
        ordinary += 1

    transfer_specs = _find_transfer_trips(trains, min_transfer, rng, 120)
    rng.shuffle(transfer_specs)
    made = 0
    for (t1, b1, a1, t2, b2, a2) in transfer_specs:
        if made >= 120:
            break
        st, ets, xs, xts = _transfer_trip(
            trains, t1, b1, a1, t2, b2, a2, walk, rng
        )
        if ets < BASE or ets > BASE + 5 * 3600 + 2 * 3600:
            continue
        card = f"CARD-T{made:04d}"
        add_event(card, st, ets, "IN")
        add_event(card, xs, xts, "OUT")
        made += 1

    trips = (ordinary + made)
    os.makedirs(out_dir, exist_ok=True)

    def dump(name: str, obj: Any) -> None:
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")

    dump("stations.json", stations)
    dump("timetable.json", {"name": "full-2026", "stops": timetable})
    events.sort(key=lambda e: (e["ts"], e["event_id"]))
    dump("gates.json", {"name": "full-2026", "events": events})

    # Delay one train from seq 3 onward by 10 minutes.  Every later stop shifts
    # too, so the timetable stays structurally valid while the correction really
    # moves boarding opportunities (and therefore candidate plans/bounds).
    a07 = trains["A07"]
    delay = 600
    correction = {
        "train": "A07",
        "changes": [
            {"seq": s["seq"], "arr_ts": s["arr_ts"] + delay,
             "dep_ts": s["dep_ts"] + delay}
            for s in a07 if s["seq"] >= 3
        ],
    }
    dump("correction.json", correction)
    return {
        "stations": len(stations),
        "trains": len(trains),
        "n_stops": len(timetable),
        "events": len(events),
        "trips_expected": trips,
        "ordinary": ordinary,
        "transfers": made,
    }


# ---------------------------------------------------------------------------
# Small exhaustive-check dataset
# ---------------------------------------------------------------------------

def build_small(out_dir: str) -> dict[str, Any]:
    stations = [
        {"station": "X1", "name": "Alpha", "entry_walk": 60, "exit_walk": 60,
         "min_transfer": 120},
        {"station": "X2", "name": "Bravo", "entry_walk": 60, "exit_walk": 60,
         "min_transfer": 120},
        {"station": "X3", "name": "Charlie", "entry_walk": 60, "exit_walk": 60,
         "min_transfer": 60},
        {"station": "X4", "name": "Delta", "entry_walk": 60, "exit_walk": 60,
         "min_transfer": 120},
        {"station": "X5", "name": "Echo", "entry_walk": 60, "exit_walk": 60,
         "min_transfer": 120},
        {"station": "X6", "name": "Foxtrot", "entry_walk": 60, "exit_walk": 60,
         "min_transfer": 120},
    ]

    def train(train_id: str, path: list[str], start: int,
              dwell: int = 30, run: int = 300) -> list[dict[str, Any]]:
        rows = []
        t = start
        for seq, station in enumerate(path, start=1):
            rows.append({"train_id": train_id, "seq": seq, "station": station,
                         "arr_ts": t, "dep_ts": t + dwell})
            t = t + dwell + run
        return rows

    stops: list[dict[str, Any]] = []
    # direct line X1-X2-X3-X4
    stops += train("D1", ["X1", "X2", "X3", "X4"], 1000)
    # transfer pair: T1 X1->X2, T2 X2->X3->X4->X5
    stops += train("T1", ["X1", "X2"], 1000)
    stops += train("T2", ["X2", "X3", "X4", "X5"], 1000 + 30 + 300 + 120)
    # loop line repeating X4: X4-X5-X6-X4-X5-X6
    stops += train("L1", ["X4", "X5", "X6", "X4", "X5", "X6"], 2000)
    # an early-only train for the missed-last-train case
    stops += train("EARLY", ["X1", "X2"], 0)

    events = [
        # direct: entry X1 900 (ready 960) -> D1 dep 1030; exit X4 6000
        {"event_id": "S-D1-IN", "card_id": "SMALL-DIRECT", "station": "X1",
         "ts": 900, "direction": "IN"},
        {"event_id": "S-D1-OUT", "card_id": "SMALL-DIRECT", "station": "X4",
         "ts": 6000, "direction": "OUT"},
        # transfer: X2 -> X5 via T1 then T2 (entry 900 ready 960; T1 dep 1030)
        {"event_id": "S-TR-IN", "card_id": "SMALL-TRANSFER", "station": "X1",
         "ts": 900, "direction": "IN"},
        {"event_id": "S-TR-OUT", "card_id": "SMALL-TRANSFER", "station": "X5",
         "ts": 5000, "direction": "OUT"},
        # loop: board X4 seq1 and alight X4 seq4 (station repeated on L1)
        {"event_id": "S-LOOP-IN", "card_id": "SMALL-LOOP", "station": "X4",
         "ts": 1950, "direction": "IN"},
        {"event_id": "S-LOOP-OUT", "card_id": "SMALL-LOOP", "station": "X4",
         "ts": 4200, "direction": "OUT"},
        # missed last train: all trains are long gone
        {"event_id": "S-LATE-IN", "card_id": "SMALL-LATE", "station": "X1",
         "ts": 9000, "direction": "IN"},
        {"event_id": "S-LATE-OUT", "card_id": "SMALL-LATE", "station": "X2",
         "ts": 10000, "direction": "OUT"},
        # duplicate identical
        {"event_id": "S-DUP-IN", "card_id": "SMALL-DUP", "station": "X1",
         "ts": 900, "direction": "IN"},
        {"event_id": "S-DUP-IN", "card_id": "SMALL-DUP", "station": "X1",
         "ts": 900, "direction": "IN"},
        {"event_id": "S-DUP-OUT", "card_id": "SMALL-DUP", "station": "X2",
         "ts": 2000, "direction": "OUT"},
        # stale entry: first IN unclosed, second IN pairs with OUT
        {"event_id": "S-STALE-IN1", "card_id": "SMALL-STALE", "station": "X1",
         "ts": 800, "direction": "IN"},
        {"event_id": "S-STALE-IN2", "card_id": "SMALL-STALE", "station": "X2",
         "ts": 1400, "direction": "IN"},
        {"event_id": "S-STALE-OUT", "card_id": "SMALL-STALE", "station": "X4",
         "ts": 3000, "direction": "OUT"},
        # orphan exit
        {"event_id": "S-ORPHAN-OUT", "card_id": "SMALL-ORPHAN", "station": "X3",
         "ts": 2500, "direction": "OUT"},
        # missing exit
        {"event_id": "S-NOEXIT-IN", "card_id": "SMALL-NOEXIT", "station": "X3",
         "ts": 1200, "direction": "IN"},
    ]

    os.makedirs(out_dir, exist_ok=True)

    def dump(name: str, obj: Any) -> None:
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")

    dump("stations.json", stations)
    dump("timetable.json", {"name": "small", "stops": stops})
    events.sort(key=lambda e: (e["ts"], e["event_id"]))
    dump("gates.json", {"name": "small", "events": events})
    dump(
        "correction.json",
        {"train": "T2", "changes": [
            {"seq": 2, "arr_ts": 1500, "dep_ts": 1530},
            {"seq": 3, "arr_ts": 1830, "dep_ts": 1860},
        ]},
    )
    return {"stations": len(stations), "trains": 4, "n_stops": len(stops),
            "events": len(events)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate synthetic metro inputs")
    p.add_argument("--out", required=True)
    p.add_argument("--small-out", default=None)
    p.add_argument("--seed", type=int, default=20260101)
    args = p.parse_args(argv)
    print(json.dumps({"full": build_full(args.out, args.seed)}, indent=2))
    if args.small_out:
        print(json.dumps({"small": build_small(args.small_out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
