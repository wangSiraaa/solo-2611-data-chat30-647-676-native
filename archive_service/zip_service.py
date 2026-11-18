"""Strict, identity-stable ZIP archive reader.

Member identity is ``<archive sha256>:<central-directory-index>``.  A name is
never used as a unique key: duplicate member names are preserved, and every
read resolves the exact central-directory record that produced the entry id.

The parser deliberately does *not* search for the next ``PK`` byte after a
broken record.  Central-directory records, local headers, explicit compressed
sizes, optional data descriptors, and the end-of-central-directory record all
have to agree.  Entry extents may not overlap each other or the central
directory.  Supported archives are stored/deflate, unencrypted, single-volume,
non-ZIP64 ZIP files.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import zlib
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
# A compressed body can be slightly larger than its 8 MiB uncompressed limit
# (deflate framing on incompressible data).  Bound raw source-range reads a
# little above the uncompressed cap so they remain usable.
MAX_SOURCE_RANGE_BYTES = MAX_ENTRY_BYTES + 1024 * 1024

_LOCAL_SIG = 0x04034B50
_CENTRAL_SIG = 0x02014B50
_EOCD_SIG = 0x06054B50
_DD_SIG = 0x08074B50
_ARCHIVE_EXTRA_SIG = 0x08064B50
_CD_DIGITAL_SIG = 0x05054B50

_ALLOWED_FLAGS = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 11)
_UTF8_FLAG = 1 << 11
_DESCRIPTOR_FLAG = 1 << 3

_STORED = 0
_DEFLATED = 8
_METHOD_NAMES = {_STORED: "stored", _DEFLATED: "deflate"}


class ZipServiceError(Exception):
    """Base class for archive-service failures."""


class ArchiveRejected(ZipServiceError):
    """The archive is structurally unsafe, malformed, or unsupported."""


class ArchiveChanged(ZipServiceError):
    """The file changed after the archive view was opened."""


class EntryNotFound(ZipServiceError):
    """No member with the supplied entry id exists in this archive view."""


class EntryReadError(ZipServiceError):
    """Base class for an error bound to one archive member."""

    def __init__(self, entry_id: str, message: str):
        super().__init__(f"{entry_id}: {message}")
        self.entry_id = entry_id
        self.message = message


class EntryChecksumError(EntryReadError):
    """The decompressed payload does not match the declared CRC32."""


class EntryDataError(EntryReadError):
    """Stored/deflate payload is truncated or otherwise malformed."""


class EntryInflateLengthError(EntryReadError):
    """A member produced more bytes than its declared uncompressed size."""


class EntryLimitError(EntryReadError):
    """A member exceeds the configured 8 MiB / archive 100 MiB limits."""


class DeliveryError(ZipServiceError):
    """A safe delivery manifest or destination is invalid."""


@dataclass(frozen=True)
class EntryInfo:
    """One immutable central-directory member record."""

    archive_sha256: str
    index: int
    name: str
    raw_name: bytes
    flags: int
    method: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    local_header_offset: int
    data_offset: int
    data_end: int
    extent_start: int
    extent_end: int
    has_data_descriptor: bool
    utf8_name: bool

    @property
    def entry_id(self) -> str:
        return f"{self.archive_sha256}:{self.index}"

    @property
    def method_name(self) -> str:
        return _METHOD_NAMES[self.method]

    @property
    def source_range(self) -> Tuple[int, int]:
        """Half-open file range of the compressed member body."""

        return (self.data_offset, self.data_end)

    def as_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "index": self.index,
            "name": self.name,
            "method": self.method_name,
            "flags": self.flags,
            "crc32": self.crc32,
            "compressed_size": self.compressed_size,
            "uncompressed_size": self.uncompressed_size,
            "local_header_offset": self.local_header_offset,
            "data_range": [self.data_offset, self.data_end],
            "extent_range": [self.extent_start, self.extent_end],
            "has_data_descriptor": self.has_data_descriptor,
            "utf8_name": self.utf8_name,
        }


def _decode_name(raw: bytes, utf8_flag: bool) -> Tuple[str, bool]:
    if utf8_flag:
        try:
            return raw.decode("utf-8"), True
        except UnicodeDecodeError as exc:
            raise ArchiveRejected(f"UTF-8 member name is not valid UTF-8: {exc}") from exc
    # No EFS flag.  The specification says CP437, but many producers write
    # UTF-8 without setting the flag; accept valid UTF-8, otherwise CP437.
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("cp437"), False


def _stat_identity(st: os.stat_result) -> Tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _read_exact(f, n: int, what: str) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise ArchiveRejected(f"truncated {what}")
    return data


def _read_bytes_at(f, offset: int, length: int) -> bytes:
    f.seek(offset)
    data = f.read(length)
    if len(data) != length:
        raise ArchiveRejected("short read")
    return data


def _parse_eocd(f, file_size: int) -> Tuple[int, int, int, int]:
    if file_size < 22:
        raise ArchiveRejected("file is too small to contain an EOCD record")
    tail_len = min(file_size, 22 + 0xFFFF)
    f.seek(file_size - tail_len)
    tail = f.read(tail_len)
    pos = tail.rfind(b"PK\x05\x06")
    while pos >= 0:
        absolute = file_size - tail_len + pos
        if absolute + 22 <= file_size:
            fields = struct.unpack("<IHHHHIIH", tail[pos : pos + 22])
            (
                sig,
                disk_no,
                cd_disk,
                entries_disk,
                entries_total,
                cd_size,
                cd_offset,
                comment_len,
            ) = fields
            if absolute + 22 + comment_len == file_size:
                if disk_no != 0 or cd_disk != 0:
                    raise ArchiveRejected("multi-volume archives are not supported")
                if entries_disk != entries_total:
                    raise ArchiveRejected("EOCD entry counts disagree")
                if entries_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
                    raise ArchiveRejected("ZIP64 archives are not supported")
                if cd_offset + cd_size != absolute:
                    raise ArchiveRejected(
                        "central directory does not end exactly at the EOCD record"
                    )
                return entries_total, cd_size, cd_offset, absolute
        pos = tail.rfind(b"PK\x05\x06", 0, pos)
    raise ArchiveRejected("no complete end-of-central-directory record found")


def _check_flags(flags: int) -> None:
    if flags & 0x0001:
        raise ArchiveRejected("encrypted members are not supported")
    if flags & ~_ALLOWED_FLAGS:
        raise ArchiveRejected(f"unsupported general-purpose flags 0x{flags:04x}")


def _check_method(method: int) -> None:
    if method not in (_STORED, _DEFLATED):
        raise ArchiveRejected(f"unsupported compression method {method}")


def _has_zip64_extra(extra: bytes) -> bool:
    offset = 0
    while offset + 4 <= len(extra):
        header_id, size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        if offset + size > len(extra):
            raise ArchiveRejected("malformed extra field")
        if header_id == 0x0001:
            return True
        offset += size
    if offset != len(extra):
        raise ArchiveRejected("malformed extra field tail")
    return False


def _parse_central_directory(
    f, entries_total: int, cd_offset: int, cd_size: int
) -> List[dict]:
    cd_end = cd_offset + cd_size
    f.seek(cd_offset)
    cursor = cd_offset

    # Optional archive extra data record is legal before the member records.
    if cursor + 8 <= cd_end:
        head = f.read(4)
        cursor += 4
        if struct.unpack("<I", head)[0] == _ARCHIVE_EXTRA_SIG:
            extra_len = struct.unpack("<I", _read_exact(f, 4, "archive extra length"))[0]
            cursor += 4
            if cursor + extra_len > cd_end:
                raise ArchiveRejected("archive extra data record exceeds central directory")
            f.seek(extra_len, os.SEEK_CUR)
            cursor += extra_len
        else:
            f.seek(cd_offset)
            cursor = cd_offset

    records: List[dict] = []
    for index in range(entries_total):
        if cursor + 46 > cd_end:
            raise ArchiveRejected("central directory entry truncated")
        raw = _read_exact(f, 46, "central directory header")
        (
            sig,
            _version_made,
            _version_needed,
            flags,
            method,
            _mtime,
            _mdate,
            crc32,
            csize,
            usize,
            name_len,
            extra_len,
            comment_len,
            disk_start,
            _int_attr,
            _ext_attr,
            local_offset,
        ) = struct.unpack("<IHHHHHHIIIHHHHHII", raw)
        if sig != _CENTRAL_SIG:
            raise ArchiveRejected(f"bad central-directory signature at entry {index}")
        cursor += 46
        if disk_start != 0:
            raise ArchiveRejected("multi-volume member is not supported")
        if (
            csize == 0xFFFFFFFF
            or usize == 0xFFFFFFFF
            or local_offset == 0xFFFFFFFF
            or disk_start == 0xFFFF
        ):
            raise ArchiveRejected("ZIP64 member fields are not supported")
        _check_flags(flags)
        _check_method(method)
        if name_len == 0:
            raise ArchiveRejected(f"entry {index} has an empty name")
        total_var = name_len + extra_len + comment_len
        if cursor + total_var > cd_end:
            raise ArchiveRejected("central directory variable data truncated")
        raw_name = _read_exact(f, name_len, "central directory name")
        extra = _read_exact(f, extra_len, "central directory extra field")
        _read_exact(f, comment_len, "central directory comment")
        cursor += total_var
        if b"\x00" in raw_name:
            raise ArchiveRejected("member name contains NUL")
        if _has_zip64_extra(extra):
            raise ArchiveRejected("ZIP64 extra field is not supported")
        if usize > MAX_ENTRY_BYTES:
            raise ArchiveRejected(
                f"entry {index} declares {usize} uncompressed bytes, over the 8 MiB limit"
            )
        name, utf8_name = _decode_name(raw_name, bool(flags & _UTF8_FLAG))
        records.append(
            {
                "index": index,
                "name": name,
                "raw_name": raw_name,
                "flags": flags,
                "method": method,
                "crc32": crc32,
                "compressed_size": csize,
                "uncompressed_size": usize,
                "local_header_offset": local_offset,
                "utf8_name": utf8_name,
            }
        )

    # Optional central-directory digital signature record.
    if cursor < cd_end:
        if cursor + 6 <= cd_end:
            sig = struct.unpack("<I", f.read(4))[0]
            cursor += 4
            if sig == _CD_DIGITAL_SIG:
                sig_len = struct.unpack("<H", _read_exact(f, 2, "digital signature length"))[0]
                cursor += 2
                if cursor + sig_len != cd_end:
                    raise ArchiveRejected("central-directory digital signature is malformed")
                f.seek(sig_len, os.SEEK_CUR)
                cursor += sig_len
            else:
                raise ArchiveRejected("unexpected bytes at end of central directory")
        else:
            raise ArchiveRejected("unexpected bytes at end of central directory")
    if cursor != cd_end:
        raise ArchiveRejected("central directory size does not match its records")

    return records


def _find_descriptor(f, pos: int, limit: int, central: dict) -> Tuple[int, bool]:
    """Return (descriptor_end, used_signature) or reject.

    Both the signed (16-byte) and unsigned (12-byte) descriptor layouts are
    accepted.  The values must equal the central-directory declarations.
    """

    available = limit - pos
    if available < 12:
        raise ArchiveRejected("truncated data descriptor")
    f.seek(pos)
    head = _read_exact(f, min(20, available), "data descriptor")
    candidates = []
    # The signature is optional, and the size fields can be 8 bytes each when
    # ZIP64 descriptors are used.  A ZIP64 layout can never match the
    # non-ZIP64 central record, so only the 4-byte layouts are accepted.
    if available >= 16 and struct.unpack_from("<I", head, 0)[0] == _DD_SIG:
        crc, csize, usize = struct.unpack_from("<III", head, 4)
        if (
            crc == central["crc32"]
            and csize == central["compressed_size"]
            and usize == central["uncompressed_size"]
        ):
            candidates.append((pos + 16, True))
    crc, csize, usize = struct.unpack_from("<III", head, 0)
    if (
        crc == central["crc32"]
        and csize == central["compressed_size"]
        and usize == central["uncompressed_size"]
    ):
        candidates.append((pos + 12, False))
    if not candidates:
        raise ArchiveRejected(
            "data descriptor does not agree with central-directory length/CRC"
        )
    return candidates[0]


def _check_local_and_extent(f, file_size: int, cd_offset: int, record: dict) -> dict:
    local_offset = record["local_header_offset"]
    if local_offset < 0 or local_offset + 30 > cd_offset:
        raise ArchiveRejected("local header lies outside the pre-central-directory region")
    f.seek(local_offset)
    raw = _read_exact(f, 30, "local header")
    (
        sig,
        _version,
        local_flags,
        local_method,
        _mtime,
        _mdate,
        local_crc,
        local_csize,
        local_usize,
        local_name_len,
        local_extra_len,
    ) = struct.unpack("<IHHHHHIIIHH", raw)
    if sig != _LOCAL_SIG:
        raise ArchiveRejected(f"bad local-header signature for entry {record['index']}")
    if local_flags != record["flags"]:
        raise ArchiveRejected(f"entry {record['index']} local/central flags differ")
    if local_method != record["method"]:
        raise ArchiveRejected(f"entry {record['index']} local/central methods differ")
    if local_name_len != len(record["raw_name"]):
        raise ArchiveRejected(f"entry {record['index']} local/central name lengths differ")
    local_name = _read_exact(f, local_name_len, "local header name")
    if local_name != record["raw_name"]:
        raise ArchiveRejected(f"entry {record['index']} local/central names differ")
    local_extra = _read_exact(f, local_extra_len, "local header extra field")
    if _has_zip64_extra(local_extra):
        raise ArchiveRejected("ZIP64 local extra field is not supported")
    data_offset = local_offset + 30 + local_name_len + local_extra_len
    if data_offset > cd_offset:
        raise ArchiveRejected(f"entry {record['index']} local header overlaps central directory")

    descriptor = bool(record["flags"] & _DESCRIPTOR_FLAG)
    if descriptor:
        # Writers commonly put zeroes in the local header when a descriptor
        # follows; if values are present they still have to be truthful.
        if local_crc not in (0, record["crc32"]):
            raise ArchiveRejected(f"entry {record['index']} local CRC contradicts descriptor")
        if local_csize not in (0, record["compressed_size"]):
            raise ArchiveRejected(f"entry {record['index']} local compressed size contradicts descriptor")
        if local_usize not in (0, record["uncompressed_size"]):
            raise ArchiveRejected(f"entry {record['index']} local size contradicts descriptor")
    else:
        if local_method == _STORED and record["compressed_size"] != record["uncompressed_size"]:
            raise ArchiveRejected(f"entry {record['index']} stored sizes differ")
        if (local_crc, local_csize, local_usize) != (
            record["crc32"],
            record["compressed_size"],
            record["uncompressed_size"],
        ):
            raise ArchiveRejected(f"entry {record['index']} local/central lengths or CRC differ")

    data_end = data_offset + record["compressed_size"]
    if data_end > cd_offset:
        raise ArchiveRejected(f"entry {record['index']} body overlaps central directory")
    if data_end > file_size:
        raise ArchiveRejected(f"entry {record['index']} body lies beyond end of file")
    extent_end = data_end
    if descriptor:
        extent_end, _used_sig = _find_descriptor(f, data_end, cd_offset, record)
    if extent_end > cd_offset:
        raise ArchiveRejected(f"entry {record['index']} extent overlaps central directory")
    result = dict(record)
    result.update(
        {
            "data_offset": data_offset,
            "data_end": data_end,
            "extent_start": local_offset,
            "extent_end": extent_end,
            "has_data_descriptor": descriptor,
        }
    )
    return result


def _check_extents(records: Sequence[dict], cd_offset: int) -> None:
    ordered = sorted(records, key=lambda r: r["extent_start"])
    previous_end = 0
    for record in ordered:
        start = record["extent_start"]
        end = record["extent_end"]
        if start < previous_end:
            raise ArchiveRejected(
                f"entry {record['index']} range overlaps another member's range"
            )
        if end > cd_offset:
            raise ArchiveRejected(f"entry {record['index']} range covers the central directory")
        if start < 0 or end < start:
            raise ArchiveRejected(f"entry {record['index']} has an invalid range")
        previous_end = end


def _iter_payload(f, info: EntryInfo) -> Iterator[bytes]:
    """Yield a member's uncompressed body while verifying it.

    Output length is measured as bytes are produced; exceeding the declared
    uncompressed length (or the 8 MiB entry cap) is a hard error even if the
    compressed stream is otherwise well-formed.  CRC and final length are
    checked after the last chunk.  Consumers must exhaust the iterator for
    those final checks to run.
    """

    f.seek(info.data_offset)
    remaining_compressed = info.compressed_size
    crc = 0
    produced = 0

    def checked(out: bytes) -> bytes:
        nonlocal crc, produced
        if not out:
            return b""
        produced += len(out)
        if produced > info.uncompressed_size:
            raise EntryInflateLengthError(
                info.entry_id,
                f"output exceeded declared {info.uncompressed_size} bytes at {produced}",
            )
        if produced > MAX_ENTRY_BYTES:
            raise EntryLimitError(info.entry_id, "member exceeds the 8 MiB output limit")
        crc = zlib.crc32(out, crc)
        return out

    if info.method == _STORED:
        while remaining_compressed > 0:
            chunk = f.read(min(65536, remaining_compressed))
            if not chunk:
                raise EntryDataError(info.entry_id, "stored body is truncated")
            remaining_compressed -= len(chunk)
            out = checked(chunk)
            if out:
                yield out
    else:
        if info.compressed_size == 0:
            if info.uncompressed_size != 0:
                raise EntryDataError(
                    info.entry_id, "empty deflate stream declares non-empty output"
                )
        else:
            decompressor = zlib.decompressobj(-15)
            while remaining_compressed > 0:
                chunk = f.read(min(65536, remaining_compressed))
                if not chunk:
                    raise EntryDataError(info.entry_id, "deflate body is truncated")
                remaining_compressed -= len(chunk)
                data = chunk
                while data:
                    room = min(
                        65536, info.uncompressed_size - produced + 1
                    )
                    if room < 1:
                        room = 1
                    try:
                        out = decompressor.decompress(data, room)
                    except zlib.error as exc:
                        raise EntryDataError(
                            info.entry_id, f"invalid deflate stream: {exc}"
                        ) from exc
                    data = decompressor.unconsumed_tail
                    verified = checked(out)
                    if verified:
                        yield verified
                    if data and not out:
                        raise EntryDataError(
                            info.entry_id, "deflate stream made no progress"
                        )
                if decompressor.eof:
                    if decompressor.unused_data or remaining_compressed:
                        raise EntryDataError(
                            info.entry_id,
                            "compressed stream ended before its declared compressed size",
                        )
                    break
            if not decompressor.eof:
                raise EntryDataError(info.entry_id, "deflate stream ended prematurely")
            try:
                tail = decompressor.flush()
            except zlib.error as exc:
                raise EntryDataError(info.entry_id, f"invalid deflate tail: {exc}") from exc
            if tail:
                verified = checked(tail)
                if verified:
                    yield verified

    if produced != info.uncompressed_size:
        raise EntryDataError(
            info.entry_id,
            f"uncompressed length {produced} differs from declared {info.uncompressed_size}",
        )
    actual_crc = crc & 0xFFFFFFFF
    if actual_crc != info.crc32:
        raise EntryChecksumError(
            info.entry_id,
            f"CRC32 0x{actual_crc:08x} differs from declared 0x{info.crc32:08x}",
        )


def _extract_payload(f, info: EntryInfo, collect: bool) -> Tuple[bytes, int]:
    if collect:
        return b"".join(_iter_payload(f, info)), info.uncompressed_size
    for _chunk in _iter_payload(f, info):
        pass
    return b"", info.uncompressed_size


class ArchiveReader:
    """A validated archive view plus identity-addressed read operations."""

    def __init__(
        self,
        path: str,
        archive_sha256: str,
        file_size: int,
        stat_identity: Tuple[int, int, int, int],
        entries: Tuple[EntryInfo, ...],
        contents_verified: bool,
        cd_offset: int,
        cd_size: int,
        cd_fingerprint: str,
        crc_failures: Optional[Dict[str, EntryChecksumError]] = None,
    ):
        self.path = path
        self.archive_sha256 = archive_sha256
        self.file_size = file_size
        self._stat_identity = stat_identity
        self.entries = entries
        self.contents_verified = contents_verified
        self._cd_offset = cd_offset
        self._cd_size = cd_size
        self._cd_fingerprint = cd_fingerprint
        self.crc_failures: Dict[str, EntryChecksumError] = dict(crc_failures or {})
        self._by_id: Dict[str, EntryInfo] = {e.entry_id: e for e in entries}

    @classmethod
    def open(cls, path: os.PathLike | str, *, verify_contents: bool = True) -> "ArchiveReader":
        """Open and structurally validate an archive.

        ``verify_contents=False`` still validates *every* structural property
        (central/local agreement, explicit lengths, descriptors, extents,
        declared total size).  Actual decompression is deferred so a checksum
        failure can be attributed to one member/table by :meth:`preflight` or
        by the per-read verification in :meth:`read_entry`.

        ``verify_contents=True`` additionally decompresses every member up
        front.  A CRC mismatch is recorded on the reader as a non-fatal member
        failure (callers that need a fully clean archive check
        :attr:`crc_failures`); truncation, over-production and limit breaches
        reject the archive because its declared structure is a lie.
        """

        path = os.fspath(path)
        try:
            f = open(path, "rb")
        except OSError as exc:
            raise ArchiveRejected(f"cannot open archive: {exc}") from exc
        with f:
            st_before = os.fstat(f.fileno())
            if not stat.S_ISREG(st_before.st_mode):
                raise ArchiveRejected("archive path is not a regular file")
            file_size = st_before.st_size
            digest = hashlib.sha256()
            f.seek(0)
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            archive_sha = digest.hexdigest()

            entries_total, cd_size, cd_offset, eocd_offset = _parse_eocd(f, file_size)
            if cd_offset < 0 or cd_offset + cd_size > file_size:
                raise ArchiveRejected("central directory lies outside the file")

            records = _parse_central_directory(f, entries_total, cd_offset, cd_size)
            declared_total = sum(r["uncompressed_size"] for r in records)
            if declared_total > MAX_TOTAL_BYTES:
                raise ArchiveRejected(
                    f"archive declares {declared_total} uncompressed bytes, "
                    f"over the 100 MiB limit"
                )
            checked: List[dict] = []
            for record in records:
                if record["method"] == _STORED and (
                    record["compressed_size"] != record["uncompressed_size"]
                ):
                    raise ArchiveRejected(
                        f"entry {record['index']} stored lengths differ"
                    )
                checked.append(_check_local_and_extent(f, file_size, cd_offset, record))
            _check_extents(checked, cd_offset)

            entries: List[EntryInfo] = []
            for record in checked:
                entries.append(
                    EntryInfo(
                        archive_sha256=archive_sha,
                        index=record["index"],
                        name=record["name"],
                        raw_name=record["raw_name"],
                        flags=record["flags"],
                        method=record["method"],
                        crc32=record["crc32"],
                        compressed_size=record["compressed_size"],
                        uncompressed_size=record["uncompressed_size"],
                        local_header_offset=record["local_header_offset"],
                        data_offset=record["data_offset"],
                        data_end=record["data_end"],
                        extent_start=record["extent_start"],
                        extent_end=record["extent_end"],
                        has_data_descriptor=record["has_data_descriptor"],
                        utf8_name=record["utf8_name"],
                    )
                )

            crc_failures: Dict[str, EntryChecksumError] = {}
            if verify_contents:
                total = 0
                for info in entries:
                    try:
                        _body, produced = _extract_payload(f, info, collect=False)
                    except EntryChecksumError as exc:
                        crc_failures[info.entry_id] = exc
                        produced = info.uncompressed_size
                    except EntryLimitError as exc:
                        raise ArchiveRejected(str(exc)) from exc
                    except EntryReadError as exc:
                        raise ArchiveRejected(str(exc)) from exc
                    total += produced
                    if total > MAX_TOTAL_BYTES:
                        raise ArchiveRejected(
                            "archive exceeds the 100 MiB actual output limit"
                        )

            st_after = os.fstat(f.fileno())
            if _stat_identity(st_after) != _stat_identity(st_before):
                raise ArchiveChanged("archive changed while it was being inspected")
            cd_fingerprint = hashlib.sha256(
                _read_bytes_at(f, cd_offset, cd_size)
            ).hexdigest()

        return cls(
            path=os.path.abspath(path),
            archive_sha256=archive_sha,
            file_size=file_size,
            stat_identity=_stat_identity(st_before),
            entries=tuple(entries),
            contents_verified=verify_contents,
            cd_offset=cd_offset,
            cd_size=cd_size,
            cd_fingerprint=cd_fingerprint,
            crc_failures=crc_failures,
        )

    # ------------------------------------------------------------------
    # Listing / identity lookup
    # ------------------------------------------------------------------
    def list_entries(self) -> Tuple[EntryInfo, ...]:
        """All members in central-directory order, including duplicate names."""

        return self.entries

    def get(self, entry_id: str) -> EntryInfo:
        try:
            return self._by_id[entry_id]
        except KeyError:
            raise EntryNotFound(f"unknown entry id {entry_id!r}") from None

    def source_range(self, entry_id: str) -> Tuple[int, int]:
        info = self.get(entry_id)
        return info.source_range

    def _reopen_same_file(self):
        try:
            f = open(self.path, "rb")
        except OSError as exc:
            raise ArchiveChanged(f"cannot reopen archive: {exc}") from exc
        st = os.fstat(f.fileno())
        if _stat_identity(st) != self._stat_identity:
            f.close()
            raise ArchiveChanged("archive file changed since it was opened")
        # Same inode/size/mtime can still coincide after an in-place rewrite;
        # hash the central directory that identities were derived from.
        try:
            current = hashlib.sha256(
                _read_bytes_at(f, self._cd_offset, self._cd_size)
            ).hexdigest()
        except (OSError, ArchiveRejected) as exc:
            f.close()
            raise ArchiveChanged(f"cannot re-validate archive: {exc}") from exc
        if current != self._cd_fingerprint:
            f.close()
            raise ArchiveChanged("archive directory region changed since it was opened")
        return f

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def read_entry(self, entry_id: str) -> bytes:
        """Return the exact verified body for ``entry_id``.

        The archive is reopened and its identity rechecked; the local header,
        explicit lengths and CRC are verified again.  No member name is used
        to locate the body.
        """

        info = self.get(entry_id)
        with self._reopen_same_file() as f:
            body, _length = _extract_payload(f, info, collect=True)
        return body

    def iter_entry_chunks(self, entry_id: str, chunk_size: int = 65536) -> Iterator[bytes]:
        """Stream a verified body chunk by chunk.

        Decompression output is measured as it is produced and the CRC is
        checked after the final chunk, so a consumer must exhaust the iterator
        for verification to complete.
        """

        if chunk_size <= 0:
            raise DeliveryError("chunk_size must be positive")
        info = self.get(entry_id)
        with self._reopen_same_file() as f:
            buffer = bytearray()
            for chunk in _iter_payload(f, info):
                buffer.extend(chunk)
                while len(buffer) >= chunk_size:
                    yield bytes(buffer[:chunk_size])
                    del buffer[:chunk_size]
            if buffer:
                yield bytes(buffer)

    def preflight(self) -> Dict[str, EntryChecksumError]:
        """Verify every member body once, before any table is written.

        Returns CRC failures keyed by entry id (data corruption that should be
        reported against the member's table).  Structural contradictions,
        decompressed-length overruns, truncation or limit violations reject the
        *whole archive* by raising :class:`ArchiveRejected`; they are never
        silently downgraded to a per-table error.
        """

        failures: Dict[str, EntryChecksumError] = {}
        if self.contents_verified:
            return dict(self.crc_failures)
        with self._reopen_same_file() as f:
            total = 0
            for info in self.entries:
                try:
                    _body, produced = _extract_payload(f, info, collect=False)
                except EntryChecksumError as exc:
                    failures[info.entry_id] = exc
                    produced = info.uncompressed_size
                except EntryReadError as exc:
                    raise ArchiveRejected(
                        f"archive rejected while verifying {info.entry_id}: {exc.message}"
                    ) from exc
                total += produced
                if total > MAX_TOTAL_BYTES:
                    raise ArchiveRejected(
                        "archive actual output exceeds the 100 MiB total limit"
                    )
        return failures

    def read_source_range(
        self, entry_id: str, offset: int = 0, length: Optional[int] = None
    ) -> bytes:
        """Read raw compressed bytes from a member's source range.

        This does not decompress, but the requested span is bounded by the
        structural body range established at open time.
        """

        info = self.get(entry_id)
        if offset < 0:
            raise DeliveryError("source-range offset must be non-negative")
        start, end = info.source_range
        if offset > end - start:
            raise DeliveryError("source-range offset is past the member body")
        if length is None:
            length = end - start - offset
        if length < 0 or offset + length > end - start:
            raise DeliveryError("source-range length is outside the member body")
        if length > MAX_SOURCE_RANGE_BYTES:
            raise DeliveryError("source-range read exceeds the service size cap")
        with self._reopen_same_file() as f:
            f.seek(start + offset)
            data = f.read(length)
            if len(data) != length:
                raise ArchiveChanged("short read from archive source range")
        return data
