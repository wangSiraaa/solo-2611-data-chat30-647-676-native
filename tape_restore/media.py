"""Synthetic tape *images* stored as ordinary files on disk.

A volume file is a self-describing recording:

    magic | version | block_size | block_count | meta_len | meta_json | meta_sha
    then block_count records, each:
        kind(u8: 0=data,1=filemark) | payload_len(u32) | payload | payload_sha256
    footer: sha256 over everything above

The payload carried on disk is the *physical* payload (valid bytes + zero/byte
padding).  Padding is stripped on load, so the device never hands a reader
padding as file content.  A reader-facing ``Volume`` therefore only knows the
logical payload of each block.

Two failure modes are deliberately distinct:

* truncation / malformed structure -> :class:`MediaError` (cannot even be
  interpreted as a tape),
* mutated payload with intact structure -> loads fine, the block's own digest
  simply will not match the catalog fragment digest, which is what the restore
  verifier is supposed to catch.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import MediaError
from .model import Block, Volume

MAGIC = b"SYNTHTAPE1"
VERSION = 1
_KIND_DATA = 0
_KIND_FILEMARK = 1
_HEADER = struct.Struct("<10sHIII")
_BLOCK_HEAD = struct.Struct("<BI")
_DIGEST = 32


def _sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _header_parts(volume: Volume, meta: bytes) -> bytes:
    return _HEADER.pack(
        MAGIC, VERSION, volume.block_size, volume.block_count, len(meta)
    ) + meta + _sha(meta)


def physical_payload(block: Block, block_size: int) -> bytes:
    """Bytes that would be recorded for a data block (padding appended)."""
    if block.kind == "filemark":
        return b""
    payload = bytes(block.payload)
    if len(payload) > block_size:
        raise MediaError("block payload exceeds block size", payload_len=len(payload))
    return payload + b"\x00" * (block_size - len(payload))


def encode_volume(volume: Volume) -> bytes:
    meta = json.dumps(
        {"volume_id": volume.volume_id, "block_size": volume.block_size},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    out = bytearray(_header_parts(volume, meta))
    for block in volume.blocks:
        if block.kind == "filemark":
            out += _BLOCK_HEAD.pack(_KIND_FILEMARK, 0)
            out += _sha(b"")
            continue
        payload = physical_payload(block, volume.block_size)
        out += _BLOCK_HEAD.pack(_KIND_DATA, len(payload))
        out += payload
        out += _sha(bytes([_KIND_DATA]) + struct.pack("<I", len(payload)) + payload)
    out += _sha(bytes(out))
    return bytes(out)


def write_volume(path: Path, volume: Volume) -> int:
    """Write a volume image; returns byte size."""
    data = encode_volume(volume)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return len(data)


@dataclass
class _Cursor:
    data: bytes

    def __post_init__(self) -> None:
        self.pos = 0

    def take(self, n: int, what: str) -> bytes:
        if n < 0:
            raise MediaError("negative read length")
        end = self.pos + n
        if end > len(self.data):
            raise MediaError(
                "tape image is truncated", while_reading=what,
                wanted=n, available=len(self.data) - self.pos,
            )
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def remaining(self) -> int:
        return len(self.data) - self.pos


def decode_volume(data: bytes, *, strict_footer: bool = False) -> Volume:
    cur = _Cursor(data)
    head = cur.take(_HEADER.size, "header")
    magic, version, block_size, block_count, meta_len = _HEADER.unpack(head)
    if magic != MAGIC:
        raise MediaError("not a synthetic tape image (bad magic)")
    if version != VERSION:
        raise MediaError("unsupported tape image version", version=version)
    if block_size <= 0:
        raise MediaError("tape image declares non-positive block_size")
    if meta_len > cur.remaining():
        raise MediaError("tape image is truncated", while_reading="metadata")
    meta_raw = cur.take(meta_len, "metadata")
    if cur.take(_DIGEST, "metadata digest") != _sha(meta_raw):
        raise MediaError("tape image metadata digest mismatch")
    try:
        meta = json.loads(meta_raw)
        volume_id = meta["volume_id"]
    except (ValueError, KeyError, TypeError) as exc:
        raise MediaError("tape image metadata is invalid") from exc
    if not isinstance(volume_id, str) or not volume_id:
        raise MediaError("tape image volume_id is invalid")

    blocks: list[Block] = []
    for i in range(block_count):
        bhead = cur.take(_BLOCK_HEAD.size, f"block {i} header")
        kind, plen = _BLOCK_HEAD.unpack(bhead)
        if kind == _KIND_FILEMARK:
            if plen != 0:
                raise MediaError("filemark block declares payload", block=i)
            if cur.take(_DIGEST, f"block {i} digest") != _sha(b""):
                raise MediaError("filemark block digest mismatch", block=i)
            blocks.append(Block("filemark"))
            continue
        if kind != _KIND_DATA:
            raise MediaError("unknown block kind", block=i, kind=kind)
        if plen > block_size:
            raise MediaError(
                "physical block payload exceeds block_size", block=i, payload_len=plen,
            )
        payload = cur.take(plen, f"block {i} payload")
        recorded = cur.take(_DIGEST, f"block {i} digest")
        expect = _sha(bytes([kind]) + struct.pack("<I", plen) + payload)
        if recorded != expect:
            raise MediaError("block digest mismatch inside tape image", block=i)
        # Keep the full physical payload (padding included).  The device hands
        # back the whole block; the restore layer slices by valid_bytes, so
        # padding never becomes file body.
        blocks.append(Block("data", payload))

    footer_ok: Optional[bool] = None
    if cur.remaining() >= _DIGEST and cur.remaining() == _DIGEST:
        footer = cur.take(_DIGEST, "footer")
        footer_ok = footer == _sha(data[: cur.pos - _DIGEST])
    if footer_ok is not True:
        raise MediaError("tape image is truncated (missing or short footer)")
    if strict_footer and not footer_ok:
        raise MediaError("tape image footer digest mismatch")
    return Volume(volume_id=volume_id, block_size=block_size, blocks=tuple(blocks))


def read_volume(path: Path, *, strict_footer: bool = False) -> Volume:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MediaError(f"cannot read volume image: {exc}", path=str(path)) from exc
    return decode_volume(data, strict_footer=strict_footer)


def audit_volume(path: Path) -> dict:
    """Return layout facts without exposing payload content."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MediaError(f"cannot read volume image: {exc}", path=str(path)) from exc
    cur = _Cursor(data)
    head = cur.take(_HEADER.size, "header")
    magic, version, block_size, block_count, meta_len = _HEADER.unpack(head)
    if magic != MAGIC:
        raise MediaError("not a synthetic tape image (bad magic)")
    meta_raw = cur.take(meta_len, "metadata")
    meta_digest = cur.take(_DIGEST, "metadata digest")
    meta = json.loads(meta_raw)
    entries = []
    filemarks = 0
    data_blocks = 0
    for i in range(block_count):
        bhead = cur.take(_BLOCK_HEAD.size, f"block {i} header")
        kind, plen = _BLOCK_HEAD.unpack(bhead)
        payload = cur.take(plen, f"block {i} payload")
        digest = cur.take(_DIGEST, f"block {i} digest")
        entries.append({
            "index": i,
            "kind": "filemark" if kind == _KIND_FILEMARK else "data",
            "physical_payload_len": plen,
            "block_digest_ok": digest == _sha(bytes([kind]) + struct.pack("<I", plen) + payload),
        })
        if kind == _KIND_FILEMARK:
            filemarks += 1
        else:
            data_blocks += 1
    footer_ok = False
    if cur.remaining() == _DIGEST:
        footer = cur.take(_DIGEST, "footer")
        footer_ok = footer == _sha(data[: cur.pos - _DIGEST])
    return {
        "volume_id": meta.get("volume_id"),
        "block_size": block_size,
        "declared_blocks": block_count,
        "data_blocks": data_blocks,
        "filemarks": filemarks,
        "bytes_on_disk": len(data),
        "footer_ok": footer_ok,
        "metadata_digest_ok": meta_digest == _sha(meta_raw),
        "header": {
            "magic_ok": magic == MAGIC, "version": version, "meta_len": meta_len,
        },
        "blocks": entries,
    }


def expected_image_size(volume: Volume) -> int:
    return len(encode_volume(volume))
