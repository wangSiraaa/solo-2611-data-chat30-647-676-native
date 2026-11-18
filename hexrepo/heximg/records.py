"""Intel HEX 文本记录解析（纯标准库，只处理合成文件，不接触硬件）。

支持的记录类型
--------------
00 Data
01 End Of File
02 Extended Segment Address   （基址 = segment << 4）
04 Extended Linear Address    （基址 = upper << 16）

其余类型（03 Start Segment Address、05 Start Linear Address 等）一律抛出
:class:`UnsupportedRecordType`，绝不静默跳过。

地址语义
~~~~~~~~
* 每条扩展地址记录按其自身定义“替换”当前基址，切换时不累加。
* 基址仅在当前文件内有效；解析器每次调用都从零状态开始，
  因此文件间作用域天然不会泄漏。
* data 的绝对地址 = 当前基址 + 记录内 16 位偏移；超出 ``0..2^32-1`` 拒绝。

每个错误都带 1 起始行号与该行在物理文件中的原字节范围（半开区间）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import (
    AddressOutOfRange,
    HexParseError,
    UnsupportedRecordType,
)
from .image import Source

REC_DATA = 0x00
REC_EOF = 0x01
REC_EXT_SEGMENT = 0x02
REC_EXT_LINEAR = 0x04
SUPPORTED_TYPES = frozenset({REC_DATA, REC_EOF, REC_EXT_SEGMENT, REC_EXT_LINEAR})

_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")


@dataclass(frozen=True)
class RawRecord:
    """通过全部行级校验的一条原始记录。"""

    line_no: int
    byte_range: tuple[int, int]
    rectype: int
    addr16: int
    data: bytes
    raw: bytes


@dataclass(frozen=True)
class DataWrite:
    """一条 data 记录解析后得到的绝对地址写入。"""

    address: int
    data: bytes
    source: Source
    line_no: int
    byte_range: tuple[int, int]


@dataclass
class ParsedHex:
    """单个 HEX 文件的解析产物。

    Attributes
    ----------
    source:
        文件标识（路径或调用方给定的名字）。
    writes:
        按文件顺序排列的绝对地址写入，已携带 :class:`Source`。
    eof_line:
        EOF 记录所在行号；文件未提供 EOF 时为 ``None``。
    raw_records:
        全部受支持记录的原始形态，便于审计。
    """

    source: str
    writes: list[DataWrite] = field(default_factory=list)
    raw_records: list[RawRecord] = field(default_factory=list)
    eof_line: int | None = None

    @property
    def total_bytes(self) -> int:
        return sum(len(w.data) for w in self.writes)


def _parse_record_line(raw: bytes, source: str, line_no: int,
                       byte_range: tuple[int, int]) -> RawRecord:
    """解析并校验单行（``raw`` 不含行尾换行）。"""
    if not raw.startswith(b":"):
        raise HexParseError(
            "记录必须以冒号 ':' 起始", source, line_no, byte_range
        )
    body = raw[1:]
    if len(body) < 10:
        raise HexParseError(
            f"记录过短：冒号后至少需要 10 个十六进制字符，实际 {len(body)} 个",
            source, line_no, byte_range,
        )
    if len(body) % 2 != 0:
        raise HexParseError(
            "十六进制字符数必须为偶数", source, line_no, byte_range
        )
    bad = sorted({chr(c) for c in body if c not in _HEX_DIGITS})
    if bad:
        raise HexParseError(
            f"存在非十六进制字符：{''.join(bad)!r}", source, line_no, byte_range
        )

    payload = bytes.fromhex(body.decode("ascii"))
    byte_count = payload[0]
    addr16 = (payload[1] << 8) | payload[2]
    rectype = payload[3]
    data = payload[4:-1]
    checksum = payload[-1]

    if byte_count != len(data):
        raise HexParseError(
            f"字节数字段声明 {byte_count}，实际数据 {len(data)} 字节",
            source, line_no, byte_range,
        )
    if (sum(payload) & 0xFF) != 0:
        raise HexParseError(
            f"8 位二补校验和错误：记录字节和模 256 = {sum(payload) & 0xFF:#04x}，"
            f"应为 0x00（声明校验字节 {checksum:#04x}）",
            source, line_no, byte_range,
        )
    if rectype not in SUPPORTED_TYPES:
        raise UnsupportedRecordType(
            f"不支持的记录类型 {rectype:#04x}；仅支持 00/01/02/04",
            source, line_no, byte_range,
        )

    if rectype in (REC_EXT_SEGMENT, REC_EXT_LINEAR) and len(data) != 2:
        raise HexParseError(
            f"记录类型 {rectype:#04x} 必须携带 2 字节数据，实际 {len(data)} 字节",
            source, line_no, byte_range,
        )
    if rectype == REC_EOF and data:
        raise HexParseError(
            "EOF 记录必须携带 0 字节数据", source, line_no, byte_range
        )

    return RawRecord(line_no, byte_range, rectype, addr16, data, raw)


def parse_hex(blob: bytes | str, *, source: str) -> ParsedHex:
    """解析 HEX 文本（字节串或字符串）。

    Parameters
    ----------
    blob:
        完整文件内容；也可直接传入文件文本。
    source:
        本文件的来源标识，会写入每条 :class:`Source`。
    """
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError("blob 必须是 bytes 或 str")

    result = ParsedHex(source=source)
    base = 0  # 当前扩展地址基址；每次切换直接替换，绝不累加
    eof_seen = False
    offset = 0
    line_no = 0

    # 手工切行以便给出与物理文件一致的字节偏移；兼容 \n 与 \r\n。
    for raw_line in blob.split(b"\n"):
        line_no += 1
        line_start = offset
        if raw_line.endswith(b"\r"):
            content = raw_line[:-1]
        else:
            content = raw_line
        offset += len(raw_line) + 1  # +1 对应被 split 掉的 \n

        stripped = content.strip()
        line_range = (line_start, line_start + len(content))
        if not stripped:
            continue  # 空行（含纯空白行）允许出现在任意位置，EOF 之后也不例外
        if eof_seen:
            raise HexParseError(
                "EOF 记录之后除空行外不允许再出现任何记录",
                source, line_no, line_range,
            )

        rec = _parse_record_line(stripped, source, line_no, line_range)
        result.raw_records.append(rec)

        if rec.rectype == REC_EOF:
            eof_seen = True
            result.eof_line = line_no
            continue

        if rec.rectype == REC_EXT_SEGMENT:
            segment = (rec.data[0] << 8) | rec.data[1]
            base = segment << 4  # 替换，而非累加
            continue

        if rec.rectype == REC_EXT_LINEAR:
            upper = (rec.data[0] << 8) | rec.data[1]
            base = upper << 16  # 替换，而非累加
            continue

        # data 记录：基址 + 16 位偏移形成绝对地址
        address = base + rec.addr16
        end = address + len(rec.data)
        if end > 0x100000000 or address > 0xFFFFFFFF:
            raise AddressOutOfRange(
                f"{source}:{line_no} data 绝对地址 {address:#010x}.."
                f"{end - 1:#010x} 超出 32 位地址空间 0..0xFFFFFFFF"
            )
        src = Source(file=source, line_no=line_no, byte_range=line_range)
        result.writes.append(
            DataWrite(address=address, data=rec.data, source=src,
                      line_no=line_no, byte_range=line_range)
        )

    return result


def parse_hex_file(path: str) -> ParsedHex:
    """从磁盘读取并解析一个 HEX 文件。"""
    with open(path, "rb") as fh:
        blob = fh.read()
    return parse_hex(blob, source=path)
