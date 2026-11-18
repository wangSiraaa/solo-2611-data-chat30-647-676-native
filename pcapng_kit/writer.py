"""PCAPNG 写出器与合成流量生成器（测试/演示专用）。

写出器逐块写入并始终带前后两份块总长度，块体 4 字节对齐；它同时提供
``write_raw`` 逃生口，供测试构造框架级损坏（尾长度不一致、未对齐等）。
时间戳由调用方给定原始 64 位整数，保证与目录侧的有理数换算可独立对账。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import blocks
from .blocks import (
    BT_CB_COPYABLE,
    BT_EPB,
    BT_IDB,
    BT_SHB,
    BYTE_ORDER_MAGIC,
    LINKTYPE_ETHERNET,
    OPT_COMMENT,
    OPT_ENDOFOPT,
    OPT_IF_DESCRIPTION,
    OPT_IF_NAME,
    OPT_IF_TSOFFSET,
    OPT_IF_TSRESOL,
)


def _pad_to_4(data: bytes) -> bytes:
    remainder = len(data) % 4
    return data + b"\x00" * ((4 - remainder) % 4)


def _option(code: int, value: bytes, endian: str) -> bytes:
    prefix = struct.pack(("<" if endian == "little" else ">") + "HH", code, len(value))
    return prefix + _pad_to_4(value)


@dataclass(slots=True)
class InterfaceSpec:
    """生成器对一个 IDB 的完整声明。"""

    link_type: int = LINKTYPE_ETHERNET
    snaplen: int = 0
    ts_base: int = 10                 # 10 或 2
    ts_exponent: int = 6
    ts_offset: int = 0                # 有符号整秒
    name: str = "eth0"
    duplicate_name: bool = False      # 在合同内重复 if_name（可重复选项）
    description: str | None = None


class PcapNGWriter:
    """按节管理字节序的 PCAPNG 写出器。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: BinaryIO = open(self.path, "wb")
        self.endian: str | None = None
        self.section_id = -1
        self.interface_count = 0

    # ---- 基础 ------------------------------------------------------------
    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "PcapNGWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def tell(self) -> int:
        return self._file.tell()

    def _pack(self, fmt: str, *values) -> bytes:
        return struct.pack(("<" if self.endian == "little" else ">") + fmt, *values)

    def _write_block(
        self,
        block_type: int,
        body: bytes,
        *,
        trailing_length: int | None = None,
    ) -> tuple[int, int]:
        """写一个完整块，返回 (块起点, 块结束)。

        ``trailing_length`` 仅用于损坏构造：正常时与块总长度相同。
        """

        assert self.endian is not None
        total = blocks.BLOCK_HEADER_SIZE + len(body) + blocks.BLOCK_TRAILER_SIZE
        start = self._file.tell()
        self._file.write(self._pack("II", block_type, total))
        self._file.write(body)
        self._file.write(self._pack("I", total if trailing_length is None else trailing_length))
        return start, start + total

    def write_raw(self, data: bytes) -> int:
        """逃生口：原样写字节，用于构造无法通过正常 API 表达的损坏。"""

        self._file.write(data)
        return self._file.tell()

    # ---- SHB / 节 --------------------------------------------------------
    def start_section(self, endian: str) -> tuple[int, int]:
        if endian not in ("little", "big"):
            raise ValueError("endian must be 'little' or 'big'")
        self.endian = endian
        self.section_id += 1
        self.interface_count = 0
        body = (
            self._pack("I", BYTE_ORDER_MAGIC)
            + self._pack("HH", 1, 0)
            + self._pack("q", -1)               # section length 不提供
        )
        body += _option(OPT_COMMENT, f"section {self.section_id}".encode(), endian)
        body += self._pack("HH", OPT_ENDOFOPT, 0)
        return self._write_block(BT_SHB, body)

    # ---- IDB -------------------------------------------------------------
    def write_interface(self, spec: InterfaceSpec) -> tuple[int, int]:
        body = self._pack("HHI", spec.link_type, 0, spec.snaplen)
        body += _option(OPT_IF_NAME, spec.name.encode(), self.endian)
        if spec.duplicate_name:
            body += _option(OPT_IF_NAME, (spec.name + "-alias").encode(), self.endian)
        if spec.description:
            body += _option(
                OPT_IF_DESCRIPTION, spec.description.encode(), self.endian
            )
        octet = spec.ts_exponent | (0x80 if spec.ts_base == 2 else 0)
        body += _option(OPT_IF_TSRESOL, bytes([octet]), self.endian)
        body += _option(
            OPT_IF_TSOFFSET, self._pack("q", spec.ts_offset), self.endian
        )
        body += self._pack("HH", OPT_ENDOFOPT, 0)
        offset_range = self._write_block(BT_IDB, body)
        self.interface_count += 1
        return offset_range

    # ---- EPB -------------------------------------------------------------
    def write_packet(
        self,
        interface_id: int,
        timestamp_raw: int,
        packet_body: bytes,
        orig_len: int | None = None,
        *,
        snaplen: int = 0,
        truncate_to: int | None = None,
    ) -> dict:
        """写 EPB。

        ``truncate_to`` 给出实际捕获长度（模拟 snaplen 截断），此时块内
        正文按截断后的长度写入并 4 字节填充，而 ``orig_len`` 保留线上
        原长。返回含块/正文精确范围与截断信息的字典，供生成器保存真值。
        """

        full_len = len(packet_body)
        if orig_len is None:
            orig_len = full_len
        if truncate_to is None:
            # 模拟捕获层：snaplen 接口上长帧自动截断，线上原长保持不变。
            cap_len = min(full_len, snaplen) if snaplen else full_len
            captured = packet_body[:cap_len]
        else:
            cap_len = min(truncate_to, full_len)
            captured = packet_body[:cap_len]
        if snaplen and cap_len > snaplen:
            raise ValueError("captured length would exceed snaplen")

        ts_high = (timestamp_raw >> 32) & 0xFFFFFFFF
        ts_low = timestamp_raw & 0xFFFFFFFF
        body = self._pack(
            "IIIII",
            interface_id,
            ts_high,
            ts_low,
            cap_len,
            orig_len,
        )
        body_offset = self.tell() + 8 + 20
        body += _pad_to_4(captured)
        body += self._pack("HH", OPT_ENDOFOPT, 0)
        block_start, block_end = self._write_block(BT_EPB, body)
        return {
            "block_range": [block_start, block_end],
            "body_range": [body_offset, body_offset + cap_len],
            "captured_len": cap_len,
            "orig_len": orig_len,
            "ts_raw": timestamp_raw,
            "ts_high": ts_high,
            "ts_low": ts_low,
            "truncated": cap_len < orig_len,
        }

    # ---- 自定义块 / 未知块 -----------------------------------------------
    def write_custom_block(self, payload: bytes, *, private: bool = False) -> tuple[int, int]:
        block_type = BT_CB_COPYABLE if not private else 0x00004000
        body = self._pack("I", 0) + _pad_to_4(payload)   # 自定义块自带 4 字节私有字段
        return self._write_block(block_type, body)

    def write_unknown_block(self, block_type: int, payload: bytes) -> tuple[int, int]:
        return self._write_block(block_type, _pad_to_4(payload))

    def write_corrupt_trailer(self, block_type: int, body: bytes, bad_trailer: int) -> tuple[int, int]:
        """写一个前长度合法、尾长度被故意写错的块（损坏隔离测试）。"""

        return self._write_block(block_type, body, trailing_length=bad_trailer)
