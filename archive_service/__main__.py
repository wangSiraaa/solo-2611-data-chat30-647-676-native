"""Command-line access to the archive service and the warehouse loader.

Commands (all operate on a real ZIP file; no database is needed for inspecting
or reading originals):

    python3 -m archive_service list ARCHIVE
    python3 -m archive_service read ARCHIVE ENTRY_ID [OUT]
    python3 -m archive_service source-range ARCHIVE ENTRY_ID OFFSET LENGTH
    python3 -m archive_service deliver ARCHIVE MANIFEST_JSON OUT_DIR
    python3 -m archive_service ingest ARCHIVE LOAD_MANIFEST_JSON DB
    python3 -m archive_service verify

``ENTRY_ID`` is ``<archive sha256>:<central-directory index>``.  Use ``list`` to
discover ids.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .delivery import build_delivery_manifest, deliver
from .ingest import ManifestError, Warehouse, load_manifest_from_dict
from .zip_service import ArchiveRejected, ArchiveReader, ZipServiceError


def _cmd_list(args) -> int:
    reader = ArchiveReader.open(args.archive)
    print(f"archive {reader.path}")
    print(f"sha256  {reader.archive_sha256}")
    print(f"entries {len(reader.entries)}")
    for info in reader.list_entries():
        print(json.dumps(info.as_dict(), ensure_ascii=False))
    return 0


def _cmd_read(args) -> int:
    reader = ArchiveReader.open(args.archive)
    body = reader.read_entry(args.entry_id)
    if args.output:
        tmp = args.output + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, args.output)
        print(f"wrote {len(body)} bytes to {args.output}")
    else:
        sys.stdout.buffer.write(body)
    return 0


def _cmd_source_range(args) -> int:
    reader = ArchiveReader.open(args.archive)
    data = reader.read_source_range(args.entry_id, args.offset, args.length)
    sys.stdout.buffer.write(data)
    return 0


def _cmd_deliver(args) -> int:
    reader = ArchiveReader.open(args.archive)
    with open(args.manifest, encoding="utf-8") as fh:
        pairs = json.load(fh)
    if not isinstance(pairs, list):
        raise SystemExit("delivery manifest must be a JSON list of [entry_id, path]")
    items = [(entry_id, path) for entry_id, path in pairs]
    manifest = build_delivery_manifest(reader, items)
    written = deliver(reader, manifest, args.out_dir)
    for entry_id, rel, length in written:
        print(f"{entry_id}\t{rel}\t{length}")
    return 0


def _cmd_ingest(args) -> int:
    reader = ArchiveReader.open(args.archive)
    with open(args.load_manifest, encoding="utf-8") as fh:
        manifest = load_manifest_from_dict(json.load(fh))
    with Warehouse(args.db) as warehouse:
        report = warehouse.ingest(reader, manifest)
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


def _cmd_verify(args) -> int:
    from .verify import main as verify_main

    return verify_main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archive_service", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list members with stable entry ids")
    p.add_argument("archive")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("read", help="verify and read one member by entry id")
    p.add_argument("archive")
    p.add_argument("entry_id")
    p.add_argument("output", nargs="?")
    p.set_defaults(func=_cmd_read)

    p = sub.add_parser("source-range", help="read raw compressed bytes of a member")
    p.add_argument("archive")
    p.add_argument("entry_id")
    p.add_argument("offset", type=int)
    p.add_argument("length", type=int)
    p.set_defaults(func=_cmd_source_range)

    p = sub.add_parser("deliver", help="write members to safe relative paths")
    p.add_argument("archive")
    p.add_argument("manifest", help='JSON list of [entry_id, relative_path]')
    p.add_argument("out_dir")
    p.set_defaults(func=_cmd_deliver)

    p = sub.add_parser("ingest", help="load manifest tables into SQLite")
    p.add_argument("archive")
    p.add_argument("load_manifest")
    p.add_argument("db")
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser("verify", help="regenerate and verify all demo artifacts")
    p.set_defaults(func=_cmd_verify)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ArchiveRejected, ZipServiceError, ManifestError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
