"""Trip explanation and explainable interval-flow bounds.

This is the second substantive source module.  It consumes the *frozen*
timetable snapshot plus paired trips and produces, for every trip, the full set
of feasible ride plans, and for every train's adjacent stop interval the
minimum/maximum passenger count over all feasible explanations, each with a
complete per-trip selection witness.

Key semantics
-------------
* board departure >= entry_ts + entry_walk(entry station)
* final alight arrival + exit_walk(exit station) <= exit_ts
* transfer: next train departure - previous train arrival >= min_transfer
* at most one transfer, on two *different* trains
* board seq < alight seq on every leg; loop trains may repeat a station
* no out-of-station walking is ever invented: transfer stations must match
  exactly and entry/exit stations must match the first board/last alight
* plans are de-duplicated by ``(train, board_seq, alight_seq)`` per leg

Interval bounds are *not* a reconstructed load.  Each bound is the extremum
over independent per-trip choices and carries its own witness selection; the
minimum of one interval and the maximum of another need not co-occur.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Iterable

from .catalog import load_snapshot
from .db import dumps, utcnow
from .model import GateEvent, Leg, Plan, Snapshot, Trip

DIRECT = "DIRECT"
TRANSFER = "TRANSFER"


class SnapshotIndex:
    """In-memory lookup structures for one snapshot, built once."""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.stations = snapshot.stations
        self.stops_at: dict[str, list] = defaultdict(list)
        for stop in snapshot.all_stops():
            self.stops_at[stop.station].append(stop)
        # ``reachable_from[m]``: stations that some train visits strictly after
        # a stop at m.  Used only as a pruning aid, never to assert feasibility.
        self.reachable_from: dict[str, set[str]] = defaultdict(set)
        for stops in snapshot.trains.values():
            suffix: set[str] = set()
            for stop in reversed(stops):
                self.reachable_from[stop.station] |= suffix
                suffix.add(stop.station)


def _plan_id(trip_id: str, signature: tuple) -> str:
    raw = f"{trip_id}|{signature}".encode("utf-8")
    return "P" + hashlib.sha1(raw).hexdigest()[:16]


def _sort_key(legs: tuple[Leg, ...], transfer_arrive: int | None = None) -> str:
    parts = [f"{leg.board_dep:012d}" for leg in legs]
    parts.append(
        "|".join(
            f"{leg.train_id}:{leg.board_seq:05d}:{leg.alight_seq:05d}" for leg in legs
        )
    )
    if transfer_arrive is not None:
        parts.append(f"{transfer_arrive:012d}")
    return "|".join(parts)


def _make_plan(trip: Trip, legs: tuple[Leg, ...], *, transfer: dict[str, Any] | None) -> Plan:
    signature = tuple((leg.train_id, leg.board_seq, leg.alight_seq) for leg in legs)
    return Plan(
        plan_id=_plan_id(trip.trip_id, signature),
        trip_id=trip.trip_id,
        kind=TRANSFER if transfer else DIRECT,
        legs=legs,
        transfer_station=transfer["station"] if transfer else None,
        transfer_min=transfer["min_transfer"] if transfer else None,
        transfer_arrive=transfer["arrive"] if transfer else None,
        transfer_depart=transfer["depart"] if transfer else None,
        transfer_slack=transfer["slack"] if transfer else None,
        sort_key=_sort_key(legs, transfer["arrive"] if transfer else None),
        signature=repr(signature),
    )


def explain_trip(index: SnapshotIndex, trip: Trip) -> tuple[list[Plan], dict[str, Any]]:
    """Enumerate every feasible plan for one trip.

    Returns ``(plans_sorted, diagnostics)``.  An empty list means the trip is
    unexplainable; the diagnostics say why in a machine-readable form.
    """
    snap = index.snapshot
    a, b = trip.entry_station, trip.exit_station
    diag: dict[str, Any] = {
        "entry_ready": None,
        "exit_deadline": None,
        "direct_candidates": 0,
        "transfer_candidates": 0,
        "reasons": [],
    }
    if a not in index.stations or b not in index.stations:
        diag["reasons"].append("unknown_station")
        return [], diag

    ready = trip.entry_ts + index.stations[a].entry_walk
    deadline = trip.exit_ts - index.stations[b].exit_walk
    diag["entry_ready"] = ready
    diag["exit_deadline"] = deadline
    if ready > deadline:
        diag["reasons"].append("entry_ready_after_exit_deadline")
        return [], diag

    by_sig: dict[tuple, Plan] = {}

    # ---- direct plans -----------------------------------------------------
    for board in index.stops_at.get(a, ()):  # type: ignore[arg-type]
        if board.dep_ts < ready or board.dep_ts > deadline:
            continue
        stops = snap.trains[board.train_id]
        for alight in stops[board.seq:]:
            if alight.arr_ts > deadline:
                break
            if alight.station != b:
                continue
            leg = Leg(
                train_id=board.train_id,
                board_seq=board.seq,
                alight_seq=alight.seq,
                board_station=a,
                alight_station=b,
                board_dep=board.dep_ts,
                alight_arr=alight.arr_ts,
            )
            plan = _make_plan(trip, (leg,), transfer=None)
            by_sig.setdefault(plan.signature_tuple(), plan)  # type: ignore[arg-type]
            diag["direct_candidates"] += 1

    # ---- first legs for a single transfer --------------------------------
    leg1_options: dict[str, list[tuple]] = defaultdict(list)
    for board1 in index.stops_at.get(a, ()):  # type: ignore[arg-type]
        if board1.dep_ts < ready or board1.dep_ts > deadline:
            continue
        stops1 = snap.trains[board1.train_id]
        for alight1 in stops1[board1.seq:]:
            if alight1.arr_ts > deadline:
                break
            m = alight1.station
            if b not in index.reachable_from.get(m, ()):  # type: ignore[arg-type]
                continue
            transfer_min = index.stations[m].min_transfer
            if alight1.arr_ts + transfer_min > deadline:
                continue
            leg1_options[m].append(
                (
                    alight1.arr_ts,
                    board1.train_id,
                    board1.seq,
                    alight1.seq,
                    board1.dep_ts,
                )
            )

    if not leg1_options:
        if not by_sig:
            diag["reasons"].append("no_direct_plan")
            diag["reasons"].append("no_transfer_first_leg")
        plans = sorted(by_sig.values(), key=lambda p: (p.sort_key, p.plan_id))
        return plans, diag

    # ---- second legs, grouped by transfer station -----------------------
    reachable_from_entry = index.reachable_from.get(a, set())  # type: ignore[arg-type]
    leg2_options: dict[str, list[tuple]] = defaultdict(list)
    for alight2 in index.stops_at.get(b, ()):  # type: ignore[arg-type]
        if alight2.arr_ts > deadline:
            continue
        stops2 = snap.trains[alight2.train_id]
        for board2 in stops2[: alight2.seq - 1]:
            m = board2.station
            if m not in reachable_from_entry:
                continue
            if board2.dep_ts > deadline:
                break  # departures are non-decreasing along the train
            if board2.dep_ts < ready + index.stations[m].min_transfer:
                continue
            leg2_options[m].append(
                (
                    board2.dep_ts,
                    board2.train_id,
                    board2.seq,
                    alight2.seq,
                    alight2.arr_ts,
                )
            )

    # ---- join with a monotone sweep per transfer station -----------------
    for m, l1s in leg1_options.items():
        l2s = leg2_options.get(m)
        if not l2s:
            continue
        transfer_min = index.stations[m].min_transfer
        l1s.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
        l2s.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
        p = 0
        n2 = len(l2s)
        for arr1, train1, bseq1, aseq1, dep1 in l1s:
            required = arr1 + transfer_min
            while p < n2 and l2s[p][0] < required:
                p += 1
            if p >= n2:
                break
            for q in range(p, n2):
                dep2, train2, bseq2, aseq2, arr2 = l2s[q]
                if train2 == train1:
                    continue
                leg1 = Leg(train1, bseq1, aseq1, a, m, dep1, arr1)
                leg2 = Leg(train2, bseq2, aseq2, m, b, dep2, arr2)
                plan = _make_plan(
                    trip,
                    (leg1, leg2),
                    transfer={
                        "station": m,
                        "min_transfer": transfer_min,
                        "arrive": arr1,
                        "depart": dep2,
                        "slack": dep2 - arr1 - transfer_min,
                    },
                )
                by_sig.setdefault(plan.signature_tuple(), plan)
                diag["transfer_candidates"] += 1

    diag["distinct_direct"] = sum(1 for p in by_sig.values() if p.kind == DIRECT)
    diag["distinct_transfer"] = sum(1 for p in by_sig.values() if p.kind == TRANSFER)
    if not by_sig:
        diag["reasons"].append("no_feasible_plan")
    plans = sorted(by_sig.values(), key=lambda p: (p.sort_key, p.plan_id))
    return plans, diag


def explain_trips(index: SnapshotIndex, trips: Iterable[Trip]) -> list[Trip]:
    for trip in trips:
        plans, diag = explain_trip(index, trip)
        trip.plans = plans
        trip.diag = diag
    return list(trips)


# ---------------------------------------------------------------------------
# Interval bounds and witnesses
# ---------------------------------------------------------------------------

def _plan_hops(plan: Plan) -> set[tuple[str, int]]:
    hops: set[tuple[str, int]] = set()
    for leg in plan.legs:
        for k in range(leg.board_seq, leg.alight_seq):
            hops.add((leg.train_id, k))
    return hops


def _xor_digest(trip_id: str, plan_id: str) -> int:
    return int(hashlib.sha1(f"{trip_id}:{plan_id}".encode()).hexdigest()[:16], 16)


RULE_MAX = "MAX_FIRST_COVERING"
RULE_MIN = "MIN_FIRST_AVOIDING"
RULE_TEXT = {
    RULE_MAX: (
        "MAX: per trip, keep the representative unless it does not cover the "
        "interval; then choose the first plan by (sort_key, plan_id) that does"
    ),
    RULE_MIN: (
        "MIN: per trip, keep the representative unless it covers the interval; "
        "then choose the first plan by (sort_key, plan_id) that avoids it; if no "
        "plan avoids it, the representative is forced"
    ),
}


def compute_bounds(
    snapshot: Snapshot, trips: list[Trip]
) -> tuple[list[dict[str, Any]], dict[tuple, dict[str, Any]], dict[str, Any]]:
    """Compute min/max per adjacent interval plus compact witness definitions.

    Witnesses are stored as a deterministic *rule* (plus a selection digest and
    the number of overrides), not as a literal plan per trip.  The rule can be
    replayed exactly from the stored candidate plans, which keeps the warehouse
    small while remaining a complete selection witness.
    """
    min_count: Counter = Counter()
    max_count: Counter = Counter()
    max_overrides: Counter = Counter()
    min_overrides: Counter = Counter()
    max_digest: dict[tuple, int] = defaultdict(int)
    min_digest: dict[tuple, int] = defaultdict(int)

    for trip in trips:
        if not trip.plans:
            continue
        plans = sorted(trip.plans, key=lambda p: (p.sort_key, p.plan_id))
        hop_sets = [_plan_hops(p) for p in plans]
        rep_hops = hop_sets[0]
        union: set[tuple[str, int]] = set()
        for hops in hop_sets:
            union |= hops
        inter = set(hop_sets[0])
        for hops in hop_sets[1:]:
            inter &= hops

        for iv in union:
            max_count[iv] += 1
            if iv not in rep_hops:
                for idx, hops in enumerate(hop_sets):
                    if iv in hops:
                        max_overrides[iv] += 1
                        max_digest[iv] ^= _xor_digest(trip.trip_id, plans[idx].plan_id)
                        break
        for iv in inter:
            min_count[iv] += 1
        for iv in union - inter:
            if iv in rep_hops:
                for idx, hops in enumerate(hop_sets):
                    if iv not in hops:
                        min_overrides[iv] += 1
                        min_digest[iv] ^= _xor_digest(trip.trip_id, plans[idx].plan_id)
                        break

    flow_rows: list[dict[str, Any]] = []
    witness_defs: dict[tuple, dict[str, Any]] = {}
    for train_id, stops in snapshot.trains.items():
        for k in range(1, len(stops)):
            iv = (train_id, k)
            mn = int(min_count.get(iv, 0))
            mx = int(max_count.get(iv, 0))
            row = {
                "train_id": train_id,
                "from_seq": k,
                "to_seq": k + 1,
                "station_from": stops[k - 1].station,
                "station_to": stops[k].station,
                "min_count": mn,
                "max_count": mx,
            }
            for bound, count, n_over, digest in (
                ("MIN", mn, min_overrides.get(iv, 0), min_digest.get(iv, 0)),
                ("MAX", mx, max_overrides.get(iv, 0), max_digest.get(iv, 0)),
            ):
                rule = RULE_MIN if bound == "MIN" else RULE_MAX
                witness_defs[(train_id, k, bound)] = {
                    "bound": bound,
                    "count": count,
                    "n_overrides": int(n_over),
                    "digest": f"{digest:016x}",
                    "rule": rule,
                    "description": (
                        f"{bound} witness for {train_id} interval "
                        f"{row['station_from']}->{row['station_to']} "
                        f"(seq {k}->{k + 1}) = {count}; independent per-trip choices, "
                        "not a single physical load"
                    ),
                }
            flow_rows.append(row)

    stats = {
        "n_intervals": len(flow_rows),
        "n_explainable_intervals": sum(1 for r in flow_rows if r["max_count"] > 0),
        "n_witnesses": len(witness_defs),
        "max_possible_bound": max((r["max_count"] for r in flow_rows), default=0),
    }
    return flow_rows, witness_defs, stats


def persist_analysis(
    conn: sqlite3.Connection,
    snapshot: Snapshot,
    import_id: int,
    trips: list[Trip],
    unclosed: list[dict[str, Any]],
    orphans: list[dict[str, Any]],
    name: str,
) -> int:
    flow_rows, witness_defs, flow_stats = compute_bounds(snapshot, trips)
    explainable = sum(1 for t in trips if t.plans)
    n_plans = sum(len(t.plans) for t in trips)
    stats = {
        "n_trips": len(trips),
        "n_explainable": explainable,
        "n_unexplainable": len(trips) - explainable,
        "n_plans": n_plans,
        "n_unclosed_entries": len(unclosed),
        "n_orphan_exits": len(orphans),
        **flow_stats,
    }
    with conn:
        cur = conn.execute(
            "INSERT INTO analysis_batches(snapshot_id, import_id, name, created_at, "
            "status, n_trips, n_explainable, n_unexplainable, n_plans, n_intervals, "
            "stats_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot.snapshot_id,
                import_id,
                name,
                utcnow(),
                "OK",
                len(trips),
                explainable,
                len(trips) - explainable,
                n_plans,
                len(flow_rows),
                dumps(stats),
            ),
        )
        batch_id = int(cur.lastrowid)

        for item in unclosed:
            conn.execute(
                "INSERT INTO unclosed_entries(batch_id, card_id, event_id, station, ts, "
                "reason) VALUES(?,?,?,?,?,?)",
                (batch_id, item["card_id"], item["event_id"], item["station"],
                 item["ts"], item["reason"]),
            )
        for item in orphans:
            conn.execute(
                "INSERT INTO orphan_exits(batch_id, card_id, event_id, station, ts, "
                "reason) VALUES(?,?,?,?,?,?)",
                (batch_id, item["card_id"], item["event_id"], item["station"],
                 item["ts"], item["reason"]),
            )

        trip_rows = []
        plan_rows = []
        use_rows = []
        for trip in trips:
            best = trip.best_plan()
            n_direct = sum(1 for p in trip.plans if p.kind == DIRECT)
            n_transfer = sum(1 for p in trip.plans if p.kind == TRANSFER)
            trip_rows.append(
                (
                    batch_id, trip.trip_id, trip.card_id, trip.entry_event_id,
                    trip.exit_event_id, trip.entry_station, trip.entry_ts,
                    trip.exit_station, trip.exit_ts, trip.status, len(trip.plans),
                    n_direct, n_transfer, best.plan_id if best else None,
                    dumps(trip.diag),
                )
            )
            for plan in trip.plans:
                legs = plan.legs
                l1 = legs[0]
                l2 = legs[1] if len(legs) > 1 else None
                plan_rows.append(
                    (
                        batch_id, trip.trip_id, plan.plan_id, plan.kind,
                        l1.train_id, l1.board_seq, l1.alight_seq,
                        l1.board_station, l1.alight_station,
                        l1.board_dep, l1.alight_arr,
                        l2.train_id if l2 else None,
                        l2.board_seq if l2 else None,
                        l2.alight_seq if l2 else None,
                        l2.board_station if l2 else None,
                        l2.alight_station if l2 else None,
                        l2.board_dep if l2 else None,
                        l2.alight_arr if l2 else None,
                        plan.transfer_station, plan.transfer_min,
                        plan.transfer_slack, plan.signature, plan.sort_key,
                    )
                )
                for leg_no, leg in enumerate(legs, start=1):
                    for k in range(leg.board_seq, leg.alight_seq):
                        use_rows.append(
                            (batch_id, trip.trip_id, plan.plan_id, leg_no,
                             leg.train_id, k, k + 1)
                        )
        conn.executemany(
            "INSERT INTO trip_pairs(batch_id, trip_id, card_id, entry_event_id, "
            "exit_event_id, entry_station, entry_ts, exit_station, exit_ts, status, "
            "n_plans, n_direct, n_transfer, baseline_plan_id, diag_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            trip_rows,
        )
        conn.executemany(
            "INSERT INTO candidate_plans(batch_id, trip_id, plan_id, kind, train1, "
            "board_seq1, alight_seq1, board_station1, alight_station1, board_dep1, "
            "alight_arr1, train2, board_seq2, alight_seq2, board_station2, "
            "alight_station2, board_dep2, alight_arr2, transfer_station, transfer_min, "
            "transfer_slack, signature, sort_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            plan_rows,
        )
        conn.executemany(
            "INSERT INTO plan_interval_uses(batch_id, trip_id, plan_id, leg_no, "
            "train_id, from_seq, to_seq) VALUES(?,?,?,?,?,?,?)",
            use_rows,
        )

        witness_ids: dict[tuple, int] = {}
        for key, wd in witness_defs.items():
            cur = conn.execute(
                "INSERT INTO witness_sets(batch_id, train_id, from_seq, bound, "
                "n_overrides, rule, selection_digest, description) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (batch_id, key[0], key[1], key[2], wd["n_overrides"], wd["rule"],
                 wd["digest"], wd["description"]),
            )
            witness_ids[key] = int(cur.lastrowid)
        flow_rows_db = [
            (
                batch_id, row["train_id"], row["from_seq"], row["to_seq"],
                row["station_from"], row["station_to"], row["min_count"],
                row["max_count"],
                witness_ids[(row["train_id"], row["from_seq"], "MIN")],
                witness_ids[(row["train_id"], row["from_seq"], "MAX")],
            )
            for row in flow_rows
        ]
        conn.executemany(
            "INSERT INTO interval_flows(batch_id, train_id, from_seq, to_seq, "
            "station_from, station_to, min_count, max_count, min_witness_id, "
            "max_witness_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            flow_rows_db,
        )
        conn.execute(
            "UPDATE analysis_batches SET stats_json=? WHERE batch_id=?",
            (dumps(stats), batch_id),
        )
    return batch_id


def import_gate_and_analyze(
    conn: sqlite3.Connection,
    snapshot_id: int,
    import_id: int,
    name: str,
) -> int:
    """Full call chain: load snapshot + gate events, pair, explain, persist."""
    from .gate import pairing_for_import

    snapshot = load_snapshot(conn, snapshot_id)
    index = SnapshotIndex(snapshot)
    trips, unclosed, orphans = pairing_for_import(conn, import_id)
    explain_trips(index, trips)
    return persist_analysis(conn, snapshot, import_id, trips, unclosed, orphans, name)
