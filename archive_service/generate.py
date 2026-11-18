"""Synthetic fixture generator with an independent ground-truth oracle.

Everything is deterministic (fixed seed).  The generator writes:

* ``artifacts/demo/warehouse.zip`` -- a normal delivery with two duplicate-name
  members holding *different* bodies, a data-descriptor member, UTF-8 names,
  and CSV with quoting, embedded newlines, NULL ``\\N`` and empty strings.
* a set of adversarial archives under ``artifacts/demo/bad/``;
* ``artifacts/demo/ground_truth.json`` -- rows and per-row source locations
  computed *before* any SQLite involvement.
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import shutil
import struct
import zlib
from typing import List, Optional, Sequence

from .csv_reader import NULL_TOKEN
from .fixtures import Member, ZipBuilder
from .zip_service import ArchiveReader

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_DIR = os.path.join(HERE, "artifacts", "demo")
BAD_DIR = os.path.join(DEMO_DIR, "bad")


def _csv_bytes(header: Sequence[str], rows: Sequence[Sequence[Optional[object]]]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    for row in rows:
        writer.writerow([NULL_TOKEN if v is None else str(v) for v in row])
    return buf.getvalue().encode("utf-8")


# ----------------------------------------------------------------------------
# Table definitions and deterministic data
# ----------------------------------------------------------------------------

CUSTOMERS_COLUMNS = [
    ("customer_id", "INTEGER", True),
    ("name", "TEXT", False),
    ("email", "TEXT", False),
    ("score", "REAL", False),
]
ORDERS_COLUMNS = [
    ("order_id", "INTEGER", True),
    ("customer_id", "INTEGER", False),
    ("note", "TEXT", False),
    ("amount", "REAL", False),
]
EVENTS_COLUMNS = [
    ("event_id", "INTEGER", True),
    ("kind", "TEXT", False),
    ("payload", "TEXT", False),
]

CUSTOMERS_SPEC = (["customer_id", "name", "email", "score"], CUSTOMERS_COLUMNS)
ORDERS_SPEC = (["order_id", "customer_id", "note", "amount"], ORDERS_COLUMNS)
EVENTS_SPEC = (["event_id", "kind", "payload"], EVENTS_COLUMNS)


def build_ground_truth(seed: int = 20261118):
    rng = random.Random(seed)
    customers: List[List[object]] = []
    for customer_id in range(1, 41):
        name = f"Customer {customer_id:03d} éè"
        if customer_id == 3:
            name = 'Multi, "quoted"\nline name'
        email = None if customer_id % 7 == 0 else f"c{customer_id}@example.test"
        if customer_id == 5:
            email = ""  # empty string must survive as '', not NULL
        score = None if customer_id % 5 == 0 else round(rng.uniform(0, 100), 3)
        customers.append([customer_id, name, email, score])

    orders: List[List[object]] = []
    for order_id in range(1, 61):
        customer_id = rng.randint(1, 40)
        note = None
        if order_id % 4 == 0:
            note = 'note with, comma and "quotes"'
        elif order_id % 6 == 0:
            note = "line one\nline two\r\nline three"
        elif order_id % 9 == 0:
            note = ""
        amount = round(rng.uniform(1, 500), 2)
        orders.append([order_id, customer_id, note, amount])

    events: List[List[object]] = []
    for event_id in range(1, 26):
        kind = ["click", "view", "purchase"][event_id % 3]
        payload = None if event_id % 11 == 0 else f'{{"n":{event_id},"text":"\\u00e9\\n"}}'
        events.append([event_id, kind, payload])

    return customers, orders, events


def _split(rows: Sequence[Sequence[object]], parts: int):
    size = (len(rows) + parts - 1) // parts
    return [list(rows[i : i + size]) for i in range(0, len(rows), size)]

# ----------------------------------------------------------------------------
# Demo archive
# ----------------------------------------------------------------------------

def source_line_map(header: Sequence[str], rows: Sequence[Sequence[Optional[object]]]) -> List[int]:
    """Physical line where each data record starts, for ground truth.

    Embedded newlines make the logical record index differ from the physical
    line; this tracks it exactly using the same CRLF terminator.
    """

    lines = [1]  # header on line 1
    line = 1
    for row in rows:
        line += 1
        lines.append(line)
        for value in row:
            if isinstance(value, str):
                line += value.count("\n") + value.count("\r") - value.count("\r\n")
    return lines


def make_demo_archive(path: str) -> dict:
    customers, orders, events = build_ground_truth()
    o_parts = _split(orders, 2)
    e_parts = _split(events, 1)
    # A same-named decoy with *different* content.  A name-keyed importer that
    # keeps the last member would silently load these rows instead.
    decoy_customers = [
        [900 + i, f"DECOY {i}", f"decoy{i}@example.test", float(i)]
        for i in range(5)
    ]

    builder = ZipBuilder()
    # Entry 0 and entry 1 share a name.  Identity is the central-directory
    # index, so the manifest can bind either one unambiguously.
    builder.add(
        Member(
            name="data/customers.csv",
            body=_csv_bytes(CUSTOMERS_SPEC[0], customers),
        )
    )
    builder.add(
        Member(
            name="data/customers.csv",
            body=_csv_bytes(CUSTOMERS_SPEC[0], decoy_customers),
        )
    )
    # Orders: first half stored, second half deflate with a data descriptor.
    builder.add(
        Member(
            name="data/orders-a.csv",
            body=_csv_bytes(ORDERS_SPEC[0], o_parts[0]),
            method=0,
        )
    )
    builder.add(
        Member(
            name="data/orders-b.csv",
            body=_csv_bytes(ORDERS_SPEC[0], o_parts[1]),
            method=8,
            descriptor=True,
        )
    )
    # UTF-8 member name.
    builder.add(
        Member(
            name="data/事件-é.csv",
            body=_csv_bytes(EVENTS_SPEC[0], e_parts[0]),
        )
    )
    # A UTF-8 name written *without* the EFS flag (common legacy producer).
    builder.add(
        Member(
            name="data/legacy-café.csv",
            body=_csv_bytes(EVENTS_SPEC[0], []),
            flags=0x0000,
        )
    )
    payload = builder.build(comment=b"demo archive")
    with open(path, "wb") as fh:
        fh.write(payload)
    return {
        "customers": customers,
        "decoy_customers": decoy_customers,
        "orders": {2: o_parts[0], 3: o_parts[1]},
        "events": {4: e_parts[0]},
    }


# ----------------------------------------------------------------------------
# Adversarial archives
# ----------------------------------------------------------------------------

def make_duplicate_name_archive(path: str) -> None:
    builder = ZipBuilder()
    builder.add(Member(name="same.csv", body=b"id,name\n1,first\n"))
    builder.add(Member(name="same.csv", body=b"id,name\n2,second\n"))
    _write(path, builder.build())


def make_descriptor_archive(path: str) -> None:
    builder = ZipBuilder()
    builder.add(
        Member(
            name="dd.csv",
            body=b"id,val\n1,a\n2,b\n",
            descriptor=True,
        )
    )
    builder.add(
        Member(
            name="stored.csv",
            body=b"id,val\n3,c\n",
            method=0,
        )
    )
    _write(path, builder.build())


def make_forged_length_archive(path: str, *, forged_usize: int = 5) -> None:
    builder = ZipBuilder()
    builder.add(
        Member(
            name="forged.csv",
            body=b"id,val\n1,abcdefghij\n",
            usize_override=forged_usize,
            local_usize_override=forged_usize,
        )
    )
    _write(path, builder.build())


def make_crc_corruption_archive(path: str) -> None:
    """Valid structure, but a stored body byte is flipped so CRC must fail.

    The stored member is used deliberately: the byte flip keeps the body length
    and framing intact, so the failure is a pure CRC mismatch for that member
    rather than an ambiguous deflate parse error.
    """

    # First build a normal archive's second member offset so we can flip a byte
    # without the (intentional) CRC failure confusing the probe read.
    builder = ZipBuilder()
    builder.add(Member(name="ok.csv", body=b"id,val\n1,good\n", method=0))
    bad_body = b"id,val\n2," + b"x" * 200 + b"\n"
    builder.add(Member(name="bad-tail.csv", body=bad_body, method=0))
    payload = bytearray(builder.build())
    # The second member's source range starts after its 30-byte local header
    # and 12-byte name; flip its final stored byte (the body tail).
    second_name = b"bad-tail.csv"
    assert payload[:4] == b"PK\x03\x04"
    second_local = 30 + len(b"ok.csv") + len(b"id,val\n1,good\n")
    assert payload[second_local : second_local + 4] == b"PK\x03\x04"
    body_start = second_local + 30 + len(second_name)
    payload[body_start + len(bad_body) - 1] ^= 0xFF
    _write(path, bytes(payload))


def make_fake_central_length_archive(path: str, *, forged_csize: int = 4) -> None:
    builder = ZipBuilder()
    builder.add(
        Member(
            name="short.csv",
            body=b"id,val\n1,hello world\n",
            csize_override=forged_csize,
            local_csize_override=forged_csize,
        )
    )
    _write(path, builder.build())


def _local_record(name: bytes, body: bytes, method: int = 0) -> bytes:
    crc = zlib.crc32(body) & 0xFFFFFFFF
    size = len(body)
    return (
        struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0x0800,
            method,
            0,
            0,
            crc,
            size,
            size,
            len(name),
            0,
        )
        + name
        + body
    )


def _central_record(name: bytes, body: bytes, offset: int, method: int = 0) -> bytes:
    crc = zlib.crc32(body) & 0xFFFFFFFF
    size = len(body)
    return (
        struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            0x031E,
            20,
            0x0800,
            method,
            0,
            0,
            crc,
            size,
            size,
            len(name),
            0,
            0,
            0,
            0,
            0,
            offset,
        )
        + name
    )


def make_overlap_archive(path: str) -> None:
    """Two structurally valid local records whose extents overlap.

    Member ``a``'s body is literally member ``b``'s local header plus body.
    Both local headers and both central records parse cleanly, so only an
    explicit extent-overlap check can reject this archive.
    """

    b_name = b"b.csv"
    b_body = b"id,val\n2,b\n"
    b_local = _local_record(b_name, b_body)
    a_name = b"a.csv"
    a_body = b_local  # a's body contains b's whole record
    a_local = _local_record(a_name, a_body)
    a_offset = 0
    b_offset = 30 + len(a_name)

    cd_offset = len(a_local)
    central = _central_record(a_name, a_body, a_offset) + _central_record(
        b_name, b_body, b_offset
    )
    eocd = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        2,
        2,
        len(central),
        cd_offset,
        0,
    )
    _write(path, a_local + central + eocd)


def make_bad_utf8_name_archive(path: str) -> None:
    builder = ZipBuilder()
    builder.add(
        Member(
            name="bad.csv",
            body=b"id,val\n1,x\n",
            name_encoding="latin-1",
        )
    )
    data = bytearray(builder.build())
    # Set the UTF-8 flag on both headers while the name contains invalid bytes.
    raw_name = "bad.csv".encode("latin-1")
    # Insert an invalid byte by renaming with a 0xFF byte in both copies.
    broken = b"b\xffd.csv"
    assert len(broken) == len(raw_name)
    data = data.replace(raw_name, broken)
    _write(path, bytes(data))


# ----------------------------------------------------------------------------
# Manifest and truth files
# ----------------------------------------------------------------------------

def build_demo_manifest(archive_sha: str, customers_real_index: int, orders_a: int, orders_b: int, events: int) -> dict:
    return {
        "archive_sha256": archive_sha,
        "tables": [
            {
                "table": "customers",
                "columns": CUSTOMERS_COLUMNS,
                # The decoy duplicate-name member (index 1) is intentionally
                # NOT listed; only the explicitly chosen id is loaded.
                "encoding": "utf-8",
                "entry_ids": [f"{archive_sha}:{customers_real_index}"],
            },
            {
                "table": "orders",
                "columns": ORDERS_COLUMNS,
                "encoding": "utf-8",
                "entry_ids": [
                    f"{archive_sha}:{orders_a}",
                    f"{archive_sha}:{orders_b}",
                ],
            },
            {
                "table": "events",
                "columns": EVENTS_COLUMNS,
                "encoding": "utf-8",
                "entry_ids": [f"{archive_sha}:{events}"],
            },
        ],
    }


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def build_delivery() -> dict:
    """Generate every artifact and return a summary dictionary."""

    os.makedirs(BAD_DIR, exist_ok=True)
    archive_path = os.path.join(DEMO_DIR, "warehouse.zip")
    demo_members = make_demo_archive(archive_path)
    reader = ArchiveReader.open(archive_path)
    sha = reader.archive_sha256
    entries = reader.list_entries()
    # Entry 0: genuine customers (first duplicate name); entry 1: same-named
    # decoy with different rows; entry 2: stored orders; entry 3: descriptor
    # orders; entry 4: events with a UTF-8 member name.
    assert entries[0].name == entries[1].name == "data/customers.csv"
    assert entries[3].has_data_descriptor is True
    assert entries[4].utf8_name is True

    manifest = build_demo_manifest(sha, 0, 2, 3, 4)
    manifest_path = os.path.join(DEMO_DIR, "load_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    # Delivery manifest maps explicit ids to safe names; duplicate-name decoy
    # is delivered to a *different* path, proving name is never a key.
    from .delivery import build_delivery_manifest, deliver

    # Every member is delivered exactly once under its own id-derived path.
    delivery_manifest = build_delivery_manifest(
        reader,
        [(info.entry_id, f"{info.index:02d}/{info.name}") for info in entries],
    )
    out_dir = os.path.join(DEMO_DIR, "delivered")
    shutil.rmtree(out_dir, ignore_errors=True)
    deliver(reader, delivery_manifest, out_dir)

    # Adversarial archives.
    bad_paths = {
        "duplicate_name.zip": make_duplicate_name_archive,
        "data_descriptor.zip": make_descriptor_archive,
        "forged_length.zip": make_forged_length_archive,
        "crc_corruption.zip": make_crc_corruption_archive,
        "fake_central_length.zip": make_fake_central_length_archive,
        "overlap.zip": make_overlap_archive,
        "bad_utf8_name.zip": make_bad_utf8_name_archive,
    }
    for name, fn in bad_paths.items():
        fn(os.path.join(BAD_DIR, name))

    # Ground truth: rows, per-member row counts, and physical source lines.
    customers, orders, events = build_ground_truth()
    o_parts = _split(orders, 2)
    e_parts = _split(events, 1)
    decoy_customers = demo_members["decoy_customers"]

    def source_rows(header, rows):
        starts = source_line_map(header, rows)
        return [{"line": starts[i + 1], "row": list(rows[i])} for i in range(len(rows))]

    truth = {
        "seed": 20261118,
        "archive_sha256": sha,
        "entries": [e.as_dict() for e in entries],
        "tables": {
            "customers": {
                "columns": list(CUSTOMERS_SPEC[0]),
                "all_rows": customers,
                "members": {
                    f"{sha}:0": source_rows(CUSTOMERS_SPEC[0], customers),
                },
            },
            "orders": {
                "columns": list(ORDERS_SPEC[0]),
                "all_rows": orders,
                "members": {
                    f"{sha}:2": source_rows(ORDERS_SPEC[0], o_parts[0]),
                    f"{sha}:3": source_rows(ORDERS_SPEC[0], o_parts[1]),
                },
            },
            "events": {
                "columns": list(EVENTS_SPEC[0]),
                "all_rows": events,
                "members": {
                    f"{sha}:4": source_rows(EVENTS_SPEC[0], e_parts[0]),
                },
            },
        },
        "decoy_member": {
            f"{sha}:1": source_rows(CUSTOMERS_SPEC[0], decoy_customers),
        },
    }
    truth_path = os.path.join(DEMO_DIR, "ground_truth.json")
    with open(truth_path, "w", encoding="utf-8") as fh:
        json.dump(truth, fh, ensure_ascii=False, indent=2)

    return {
        "archive": archive_path,
        "manifest": manifest_path,
        "truth": truth_path,
        "archive_sha256": sha,
        "entries": entries,
    }


if __name__ == "__main__":
    summary = build_delivery()
    print(json.dumps(
        {
            "archive": summary["archive"],
            "archive_sha256": summary["archive_sha256"],
            "entries": [e.as_dict() for e in summary["entries"]],
        },
        ensure_ascii=False,
        indent=2,
    ))
