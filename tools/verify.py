#!/usr/bin/env python3
"""Independent exhaustive cross-check of the warehouse.

This script deliberately re-implements, in the simplest possible way, the
pairing rules, plan feasibility rules and per-interval bounds, then compares
those brute-force answers against what the production pipeline stored in
SQLite.  It does not import the production analysis modules.

Usage:
    python3 tools/verify.py check     --input DIR --db PATH --batch N
    python3 tools/verify.py correction --input DIR --db PATH \
            --base-batch N --new-batch M --train T --changes FILE

Exit code 0 means every compared item matched.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import sys
from collections import defaultdict


# ---------------------------------------------------------------------------
# Independent input model
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_inputs(input_dir):
    stations = {s["station"]: s for s in load_json(f"{input_dir}/stations.json")}
    raw = load_json(f"{input_dir}/timetable.json")
    rows = raw["stops"] if isinstance(raw, dict) else raw
    trains = defaultdict(list)
    for r in rows:
        trains[r["train_id"]].append(r)
    for stops in trains.values():
        stops.sort(key=lambda x: x["seq"])
    gates = load_json(f"{input_dir}/gates.json")
    events = gates["events"] if isinstance(gates, dict) else gates
    return stations, dict(trains), events


def apply_changes(trains, train_id, changes):
    trains = {k: [dict(s) for s in v] for k, v in trains.items()}
    by_seq = {s["seq"]: s for s in trains[train_id]}
    for ch in changes:
        stop = by_seq[ch["seq"]]
        if "arr_ts" in ch:
            stop["arr_ts"] = ch["arr_ts"]
        if "dep_ts" in ch:
            stop["dep_ts"] = ch["dep_ts"]
        if "station" in ch:
            stop["station"] = ch["station"]
    return trains


# ---------------------------------------------------------------------------
# Independent pairing
# ---------------------------------------------------------------------------

def dedup_events(events):
    seen = {}
    dup = 0
    for e in events:
        key = (e["card_id"], e["station"], int(e["ts"]), e["direction"])
        if e["event_id"] in seen:
            if seen[e["event_id"]] != key:
                raise AssertionError(f"conflicting event id {e['event_id']}")
            dup += 1
            continue
        seen[e["event_id"]] = key
    return dup


def independent_pair(events):
    by_card = defaultdict(list)
    seen_ids = set()
    for e in events:
        if e["event_id"] in seen_ids:
            continue
        seen_ids.add(e["event_id"])
        by_card[e["card_id"]].append(e)

    trips = {}
    unclosed = []
    orphans = []
    for card, evs in by_card.items():
        evs = sorted(evs, key=lambda e: (int(e["ts"]), e["event_id"]))
        pending = None
        for e in evs:
            if e["direction"] == "IN":
                if pending is not None:
                    unclosed.append(pending["event_id"])
                pending = e
            else:
                if pending is None:
                    orphans.append(e["event_id"])
                    continue
                trips[f"TRIP-{pending['event_id']}-{e['event_id']}"] = {
                    "card": card,
                    "entry": pending,
                    "exit": e,
                }
                pending = None
        if pending is not None:
            unclosed.append(pending["event_id"])
    return trips, sorted(unclosed), sorted(orphans)


# ---------------------------------------------------------------------------
# Brute-force plan enumeration
# ---------------------------------------------------------------------------

def _by_station(trains):
    idx = defaultdict(list)
    for train_id, stops in trains.items():
        for s in stops:
            idx[s["station"]].append((train_id, s))
    return idx


def brute_plans(trip, stations, trains, by_station):
    """Straightforward exhaustive enumeration of every feasible ride.

    Deliberately written as a direct transcription of the rules, independent of
    the production analyser.  ``by_station`` is only a lookup helper; the join
    logic below is the full nested search.
    """
    a = trip["entry"]["station"]
    b = trip["exit"]["station"]
    ready = int(trip["entry"]["ts"]) + stations[a]["entry_walk"]
    deadline = int(trip["exit"]["ts"]) - stations[b]["exit_walk"]
    plans = set()
    if ready > deadline:
        return plans

    def after(train_id, seq):
        return [s for s in trains[train_id] if s["seq"] > seq]

    # direct
    for t1, s1 in by_station[a]:
        if s1["dep_ts"] < ready or s1["dep_ts"] > deadline:
            continue
        for a1 in after(t1, s1["seq"]):
            if a1["arr_ts"] > deadline:
                break
            if a1["station"] == b:
                plans.add(((t1, s1["seq"], a1["seq"]),))

    # exactly one transfer
    for t1, s1 in by_station[a]:
        if s1["dep_ts"] < ready or s1["dep_ts"] > deadline:
            continue
        for a1 in after(t1, s1["seq"]):
            if a1["arr_ts"] > deadline:
                break
            m = a1["station"]
            mt = stations[m]["min_transfer"]
            earliest = a1["arr_ts"] + mt
            if earliest > deadline:
                continue
            for t2, b2 in by_station[m]:
                if t2 == t1:
                    continue
                if b2["dep_ts"] < earliest or b2["dep_ts"] > deadline:
                    continue
                for a2 in after(t2, b2["seq"]):
                    if a2["arr_ts"] > deadline:
                        break
                    if a2["station"] == b:
                        plans.add(
                            ((t1, s1["seq"], a1["seq"]),
                             (t2, b2["seq"], a2["seq"]))
                        )
    return plans


def hops_of(plan):
    hops = set()
    for train, bseq, aseq in plan:
        for k in range(bseq, aseq):
            hops.add((train, k))
    return hops


# ---------------------------------------------------------------------------
# DB readers (raw SQL, no production imports)
# ---------------------------------------------------------------------------

def db_trips(conn, batch):
    rows = conn.execute(
        "SELECT trip_id, entry_event_id, exit_event_id, status, n_plans "
        "FROM trip_pairs WHERE batch_id=?", (batch,)
    ).fetchall()
    return {r[0]: r for r in rows}


def db_plans(conn, batch):
    out = defaultdict(set)
    for r in conn.execute(
        "SELECT trip_id, train1, board_seq1, alight_seq1, train2, board_seq2, "
        "alight_seq2 FROM candidate_plans WHERE batch_id=?", (batch,)
    ):
        plan = [(r[1], r[2], r[3])]
        if r[4] is not None:
            plan.append((r[4], r[5], r[6]))
        out[r[0]].add(tuple(plan))
    return out


def db_bounds(conn, batch):
    return {
        (r[0], r[1]): (r[2], r[3])
        for r in conn.execute(
            "SELECT train_id, from_seq, min_count, max_count FROM interval_flows "
            "WHERE batch_id=?", (batch,)
        )
    }


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check(input_dir, db_path, batch, trains_override=None, label=""):
    stations, trains, events = load_inputs(input_dir)
    if trains_override is not None:
        trains = trains_override
    failures = []
    notes = []

    dup = dedup_events(events)
    trips, unclosed, orphans = independent_pair(events)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    n_db_trips = conn.execute(
        "SELECT COUNT(*) FROM trip_pairs WHERE batch_id=?", (batch,)
    ).fetchone()[0]
    if n_db_trips != len(trips):
        failures.append(f"{label}trip count: brute={len(trips)} db={n_db_trips}")

    db_unclosed = sorted(
        r[0] for r in conn.execute(
            "SELECT event_id FROM unclosed_entries WHERE batch_id=?", (batch,)
        )
    )
    db_orphans = sorted(
        r[0] for r in conn.execute(
            "SELECT event_id FROM orphan_exits WHERE batch_id=?", (batch,)
        )
    )
    if db_unclosed != unclosed:
        failures.append(f"{label}unclosed mismatch: {unclosed} vs {db_unclosed}")
    if db_orphans != orphans:
        failures.append(f"{label}orphan mismatch: {orphans} vs {db_orphans}")

    db_t = db_trips(conn, batch)
    db_p = db_plans(conn, batch)
    if set(db_t) != set(trips):
        failures.append(f"{label}trip id set mismatch")

    all_brute = {}
    by_station = _by_station(trains)
    for trip_id, trip in trips.items():
        brute = brute_plans(trip, stations, trains, by_station)
        all_brute[trip_id] = brute
        stored = db_p.get(trip_id, set())
        if brute != stored:
            failures.append(
                f"{label}{trip_id}: plan set mismatch "
                f"missing={sorted(brute - stored)[:5]} extra={sorted(stored - brute)[:5]} "
                f"(brute={len(brute)} db={len(stored)})"
            )
        elif trip_id in db_t and db_t[trip_id]["n_plans"] != len(brute):
            failures.append(f"{label}{trip_id}: n_plans mismatch")
        expected_status = "EXPLAINABLE" if brute else "UNEXPLAINABLE"
        if trip_id in db_t and db_t[trip_id]["status"] != expected_status:
            failures.append(f"{label}{trip_id}: status mismatch")

    # ---- independent interval bounds -------------------------------------
    universe = set()
    for stops in trains.values():
        for k in range(len(stops) - 1):
            universe.add((stops[0]["train_id"], k + 1))
    brute_min = defaultdict(int)
    brute_max = defaultdict(int)
    # inverted index: interval -> trips whose plan-union includes it, and ->
    # trips whose plan-intersection includes it.  Lets the witness check run in
    # O(total distinct hops) instead of O(intervals x trips x plans).
    trips_with_hop: dict[tuple, int] = defaultdict(int)
    trips_forced_hop: dict[tuple, int] = defaultdict(int)
    for trip_id, plans in all_brute.items():
        if not plans:
            continue
        hop_list = [hops_of(p) for p in plans]
        union = set().union(*hop_list)
        inter = set(hop_list[0])
        for h in hop_list[1:]:
            inter &= h
        for iv in union:
            brute_max[iv] += 1
            trips_with_hop[iv] += 1
        for iv in inter:
            brute_min[iv] += 1
            trips_forced_hop[iv] += 1

    db_b = db_bounds(conn, batch)
    if set(db_b) != universe:
        failures.append(
            f"{label}interval key set mismatch: brute={len(universe)} db={len(db_b)}"
        )
    for iv in sorted(universe):
        exp = (brute_min.get(iv, 0), brute_max.get(iv, 0))
        got = db_b.get(iv)
        if got != exp:
            failures.append(f"{label}interval {iv}: brute={exp} db={got}")

    # ---- independent witness replay from *stored* plans ------------------
    # Reimplement the stored witness rules here (separately from production)
    # and confirm each rule reproduces the stored bound, override count and
    # selection digest.  This checks that a witness really exists in the
    # persisted candidate plans, not just in the brute-force set.
    stored_plans = defaultdict(list)
    for r in conn.execute(
        "SELECT trip_id, plan_id, train1, board_seq1, alight_seq1, "
        "train2, board_seq2, alight_seq2 FROM candidate_plans "
        "WHERE batch_id=? ORDER BY trip_id, sort_key, plan_id",
        (batch,),
    ):
        legs = [(r[2], r[3], r[4])]
        if r[5] is not None:
            legs.append((r[5], r[6], r[7]))
        stored_plans[r[0]].append({"plan_id": r[1], "hops": hops_of(tuple(legs))})

    import hashlib

    replay_max = defaultdict(int)
    replay_min = defaultdict(int)
    replay_max_over = defaultdict(int)
    replay_min_over = defaultdict(int)
    replay_max_digest = defaultdict(int)
    replay_min_digest = defaultdict(int)

    def xd(trip_id, plan_id):
        return int(hashlib.sha1(f"{trip_id}:{plan_id}".encode()).hexdigest()[:16], 16)

    for trip_id, plans in stored_plans.items():
        rep = plans[0]["hops"]
        union = set()
        for p in plans:
            union |= p["hops"]
        inter = set(plans[0]["hops"])
        for p in plans[1:]:
            inter &= p["hops"]
        for iv in union:
            replay_max[iv] += 1
        for iv in inter:
            replay_min[iv] += 1
        for iv in union - inter:
            if iv in rep:
                for p in plans:
                    if iv not in p["hops"]:
                        replay_min_over[iv] += 1
                        replay_min_digest[iv] ^= xd(trip_id, p["plan_id"])
                        break
        for iv in union:
            if iv not in rep:
                for p in plans:
                    if iv in p["hops"]:
                        replay_max_over[iv] += 1
                        replay_max_digest[iv] ^= xd(trip_id, p["plan_id"])
                        break

    witnesses = {
        (r[0], r[1], r[2]): r
        for r in conn.execute(
            "SELECT train_id, from_seq, bound, n_overrides, selection_digest "
            "FROM witness_sets WHERE batch_id=?",
            (batch,),
        )
    }
    for iv in sorted(universe):
        train, fseq = iv
        cases = (
            ("MAX", brute_max.get(iv, 0), replay_max.get(iv, 0),
             replay_max_over.get(iv, 0), replay_max_digest.get(iv, 0)),
            ("MIN", brute_min.get(iv, 0), replay_min.get(iv, 0),
             replay_min_over.get(iv, 0), replay_min_digest.get(iv, 0)),
        )
        for bound, expected, count, overrides, digest in cases:
            if count != expected:
                failures.append(
                    f"{label}witness {bound} {iv}: replayed {count} != {expected}"
                )
            w = witnesses.get((train, fseq, bound))
            if w is None:
                failures.append(f"{label}witness {bound} {iv}: missing definition")
                continue
            if overrides != w[3]:
                failures.append(
                    f"{label}witness {bound} {iv}: overrides {overrides} != {w[3]}"
                )
            if f"{digest:016x}" != w[4]:
                failures.append(f"{label}witness {bound} {iv}: digest mismatch")

    conn.close()
    return {
        "label": label,
        "ok": not failures,
        "n_stations": len(stations),
        "n_trains": len(trains),
        "n_events_unique": len(events) - dup,
        "n_duplicate_rows": dup,
        "n_trips": len(trips),
        "n_unclosed": len(unclosed),
        "n_orphans": len(orphans),
        "n_interval_keys": len(universe),
        "failures": failures[:40],
        "n_failures": len(failures),
    }


def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--input", required=True)
    c.add_argument("--db", required=True)
    c.add_argument("--batch", type=int, required=True)
    c.add_argument("--train-override", default=None,
                   help="optional JSON with {'train':..,'changes':[..]}")
    c.add_argument("--label", default="")

    k = sub.add_parser("correction")
    k.add_argument("--input", required=True)
    k.add_argument("--db", required=True)
    k.add_argument("--base-batch", type=int, required=True)
    k.add_argument("--new-batch", type=int, required=True)
    k.add_argument("--train", required=True)
    k.add_argument("--changes", required=True)

    args = p.parse_args(argv)

    if args.cmd == "check":
        trains_override = None
        if args.train_override:
            spec = load_json(args.train_override)
            _, trains, _ = load_inputs(args.input)
            trains_override = apply_changes(trains, spec["train"], spec["changes"])
        result = check(args.input, args.db, args.batch, trains_override, args.label)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    # correction mode: verify the new batch against the corrected timetable
    spec = load_json(args.changes)
    if isinstance(spec, list):
        spec = {"train": args.train, "changes": spec}
    _, trains, _ = load_inputs(args.input)
    corrected = apply_changes(trains, spec.get("train", args.train), spec["changes"])
    result = check(args.input, args.db, args.new_batch, corrected, "corrected: ")
    result["base_batch"] = args.base_batch
    result["new_batch"] = args.new_batch
    result["train"] = spec.get("train", args.train)
    result["changes"] = spec["changes"]
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
