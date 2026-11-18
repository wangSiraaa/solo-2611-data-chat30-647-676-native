"""Low-level ZIP construction helpers used by fixtures and tests.

These helpers exist so tests can build adversarial archives that ``zipfile``
would refuse to produce: duplicate names, descriptor layouts, deliberately
wrong declared lengths, trailing CRC corruption, and overlapping entry extents.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import List, Optional


def raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    out = compressor.compress(data) + compressor.flush()
    return out


@dataclass
class Member:
    name: str
    body: Optional[bytes] = None
    stored: bytes = b""
    method: int = 8
    flags: int = 0x0800  # UTF-8 names
    # When set, these override what is written to headers/descriptors.
    crc_override: Optional[int] = None
    csize_override: Optional[int] = None
    usize_override: Optional[int] = None
    descriptor: bool = False
    local_csize_override: Optional[int] = None
    local_usize_override: Optional[int] = None
    local_crc_override: Optional[int] = None
    name_encoding: str = "utf-8"


class ZipBuilder:
    def __init__(self) -> None:
        self._parts: List[bytes] = []
        self._central: List[bytes] = []
        self._offset = 0

    def _add(self, data: bytes) -> int:
        position = self._offset
        self._parts.append(data)
        self._offset += len(data)
        return position

    def add(self, member: Member) -> "ZipBuilder":
        if member.body is None:
            raise ValueError("member body is required")
        raw_name = member.name.encode(member.name_encoding)
        if member.method == 0:
            payload = member.body
        elif member.method == 8:
            payload = raw_deflate(member.body)
        else:
            raise ValueError("fixtures support stored and deflate only")
        if member.stored:
            payload = member.stored
        crc = zlib.crc32(member.body) & 0xFFFFFFFF
        csize = len(payload)
        usize = len(member.body)
        if member.crc_override is not None:
            crc = member.crc_override
        if member.csize_override is not None:
            csize = member.csize_override
        if member.usize_override is not None:
            usize = member.usize_override

        flags = member.flags
        if member.descriptor:
            flags |= 0x0008
            local_csize = member.local_csize_override if member.local_csize_override is not None else 0
            local_usize = member.local_usize_override if member.local_usize_override is not None else 0
            local_crc = member.local_crc_override if member.local_crc_override is not None else 0
        else:
            local_csize = member.local_csize_override if member.local_csize_override is not None else csize
            local_usize = member.local_usize_override if member.local_usize_override is not None else usize
            local_crc = member.local_crc_override if member.local_crc_override is not None else crc

        local_offset = self._add(
            struct.pack(
                "<IHHHHHIIIHH",
                0x04034B50,
                20,
                flags,
                member.method,
                0,
                0,
                local_crc,
                local_csize,
                local_usize,
                len(raw_name),
                0,
            )
            + raw_name
            + payload
        )
        if member.descriptor:
            dd_crc = member.local_crc_override if member.local_crc_override is not None else crc
            dd_csize = member.local_csize_override if member.local_csize_override is not None else csize
            dd_usize = member.local_usize_override if member.local_usize_override is not None else usize
            self._add(
                struct.pack("<IIII", 0x08074B50, dd_crc, dd_csize, dd_usize)
            )
        self._central.append(
            struct.pack(
                "<IHHHHHHIIIHHHHHII",
                0x02014B50,
                0x031E,
                20,
                flags,
                member.method,
                0,
                0,
                crc,
                csize,
                usize,
                len(raw_name),
                0,
                0,
                0,
                0,
                0,
                local_offset,
            )
            + raw_name
        )
        return self

    def build(self, comment: bytes = b"") -> bytes:
        cd_offset = self._offset
        cd_bytes = b"".join(self._central)
        for entry in self._central:
            self._add(entry)
        cd_size = len(cd_bytes)
        count = len(self._central)
        eocd = struct.pack(
            "<IHHHHIIH",
            0x06054B50,
            0,
            0,
            count,
            count,
            cd_size,
            cd_offset,
            len(comment),
        ) + comment
        self._add(eocd)
        return b"".join(self._parts)
