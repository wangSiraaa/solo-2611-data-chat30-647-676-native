"""Safe extraction of archive members to delivery paths.

A delivery manifest explicitly maps an ``entry_id`` to a relative destination
path.  Names are never inferred from the archive for filesystem purposes.
Paths are rejected (not silently rewritten) for absolute paths, drive/UNC
prefixes, ``..`` segments, empty segments, non-NFC or separator tricks, and
any file/directory prefix collisions across the manifest.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .zip_service import (
    ArchiveReader,
    DeliveryError,
    EntryInfo,
    EntryLimitError,
    MAX_ENTRY_BYTES,
    MAX_TOTAL_BYTES,
)


@dataclass(frozen=True)
class DeliveryItem:
    entry_id: str
    relative_path: str
    info: EntryInfo


def canonical_relative_path(raw: str) -> str:
    """Validate and normalize one manifest destination path.

    Rejection is explicit; dangerous characters are never stripped and then
    written, because that turns a rejected manifest into an untrusted mapping.
    """

    if not isinstance(raw, str) or raw == "":
        raise DeliveryError("destination path must be a non-empty string")
    if "\x00" in raw:
        raise DeliveryError("destination path contains NUL")
    if "\\" in raw:
        raise DeliveryError("destination path must use '/' separators")
    if raw.startswith("/") or os.path.isabs(raw):
        raise DeliveryError(f"absolute destination path is not allowed: {raw!r}")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise DeliveryError(f"drive-qualified destination path is not allowed: {raw!r}")
    if raw.endswith("/"):
        raise DeliveryError(f"destination path must name a file, not a directory: {raw!r}")
    parts = raw.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise DeliveryError(
                f"destination path contains an empty, '.' or '..' segment: {raw!r}"
            )
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in part):
            raise DeliveryError(f"destination path segment contains a control character: {raw!r}")
        if ":" in part:
            raise DeliveryError(f"destination path segment contains ':': {raw!r}")
        if part != unicodedata.normalize("NFC", part):
            raise DeliveryError(f"destination path segment is not NFC-normalized: {raw!r}")
        if part.strip() != part:
            raise DeliveryError(f"destination path segment has surrounding whitespace: {raw!r}")
    if raw != unicodedata.normalize("NFC", raw):
        raise DeliveryError("destination path must be NFC-normalized")
    return "/".join(parts)


def _prefix_key(path: str, casefold: bool) -> Tuple[str, ...]:
    parts = path.split("/")
    if casefold:
        parts = [part.casefold() for part in parts]
    return tuple(parts)


def build_delivery_manifest(
    reader: ArchiveReader, items: Sequence[Tuple[str, str]]
) -> Tuple[DeliveryItem, ...]:
    """Resolve explicit ``(entry_id, relative_path)`` pairs into safe items.

    All entries must exist in ``reader``.  Repeating an entry id is allowed
    (two names may deliver the same identity), but two *different* entry ids
    may not target the same destination, and no destination may be a file
    prefix of another destination.
    """

    resolved: List[DeliveryItem] = []
    canonical_seen: Dict[Tuple[str, ...], str] = {}
    casefold_seen: Dict[Tuple[str, ...], str] = {}
    id_seen: Dict[str, str] = {}
    for entry_id, raw_path in items:
        info = reader.get(entry_id)
        path = canonical_relative_path(raw_path)
        if entry_id in id_seen:
            raise DeliveryError(
                f"entry id {entry_id} is assigned more than once "
                f"({id_seen[entry_id]!r} and {path!r})"
            )
        id_seen[entry_id] = path
        key = _prefix_key(path, casefold=False)
        folded = _prefix_key(path, casefold=True)
        if key in canonical_seen:
            raise DeliveryError(f"destination {path!r} is assigned more than once")
        if folded in casefold_seen:
            raise DeliveryError(
                f"destination {path!r} collides case-insensitively with another destination"
            )
        canonical_seen[key] = entry_id
        casefold_seen[folded] = entry_id
        resolved.append(DeliveryItem(entry_id=entry_id, relative_path=path, info=info))

    all_paths = {_prefix_key(item.relative_path, casefold=False) for item in resolved}
    all_folded = {_prefix_key(item.relative_path, casefold=True) for item in resolved}
    for item in resolved:
        key = _prefix_key(item.relative_path, casefold=False)
        folded = _prefix_key(item.relative_path, casefold=True)
        for length in range(1, len(key)):
            if key[:length] in all_paths:
                raise DeliveryError(
                    f"destination {item.relative_path!r} uses a file as a directory prefix"
                )
            if folded[:length] in all_folded:
                raise DeliveryError(
                    f"destination {item.relative_path!r} has a case-insensitive "
                    "file/directory prefix collision"
                )
    return tuple(resolved)


def safe_join(root: os.PathLike | str, relative_path: str) -> str:
    """Join a canonical relative path under ``root``, verifying containment."""

    root_real = os.path.realpath(os.fspath(root))
    candidate = os.path.realpath(os.path.join(root_real, *relative_path.split("/")))
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        raise DeliveryError(f"destination escapes the delivery root: {relative_path!r}")
    return candidate


def deliver(
    reader: ArchiveReader,
    manifest: Sequence[DeliveryItem],
    root: os.PathLike | str,
) -> List[Tuple[str, str, int]]:
    """Write all manifest members efficiently, with all-or-nothing commit.

    Every body is first read through the archive service, which verifies the
    source range, explicit lengths and CRC32.  Bodies are staged as temporary
    files only after their own verification; if *any* member fails, no
    destination is committed.  Staging keeps only one body in memory at once,
    while still making a late CRC failure leave the destination untouched.
    """

    root = os.fspath(root)
    os.makedirs(root, exist_ok=True)
    destinations = [safe_join(root, item.relative_path) for item in manifest]
    parents = set()
    for dest in destinations:
        parent = os.path.dirname(dest)
        while parent and parent != os.path.realpath(root):
            if parent in destinations:
                raise DeliveryError(f"file/directory prefix conflict on disk: {dest!r}")
            parents.add(parent)
            parent = os.path.dirname(parent)
    for dest in destinations:
        if dest in parents:
            raise DeliveryError(f"file/directory prefix conflict on disk: {dest!r}")

    staged: List[Tuple[str, str, int]] = []
    total_written = 0
    try:
        for item, dest in zip(manifest, destinations):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            temp = f"{dest}.tmp-{os.getpid()}-{item.info.index}"
            written = 0
            try:
                with open(temp, "xb") as out:
                    for chunk in reader.iter_entry_chunks(item.entry_id):
                        out.write(chunk)
                        written += len(chunk)
                        if written > MAX_ENTRY_BYTES:
                            raise EntryLimitError(
                                item.entry_id, "member exceeds the 8 MiB limit"
                            )
                total_written += written
                if total_written > MAX_TOTAL_BYTES:
                    raise DeliveryError(
                        "delivery exceeds the 100 MiB total output limit"
                    )
                staged.append((temp, dest, written))
            except BaseException:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise
    except BaseException:
        for temp, _dest, _length in staged:
            try:
                os.unlink(temp)
            except OSError:
                pass
        raise

    committed: List[str] = []
    try:
        for temp, dest, _length in staged:
            os.replace(temp, dest)
            committed.append(dest)
    except BaseException:
        for path in committed:
            try:
                os.unlink(path)
            except OSError:
                pass
        for temp, _dest, _length in staged:
            try:
                os.unlink(temp)
            except OSError:
                pass
        raise
    return [
        (item.entry_id, item.relative_path, length)
        for item, (_t, _d, length) in zip(manifest, staged)
    ]
