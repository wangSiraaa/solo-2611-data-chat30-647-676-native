"""Independent verification pass over the generated artifacts.

This module deliberately re-derives the truth instead of trusting the ingest
code path:

* every archive member is parsed with the standard-library ``csv`` module and
  compared row-by-row (values, Python types and physical line numbers) against
  ``ground_truth.json``;
* every SQLite row is traced through ``_zs_row_source`` back to its
  ``entry_id`` and line, the original member is re-read by id, and the row is
  compared again after the database has been closed and reopened;
* every adversarial archive and unsafe delivery manifest is exercised and its
  rejection recorded.

Run ``python3 -m archive_service.verify`` to rewrite
``artifacts/demo/verification_report.json``.  Exit status is non-zero if any
check fails.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

from .delivery import build_delivery_manifest, canonical_relative_path, deliver
from .generate import BAD_DIR, DEMO_DIR, build_delivery
from .ingest import Warehouse, load_manifest_from_dict
from .zip_service import ArchiveReader, ArchiveRejected, DeliveryError

REPORT_PATH = os.path.join(DEMO_DIR, "verification_report.json")


def independent_rows(body: bytes, types: List[str]) -> List[Tuple[int, Tuple]]:
    """Parse a member with the standard library only."""

    reader = csv.reader(io.StringIO(body.decode("utf-8"), newline=""))
    next(reader)
    line = reader.line_num
    rows = []
    for fields in reader:
        start = line + 1
        line = reader.line_num
        converted = []
        for raw, declared in zip(fields, types):
            if raw == "\\N":
                converted.append(None)
            elif declared == "INTEGER":
                converted.append(int(raw))
            elif declared == "REAL":
                converted.append(float(raw))
            else:
                converted.append(raw)
        rows.append((start, tuple(converted)))
    return rows


def _types_by_table(manifest_payload: dict) -> Dict[str, List[str]]:
    return {
        t["table"]: [c[1] for c in t["columns"]] for t in manifest_payload["tables"]
    }


def _entry_ids_by_table(manifest_payload: dict) -> Dict[str, List[str]]:
    return {t["table"]: list(t["entry_ids"]) for t in manifest_payload["tables"]}


def verify_demo(summary: Optional[dict] = None, *, db_path: Optional[str] = None) -> dict:
    summary = summary or build_delivery()
    archive_path = summary["archive"]
    with open(summary["truth"], encoding="utf-8") as fh:
        truth = json.load(fh)
    with open(summary["manifest"], encoding="utf-8") as fh:
        manifest_payload = json.load(fh)
    manifest = load_manifest_from_dict(manifest_payload)
    types = _types_by_table(manifest_payload)
    ids_by_table = _entry_ids_by_table(manifest_payload)

    failures: List[str] = []
    member_checks = 0
    row_checks = 0
    type_checks = 0
    provenance_checks = 0

    reader = ArchiveReader.open(archive_path)

    # 1. Independent member parsing vs generated truth.
    for table, info in truth["tables"].items():
        for entry_id, expected in info["members"].items():
            parsed = independent_rows(reader.read_entry(entry_id), types[table])
            member_checks += 1
            if len(parsed) != len(expected):
                failures.append(f"{entry_id}: row count {len(parsed)} != {len(expected)}")
                continue
            for (line, row), exp in zip(parsed, expected):
                row_checks += 1
                if list(row) != exp["row"]:
                    failures.append(f"{entry_id} line {line}: row mismatch")
                if line != exp["line"]:
                    failures.append(f"{entry_id}: line {line} != {exp['line']}")
                type_checks += len(row)
    # Decoy members are readable but must never appear in the manifest.
    for entry_id in truth["decoy_member"]:
        if any(entry_id in ids for ids in ids_by_table.values()):
            failures.append(f"decoy {entry_id} is bound in the load manifest")

    # 2. Ingest, close, reopen, and trace every row back to its member.
    target_db = db_path or os.path.join(DEMO_DIR, "warehouse.sqlite")
    if os.path.exists(target_db):
        os.unlink(target_db)
    with Warehouse(target_db) as warehouse:
        report = warehouse.ingest(reader, manifest)
        if not report.ok:
            failures.append(f"ingest failed: {report.as_dict()}")
        for table, info in truth["tables"].items():
            expected_rows = [tuple(r) for r in info["all_rows"]]
            rows = warehouse.table_rows(table)
            if rows != expected_rows:
                failures.append(f"{table}: database rows differ from truth")
            for got, exp in zip(rows, expected_rows):
                if [type(v) for v in got] != [type(v) for v in exp]:
                    failures.append(f"{table}: Python types differ")

    del warehouse
    conn = sqlite3.connect(target_db)
    try:
        for table, info in truth["tables"].items():
            expected_rows = [tuple(r) for r in info["all_rows"]]
            rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            if [tuple(r) for r in rows] != expected_rows:
                failures.append(f"{table}: rows differ after reopen")
            expected_prov = []
            for entry_id in ids_by_table[table]:
                expected_prov.extend(
                    (entry_id, m["line"]) for m in info["members"][entry_id]
                )
            prov = conn.execute(
                "SELECT rowid_value, entry_id, source_line FROM _zs_row_source "
                "WHERE table_name=? ORDER BY rowid_value",
                (table,),
            ).fetchall()
            if [(e, l) for _r, e, l in prov] != expected_prov:
                failures.append(f"{table}: provenance ledger differs")
            for rowid, entry_id, line in prov:
                parsed = independent_rows(
                    reader.read_entry(entry_id), types[table]
                )
                matches = [r for start, r in parsed if start == line]
                provenance_checks += 1
                if len(matches) != 1:
                    failures.append(f"{table} row {rowid}: cannot re-locate source line")
                    continue
                if tuple(rows[rowid - 1]) != matches[0]:
                    failures.append(f"{table} row {rowid}: source re-read mismatch")
    finally:
        conn.close()

    # 3. Delivery from explicit ids reproduces every body.
    delivery_root = os.path.join(DEMO_DIR, "delivered")
    if os.path.isdir(delivery_root):
        import shutil

        shutil.rmtree(delivery_root)
    items = [
        (e.entry_id, f"{e.index:02d}/{e.name}") for e in reader.list_entries()
    ]
    delivered = deliver(reader, build_delivery_manifest(reader, items), delivery_root)
    for entry_id, rel, length in delivered:
        path = os.path.join(delivery_root, *rel.split("/"))
        with open(path, "rb") as fh:
            body = fh.read()
        if body != reader.read_entry(entry_id) or length != len(body):
            failures.append(f"delivery mismatch for {rel}")

    # 4. Adversarial archives and unsafe delivery paths.
    adversarial = {}
    expectations = {
        "duplicate_name.zip": "accepted-duplicate-names",
        "data_descriptor.zip": "accepted-descriptor",
        "forged_length.zip": "rejected",
        "fake_central_length.zip": "rejected",
        "crc_corruption.zip": "member-crc-failure",
        "overlap.zip": "rejected",
        "bad_utf8_name.zip": "rejected",
    }
    for name, expectation in expectations.items():
        path = os.path.join(BAD_DIR, name)
        outcome: Dict[str, Any] = {"expectation": expectation}
        try:
            bad_reader = ArchiveReader.open(path)
        except ArchiveRejected as exc:
            outcome["result"] = "rejected"
            outcome["reason"] = str(exc)
            if expectation != "rejected":
                failures.append(f"{name}: unexpectedly rejected ({exc})")
        else:
            if expectation == "duplicate_names":
                pass
            outcome["result"] = "opened"
            if expectation == "accepted-duplicate-names":
                infos = bad_reader.list_entries()
                outcome["names"] = [e.name for e in infos]
                outcome["bodies_distinct"] = len(
                    {bad_reader.read_entry(e.entry_id) for e in infos}
                ) == len(infos)
            elif expectation == "accepted-descriptor":
                outcome["descriptor_members"] = [
                    e.index for e in bad_reader.list_entries() if e.has_data_descriptor
                ]
                if not outcome["descriptor_members"]:
                    failures.append(f"{name}: no descriptor member")
            elif expectation == "member-crc-failure":
                outcome["crc_failed_ids"] = sorted(bad_reader.crc_failures)
                if not bad_reader.crc_failures:
                    failures.append(f"{name}: expected a member CRC failure")
            else:
                failures.append(f"{name}: expected rejection but it opened")
        adversarial[name] = outcome

    unsafe_paths = [
        "/etc/passwd",
        "../escape.csv",
        "data//double.csv",
        "data/./x.csv",
        "data\\backslash.csv",
        "C:/drive.csv",
        "",
        "data/",
        "data/../escape.csv",
        "nested/../../escape.csv",
    ]
    path_results = {}
    for raw in unsafe_paths:
        try:
            canonical_relative_path(raw)
        except DeliveryError as exc:
            path_results[raw] = {"result": "rejected", "reason": str(exc)}
        else:
            path_results[raw] = {"result": "accepted"}
            failures.append(f"unsafe path {raw!r} was accepted")
    # Collision cases need real ids.
    ids = [e.entry_id for e in reader.list_entries()]
    collisions = {
        "duplicate-destination": [(ids[0], "a.csv"), (ids[1], "a.csv")],
        "case-insensitive": [(ids[0], "Data.csv"), (ids[1], "data.csv")],
        "file-prefix": [(ids[0], "out"), (ids[1], "out/a.csv")],
        "empty-segment": [(ids[0], "a//b.csv")],
    }
    for label, pairs in collisions.items():
        try:
            build_delivery_manifest(reader, pairs)
        except DeliveryError as exc:
            path_results[label] = {"result": "rejected", "reason": str(exc)}
        else:
            path_results[label] = {"result": "accepted"}
            failures.append(f"delivery collision {label} was accepted")

    report = {
        "ok": not failures,
        "archive_sha256": reader.archive_sha256,
        "members": len(reader.list_entries()),
        "checks": {
            "members_compared": member_checks,
            "rows_compared": row_checks,
            "typed_values_compared": type_checks,
            "provenance_rows_reverified": provenance_checks,
            "delivered_members": len(delivered),
        },
        "failures": failures,
        "adversarial_archives": adversarial,
        "delivery_paths": path_results,
        "database": os.path.relpath(target_db, os.path.dirname(DEMO_DIR)),
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def main() -> int:
    report = verify_demo()
    print(json.dumps(report["checks"], indent=2))
    print("ok" if report["ok"] else "FAILURES")
    for failure in report["failures"]:
        print(f"  - {failure}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
