"""Command-line interface for the explainable metro-flow warehouse."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import analysis, catalog, db, gate, queries


def _read_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: str, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if path == "-":
        print(text)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")


def _fail(message: str, problems: list | None = None, code: int = 2) -> int:
    payload = {"ok": False, "error": message}
    if problems:
        payload["problems"] = problems
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def _open(args) -> "db.sqlite3.Connection":
    conn = db.connect(args.db)
    db.init_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    conn = _open(args)
    if args.reset:
        for table in (
            "witness_sets", "interval_flows",
            "plan_interval_uses", "candidate_plans", "unclosed_entries",
            "orphan_exits", "trip_pairs", "analysis_batches", "gate_events",
            "gate_imports", "snapshot_stops", "timetable_snapshots", "stations",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    print(json.dumps({"ok": True, "db": args.db, "schema_version": db.SCHEMA_VERSION}))
    return 0


def cmd_import_stations(args) -> int:
    conn = _open(args)
    try:
        n = catalog.upsert_stations(conn, _read_json(args.file))
    except catalog.TimetableError as exc:
        return _fail(str(exc), exc.problems)
    print(json.dumps({"ok": True, "stations": n}))
    return 0


def cmd_import_timetable(args) -> int:
    conn = _open(args)
    payload = _read_json(args.file)
    rows = payload["stops"] if isinstance(payload, dict) else payload
    try:
        snapshot_id = catalog.import_timetable(
            conn, rows, name=args.name, note=args.note
        )
    except catalog.TimetableError as exc:
        return _fail(str(exc), exc.problems)
    print(json.dumps({"ok": True, "snapshot_id": snapshot_id}))
    return 0


def cmd_import_gates(args) -> int:
    conn = _open(args)
    payload = _read_json(args.file)
    rows = payload["events"] if isinstance(payload, dict) else payload
    try:
        summary = gate.store_import(conn, rows, name=args.name)
    except gate.GateDataError as exc:
        return _fail(str(exc), exc.problems)
    print(json.dumps({"ok": True, **summary}))
    return 0


def cmd_correct(args) -> int:
    conn = _open(args)
    payload = _read_json(args.file)
    try:
        snapshot_id = catalog.correct_train_times(
            conn,
            args.snapshot,
            args.train,
            payload["changes"] if isinstance(payload, dict) else payload,
            name=args.name,
            note=args.note,
        )
    except catalog.TimetableError as exc:
        return _fail(str(exc), exc.problems)
    print(json.dumps({"ok": True, "snapshot_id": snapshot_id, "base": args.snapshot}))
    return 0


def cmd_analyze(args) -> int:
    conn = _open(args)
    try:
        batch_id = analysis.import_gate_and_analyze(
            conn, args.snapshot, args.import_id, name=args.name
        )
    except (KeyError, catalog.TimetableError) as exc:
        return _fail(str(exc))
    summary = queries.batch_summary(conn, batch_id)
    summary.pop("unclosed_entries", None)
    summary.pop("orphan_exits", None)
    print(json.dumps({"ok": True, "batch_id": batch_id, **summary["stats"],
                      "snapshot_id": args.snapshot}, indent=2))
    return 0


def cmd_snapshots(args) -> int:
    conn = _open(args)
    _write_json(args.out, catalog.list_snapshots(conn))
    return 0


def cmd_station_times(args) -> int:
    conn = _open(args)
    result = {
        "snapshot_id": args.snapshot,
        "station": args.station,
        "stops": catalog.station_times(conn, args.snapshot, args.station,
                                       limit=args.limit),
    }
    _write_json(args.out, result)
    return 0


def cmd_train_stops(args) -> int:
    conn = _open(args)
    _write_json(args.out, catalog.train_stops(conn, args.snapshot, args.train))
    return 0


def cmd_batches(args) -> int:
    conn = _open(args)
    rows = queries.list_batches(conn)
    if args.snapshot is not None:
        rows = [r for r in rows if r["snapshot_id"] == args.snapshot]
    _write_json(args.out, rows)
    return 0


def cmd_summary(args) -> int:
    conn = _open(args)
    _write_json(args.out, queries.batch_summary(conn, args.batch))
    return 0


def cmd_trips(args) -> int:
    conn = _open(args)
    _write_json(args.out, queries.list_trips(conn, args.batch, status=args.status))
    return 0


def cmd_explain(args) -> int:
    conn = _open(args)
    try:
        _write_json(args.out, queries.explain_trip(conn, args.batch, args.trip))
    except KeyError as exc:
        return _fail(str(exc), code=3)
    return 0


def cmd_interval(args) -> int:
    conn = _open(args)
    try:
        rows = queries.query_interval(conn, args.batch, args.train, args.from_seq)
    except KeyError as exc:
        return _fail(str(exc), code=3)
    _write_json(args.out, rows)
    return 0


def cmd_witness(args) -> int:
    conn = _open(args)
    try:
        result = queries.replay_witness(
            conn, args.batch, args.train, args.from_seq, args.bound,
            with_selection=not args.no_selection,
        )
    except KeyError as exc:
        return _fail(str(exc), code=3)
    _write_json(args.out, result)
    return 1 if not result["ok"] else 0


def cmd_audit(args) -> int:
    conn = _open(args)
    result = queries.audit_batch(conn, args.batch)
    _write_json(args.out, result)
    return 0 if result["ok"] else 1


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="metro",
        description="Explainable metro gate-to-train interval flow warehouse",
    )
    p.add_argument("--db", required=True, help="SQLite database path")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create/reset the schema")
    s.add_argument("--reset", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("import-stations", help="import station walking/transfer times")
    s.add_argument("file")
    s.set_defaults(func=cmd_import_stations)

    s = sub.add_parser("import-timetable", help="import stops and freeze a snapshot")
    s.add_argument("file")
    s.add_argument("--name", required=True)
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_import_timetable)

    s = sub.add_parser("import-gates", help="import gate events (whole batch validated)")
    s.add_argument("file")
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_import_gates)

    s = sub.add_parser("correct", help="correct one train's times into a new snapshot")
    s.add_argument("--snapshot", type=int, required=True)
    s.add_argument("--train", required=True)
    s.add_argument("file")
    s.add_argument("--name", required=True)
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_correct)

    s = sub.add_parser("analyze", help="pair + explain + materialise bounds")
    s.add_argument("--snapshot", type=int, required=True)
    s.add_argument("--import-id", type=int, required=True)
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("snapshots", help="list timetable snapshots")
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_snapshots)

    s = sub.add_parser("station-times", help="station-time query for a snapshot")
    s.add_argument("--snapshot", type=int, required=True)
    s.add_argument("--station", required=True)
    s.add_argument("--limit", type=int, default=1000)
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_station_times)

    s = sub.add_parser("train-stops", help="stop sequence of one train")
    s.add_argument("--snapshot", type=int, required=True)
    s.add_argument("--train", required=True)
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_train_stops)

    s = sub.add_parser("batches", help="list analysis batches")
    s.add_argument("--snapshot", type=int)
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_batches)

    s = sub.add_parser("summary", help="batch summary incl. anomalies")
    s.add_argument("--batch", type=int, required=True)
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_summary)

    s = sub.add_parser("trips", help="list trips and their plan counts")
    s.add_argument("--batch", type=int, required=True)
    s.add_argument("--status", choices=["EXPLAINABLE", "UNEXPLAINABLE"])
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_trips)

    s = sub.add_parser("explain", help="all feasible plans for one trip")
    s.add_argument("--batch", type=int, required=True)
    s.add_argument("--trip", required=True)
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser("interval", help="min/max interval flows for a train")
    s.add_argument("--batch", type=int, required=True)
    s.add_argument("--train", required=True)
    s.add_argument("--from-seq", type=int)
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_interval)

    s = sub.add_parser("witness", help="replay and verify one bound witness")
    s.add_argument("--batch", type=int, required=True)
    s.add_argument("--train", required=True)
    s.add_argument("--from-seq", type=int, required=True)
    s.add_argument("--bound", required=True, choices=["MIN", "MAX", "min", "max"])
    s.add_argument("--no-selection", action="store_true")
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_witness)

    s = sub.add_parser("audit", help="replay every stored bound/witness")
    s.add_argument("--batch", type=int, required=True)
    s.add_argument("--out", default="-")
    s.set_defaults(func=cmd_audit)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        return _fail(f"file not found: {exc}")
    except json.JSONDecodeError as exc:
        return _fail(f"invalid JSON input: {exc}")


if __name__ == "__main__":
    sys.exit(main())
