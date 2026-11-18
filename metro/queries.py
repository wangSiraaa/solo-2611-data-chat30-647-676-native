"""Read-side queries: batches, trip explanations, interval bounds, witnesses.

Everything here reads only what is stored in SQLite, so a reopened warehouse
answers identically without the original inputs.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import loads


def list_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT b.batch_id, b.name, b.created_at, b.snapshot_id, s.name AS snapshot_name,
               b.import_id, b.status, b.n_trips, b.n_explainable,
               b.n_unexplainable, b.n_plans, b.n_intervals, b.stats_json
        FROM analysis_batches b
        JOIN timetable_snapshots s ON s.snapshot_id = b.snapshot_id
        ORDER BY b.batch_id
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stats"] = loads(d.pop("stats_json"), {})
        out.append(d)
    return out


def batch_summary(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT b.*, s.name AS snapshot_name, s.base_snapshot_id, s.note AS snapshot_note,
               gi.name AS import_name
        FROM analysis_batches b
        JOIN timetable_snapshots s ON s.snapshot_id = b.snapshot_id
        JOIN gate_imports gi ON gi.import_id = b.import_id
        WHERE b.batch_id=?
        """,
        (batch_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown batch {batch_id}")
    d = dict(row)
    d["stats"] = loads(d.pop("stats_json"), {})
    d["unclosed_entries"] = [
        dict(r)
        for r in conn.execute(
            "SELECT card_id, event_id, station, ts, reason FROM unclosed_entries "
            "WHERE batch_id=? ORDER BY ts",
            (batch_id,),
        )
    ]
    d["orphan_exits"] = [
        dict(r)
        for r in conn.execute(
            "SELECT card_id, event_id, station, ts, reason FROM orphan_exits "
            "WHERE batch_id=? ORDER BY ts",
            (batch_id,),
        )
    ]
    return d


def list_trips(conn: sqlite3.Connection, batch_id: int, status: str | None = None):
    sql = (
        "SELECT trip_id, card_id, entry_station, entry_ts, exit_station, exit_ts, "
        "status, n_plans, n_direct, n_transfer, baseline_plan_id, diag_json "
        "FROM trip_pairs WHERE batch_id=?"
    )
    params: list[Any] = [batch_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY entry_ts, trip_id"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["diag"] = loads(d.pop("diag_json"), {})
        out.append(d)
    return out


def _plan_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    legs = [
        {
            "train": r["train1"],
            "board_seq": r["board_seq1"],
            "alight_seq": r["alight_seq1"],
            "board_station": r["board_station1"],
            "alight_station": r["alight_station1"],
            "board_dep": r["board_dep1"],
            "alight_arr": r["alight_arr1"],
        }
    ]
    transfer = None
    if r["kind"] == "TRANSFER":
        legs.append(
            {
                "train": r["train2"],
                "board_seq": r["board_seq2"],
                "alight_seq": r["alight_seq2"],
                "board_station": r["board_station2"],
                "alight_station": r["alight_station2"],
                "board_dep": r["board_dep2"],
                "alight_arr": r["alight_arr2"],
            }
        )
        transfer = {
            "station": r["transfer_station"],
            "min_transfer": r["transfer_min"],
            "arrive": r["alight_arr1"],
            "depart": r["board_dep2"],
            "slack": r["transfer_slack"],
        }
    return {
        "plan_id": r["plan_id"],
        "kind": r["kind"],
        "legs": legs,
        "transfer": transfer,
        "sort_key": r["sort_key"],
    }


def load_plans(conn: sqlite3.Connection, batch_id: int, trip_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM candidate_plans WHERE batch_id=? AND trip_id=? "
        "ORDER BY sort_key, plan_id",
        (batch_id, trip_id),
    ).fetchall()
    return [_plan_row_to_dict(r) for r in rows]


def explain_trip(conn: sqlite3.Connection, batch_id: int, trip_id: str) -> dict[str, Any]:
    trip = conn.execute(
        "SELECT * FROM trip_pairs WHERE batch_id=? AND trip_id=?",
        (batch_id, trip_id),
    ).fetchone()
    if trip is None:
        raise KeyError(f"unknown trip {trip_id} in batch {batch_id}")
    d = dict(trip)
    d["diag"] = loads(d.pop("diag_json"), {})
    d["plans"] = load_plans(conn, batch_id, trip_id)
    d["interpretation"] = (
        "All plans are feasible explanations under the stated assumptions. "
        "No single plan is asserted to be the passenger's actual ride; the "
        "baseline plan is only a deterministic representative."
        if d["plans"]
        else "No feasible ride plan exists for this trip. The trip is reported "
        "as unexplainable rather than dropped or forced into a pairing."
    )
    return d


def query_interval(
    conn: sqlite3.Connection,
    batch_id: int,
    train_id: str,
    from_seq: int | None = None,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT train_id, from_seq, to_seq, station_from, station_to, min_count, "
        "max_count, min_witness_id, max_witness_id FROM interval_flows "
        "WHERE batch_id=? AND train_id=?"
    )
    params: list[Any] = [batch_id, train_id]
    if from_seq is not None:
        sql += " AND from_seq=?"
        params.append(from_seq)
    sql += " ORDER BY from_seq"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        exists = conn.execute(
            "SELECT 1 FROM analysis_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if not exists:
            raise KeyError(f"unknown batch {batch_id}")
    out = []
    for r in rows:
        d = dict(r)
        d["note"] = (
            "min/max over independently chosen feasible explanations; the two "
            "bounds and different intervals need not come from one selection"
        )
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Witness definitions and replay
# ---------------------------------------------------------------------------

def witness_definition(
    conn: sqlite3.Connection,
    batch_id: int,
    train_id: str,
    from_seq: int,
    bound: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM witness_sets WHERE batch_id=? AND train_id=? AND from_seq=? "
        "AND bound=?",
        (batch_id, train_id, from_seq, bound.upper()),
    ).fetchone()
    if row is None:
        raise KeyError(
            f"no {bound} witness for {train_id} seq {from_seq} in batch {batch_id}"
        )
    return dict(row)


def _plan_covers(plan: dict[str, Any], train_id: str, from_seq: int) -> bool:
    for leg in plan["legs"]:
        if leg["train"] == train_id and leg["board_seq"] <= from_seq < leg["alight_seq"]:
            return True
    return False


def select_plan(plans: list[dict[str, Any]], train_id: str, from_seq: int, bound: str):
    """Apply a stored witness rule to one trip's canonical plans.

    ``plans`` must be sorted by ``(sort_key, plan_id)``; index 0 is the
    representative.  Returns ``(plan, override?)``.
    """
    rep = plans[0]
    if bound == "MAX":
        if _plan_covers(rep, train_id, from_seq):
            return rep, False
        for plan in plans:
            if _plan_covers(plan, train_id, from_seq):
                return plan, True
        return rep, False
    # MIN
    if not _plan_covers(rep, train_id, from_seq):
        return rep, False
    for plan in plans:
        if not _plan_covers(plan, train_id, from_seq):
            return plan, True
    return rep, False


def replay_witness(
    conn: sqlite3.Connection,
    batch_id: int,
    train_id: str,
    from_seq: int,
    bound: str,
    *,
    with_selection: bool = True,
) -> dict[str, Any]:
    """Replay a compact witness from stored candidate plans and check it.

    Confirms (a) the selection really reaches the reported bound and (b) its
    digest matches the stored digest.
    """
    definition = witness_definition(conn, batch_id, train_id, from_seq, bound)
    flow = conn.execute(
        "SELECT * FROM interval_flows WHERE batch_id=? AND train_id=? AND from_seq=?",
        (batch_id, train_id, from_seq),
    ).fetchone()
    expected = flow["max_count"] if bound.upper() == "MAX" else flow["min_count"]
    grouped = _load_all_plans_grouped(conn, batch_id)
    count = 0
    overrides = 0
    digest = 0
    selection = []
    for trip_id in sorted(grouped):
        plans = grouped[trip_id]
        if not plans:
            continue
        plan, is_override = select_plan(plans, train_id, from_seq, bound.upper())
        covers = _plan_covers(plan, train_id, from_seq)
        if covers:
            count += 1
        if is_override:
            overrides += 1
            digest ^= _xor_digest(trip_id, plan["plan_id"])
        if with_selection and covers:
            selection.append(
                {
                    "trip_id": trip_id,
                    "plan_id": plan["plan_id"],
                    "kind": plan["kind"],
                    "override": is_override,
                    "legs": [
                        f"{l['train']}[{l['board_seq']}->{l['alight_seq']}]"
                        for l in plan["legs"]
                    ],
                }
            )
    ok = count == expected
    digest_ok = f"{digest:016x}" == definition["selection_digest"]
    return {
        "witness_id": definition["witness_id"],
        "train_id": train_id,
        "from_seq": from_seq,
        "bound": bound.upper(),
        "expected_count": expected,
        "replayed_count": count,
        "count_ok": ok,
        "rule": definition["rule"],
        "n_overrides_stored": definition["n_overrides"],
        "n_overrides_replayed": overrides,
        "digest_ok": digest_ok,
        "ok": ok and digest_ok and overrides == definition["n_overrides"],
        "selection": selection if with_selection else [],
        "note": definition["description"],
    }


def _load_all_plans_grouped(conn: sqlite3.Connection, batch_id: int):
    """Load every stored candidate plan once, grouped by trip, already sorted."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        "SELECT * FROM candidate_plans WHERE batch_id=? ORDER BY trip_id, sort_key, plan_id",
        (batch_id,),
    ).fetchall()
    for r in rows:
        grouped.setdefault(r["trip_id"], []).append(_plan_row_to_dict(r))
    return grouped


def _hops(plan: dict[str, Any]) -> set[tuple[str, int]]:
    hops: set[tuple[str, int]] = set()
    for leg in plan["legs"]:
        for k in range(leg["board_seq"], leg["alight_seq"]):
            hops.add((leg["train"], k))
    return hops


def _xor_digest(trip_id: str, plan_id: str) -> int:
    import hashlib

    return int(hashlib.sha1(f"{trip_id}:{plan_id}".encode()).hexdigest()[:16], 16)


def audit_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any]:
    """Replay every stored interval bound/witness from persisted plans in one pass.

    This re-derives min/max and the witness override counts/digests straight
    from ``candidate_plans`` and compares them with what was materialised.
    """
    from collections import Counter, defaultdict

    exists = conn.execute(
        "SELECT 1 FROM analysis_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if not exists:
        raise KeyError(f"unknown batch {batch_id}")

    grouped = _load_all_plans_grouped(conn, batch_id)
    min_count: Counter = Counter()
    max_count: Counter = Counter()
    max_over: Counter = Counter()
    min_over: Counter = Counter()
    max_digest: dict[tuple, int] = defaultdict(int)
    min_digest: dict[tuple, int] = defaultdict(int)

    for trip_id, plans in grouped.items():
        hop_sets = [_hops(p) for p in plans]
        rep = hop_sets[0]
        union: set[tuple[str, int]] = set()
        for h in hop_sets:
            union |= h
        inter = set(hop_sets[0])
        for h in hop_sets[1:]:
            inter &= h
        for iv in union:
            max_count[iv] += 1
            if iv not in rep:
                for idx, hs in enumerate(hop_sets):
                    if iv in hs:
                        max_over[iv] += 1
                        max_digest[iv] ^= _xor_digest(trip_id, plans[idx]["plan_id"])
                        break
        for iv in inter:
            min_count[iv] += 1
        for iv in union - inter:
            if iv in rep:
                for idx, hs in enumerate(hop_sets):
                    if iv not in hs:
                        min_over[iv] += 1
                        min_digest[iv] ^= _xor_digest(trip_id, plans[idx]["plan_id"])
                        break

    flows = conn.execute(
        "SELECT * FROM interval_flows WHERE batch_id=?", (batch_id,)
    ).fetchall()
    witness = {
        (r["train_id"], r["from_seq"], r["bound"]): r
        for r in conn.execute(
            "SELECT * FROM witness_sets WHERE batch_id=?", (batch_id,)
        )
    }
    failures: list[dict[str, Any]] = []
    for f in flows:
        iv = (f["train_id"], f["from_seq"])
        checks = (
            ("MIN", f["min_count"], min_count.get(iv, 0), min_over.get(iv, 0),
             min_digest.get(iv, 0)),
            ("MAX", f["max_count"], max_count.get(iv, 0), max_over.get(iv, 0),
             max_digest.get(iv, 0)),
        )
        for bound, stored, replayed, over, digest in checks:
            problems = []
            if stored != replayed:
                problems.append(f"count {stored}->{replayed}")
            w = witness.get((f["train_id"], f["from_seq"], bound))
            if w is None:
                problems.append("missing witness")
            else:
                if w["n_overrides"] != over:
                    problems.append(
                        f"overrides {w['n_overrides']}->{over}"
                    )
                if f"{digest:016x}" != w["selection_digest"]:
                    problems.append(
                        f"digest {w['selection_digest']}->{digest:016x}"
                    )
            if problems:
                failures.append(
                    {"train_id": f["train_id"], "from_seq": f["from_seq"],
                     "bound": bound, "problems": problems}
                )

    return {
        "batch_id": batch_id,
        "n_intervals": len(flows),
        "n_checked": len(flows) * 2,
        "n_trips_replayed": len(grouped),
        "n_plans_replayed": sum(len(p) for p in grouped.values()),
        "n_failures": len(failures),
        "ok": not failures,
        "failures": failures[:20],
    }
