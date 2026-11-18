"""End-to-end demo: validate a ZIP, deliver it safely, and load tables to SQLite.

Run from the repository root:

    python3 -m archive_service.demo

It regenerates the synthetic delivery (fixed seed), prints the archive listing,
verifies every member body, writes the safe delivery tree, ingests the manifest
into ``artifacts/demo/warehouse.sqlite`` and prints an independent row count.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

from .delivery import build_delivery_manifest, deliver
from .generate import build_delivery
from .ingest import Warehouse, load_manifest_from_dict
from .zip_service import ArchiveReader


def main() -> int:
    summary = build_delivery()
    archive_path = summary["archive"]
    manifest_path = summary["manifest"]
    demo_dir = os.path.dirname(archive_path)

    print(f"archive : {archive_path}")
    print(f"sha256  : {summary['archive_sha256']}")
    print()

    # Public list/read surface: no database required to inspect originals.
    reader = ArchiveReader.open(archive_path)  # validates every member + CRC
    print("members (central-directory order, duplicate names preserved):")
    for info in reader.list_entries():
        print(
            f"  {info.entry_id}  {info.name!r:<28} {info.method_name:<7} "
            f"body={info.source_range} out={info.uncompressed_size}B "
            f"dd={info.has_data_descriptor} utf8={info.utf8_name}"
        )
    print()

    # Both same-named members are independently readable by entry id.
    first, second = reader.list_entries()[0], reader.list_entries()[1]
    assert first.name == second.name
    assert reader.read_entry(first.entry_id) != reader.read_entry(second.entry_id)
    print(
        f"same name, distinct ids: {first.entry_id} != {second.entry_id}; "
        f"bodies differ ({len(reader.read_entry(first.entry_id))}B vs "
        f"{len(reader.read_entry(second.entry_id))}B)"
    )

    # Safe delivery from explicit entry ids.  The duplicate name cannot be used
    # as a destination key, so the manifest disambiguates by entry index.
    out_dir = os.path.join(demo_dir, "delivered")
    items = [
        (info.entry_id, f"{info.index:02d}/{info.name}")
        for info in reader.list_entries()
    ]
    manifest = build_delivery_manifest(reader, items)
    written = deliver(reader, manifest, out_dir)
    print(f"\ndelivered {len(written)} members under {out_dir}")
    for entry_id, rel, length in written:
        print(f"  {rel:<34} {length}B  <- {entry_id}")
    print()

    db_path = os.path.join(demo_dir, "warehouse.sqlite")
    if os.path.exists(db_path):
        os.unlink(db_path)
    with open(manifest_path, encoding="utf-8") as fh:
        load_manifest = load_manifest_from_dict(json.load(fh))

    with Warehouse(db_path) as warehouse:
        report = warehouse.ingest(reader, load_manifest)
        print("ingest report:", json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        for table in warehouse.tables():
            info = warehouse.table_info(table)
            print(
                f"  table {table:<10} rows={info['row_count']:<4} "
                f"members={info['member_count']} archive={info['archive_sha256'][:12]}…"
            )
            prov = warehouse.all_provenance(table)
            for rowid, entry_id, line in prov[:2]:
                print(f"    rowid {rowid} <- {entry_id} line {line}")

    # Independent check from a fresh connection.
    conn = sqlite3.connect(db_path)
    for table in ("customers", "orders", "events"):
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        print(f"reopened sqlite: {table} has {count} rows")
    conn.close()
    if not report.ok:
        print("ingest had failures", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
