"""PCAPNG 结构/时间目录（第一模块，不做任何业务协议解析）。

目录在构造时对文件做一次顺序扫描，建立以下轻量事实：

* 每节的字节范围、大小端与版本；
* 每接口（仅在所属节内编号）的链路类型、snaplen、时间分辨率
  （10^-n 或 2^-n 秒）与有符号整秒偏移；
* 每个 EPB 的接口归属、64 位时间戳高低 32 位、捕获长度/线上原长、
  块与正文的精确字节范围、截断标记；
* 自定义保留块、按长度整体跳过的未知块；
* 所有框架损坏/合同违规点，以及扫描是否已从下一节恢复。

时间换算全程使用 :class:`fractions.Fraction`，64 位时间戳先由高低
32 位拼成整数再除以分辨率，绝不经过浮点。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

from . import blocks
from .errors import (
    BodyBufferLimitError,
    PcapngContractError,
    PcapngCorruptionError,
)


# ---------------------------------------------------------------------------
# 目录事实数据类
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SectionInfo:
    """一节（SHB 起，下一个 SHB 或 EOF 止）的身份与字节范围。"""

    section_id: int
    block_offset: int
    block_end: int
    endian: str                  # "little" | "big"
    byte_order_magic: int
    major_version: int
    minor_version: int
    section_length: int          # SHB 自带的节长度，-1 表示未提供
    end_offset: int = 0          # 本节覆盖到的文件偏移（下一节起点或 EOF）

    @property
    def uid(self) -> tuple[int]:
        return (self.section_id,)


@dataclass(slots=True)
class InterfaceInfo:
    """IDB 声明的接口事实；接口号只在所属节内有效。"""

    section_id: int
    interface_id: int
    block_offset: int
    block_end: int
    link_type: int
    snaplen: int
    ts_base: int                 # 10 或 2
    ts_exponent: int             # 分辨率 = base ** (-exponent) 秒
    ts_offset: Fraction          # 有符号整秒偏移（IDB 里是 int64）
    names: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    endian: str = "little"

    @property
    def uid(self) -> tuple[int, int]:
        """接口真实身份：节号 + 节内接口号。"""

        return (self.section_id, self.interface_id)

    @property
    def resolution(self) -> Fraction:
        """每拍时间戳代表的秒数（有理数）。"""

        if self.ts_base == 10:
            return Fraction(1, 10**self.ts_exponent)
        return Fraction(1, 1 << self.ts_exponent)

    def to_timestamp(self, ts_high: int, ts_low: int) -> Fraction:
        """把 EPB 的高低 32 位时间戳换算为绝对秒（有理数，含整秒偏移）。"""

        ticks = (ts_high << 32) | ts_low
        if self.ts_base == 10:
            local = Fraction(ticks, 10**self.ts_exponent)
        else:
            local = Fraction(ticks, 1 << self.ts_exponent)
        return local + self.ts_offset


@dataclass(slots=True)
class PacketRecord:
    """一个 Enhanced Packet Block 的结构事实（不含协议解释）。"""

    index: int                   # 全文件包序号
    section_id: int
    interface_id: int
    block_offset: int
    block_end: int               # 独占
    body_offset: int
    body_end: int                # 独占；仅覆盖 captured_len 个真实字节
    options_offset: int
    options_end: int             # 独占
    captured_len: int
    orig_len: int
    ts_high: int
    ts_low: int
    timestamp: Fraction          # 绝对秒，有理数
    truncated: bool              # captured_len < orig_len
    link_type: int
    endian: str

    @property
    def interface_uid(self) -> tuple[int, int]:
        return (self.section_id, self.interface_id)


@dataclass(slots=True)
class CustomBlockRecord:
    """自定义保留块（0x00000BAD / 0x00004000）。"""

    section_id: int
    block_type: int
    block_offset: int
    block_end: int
    body_offset: int
    body_end: int
    endian: str


@dataclass(slots=True)
class UnknownBlockRecord:
    """不认识的块：只登记类型与范围，正文从不被猜测。"""

    section_id: int
    block_type: int
    block_offset: int
    block_end: int
    endian: str


@dataclass(slots=True)
class Corruption:
    """一次框架损坏或合同违规。"""

    offset: int
    end: int
    reason: str
    recovered: bool              # 是否已从后续 SHB 恢复扫描
    section_id: int | None
    fatal: bool                  # True=块框架损坏；False=合同违规/跳过单块


# ---------------------------------------------------------------------------
# 目录本体
# ---------------------------------------------------------------------------
class Catalog:
    """一次扫描建偏移索引，随后按需回读正文的 PCAPNG 目录。

    典型用法::

        with Catalog.open("sample.pcapng") as cat:
            for pkt in cat.packets:
                body = cat.read_body(pkt)

    目录可以独立使用：只列结构与时间，不生成任何业务统计。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        body_buffer_limit: int = blocks.MAX_BODY_BUFFER,
    ) -> None:
        self.path = Path(path)
        self.body_buffer_limit = body_buffer_limit
        self._file: BinaryIO = open(self.path, "rb")
        self.file_size = self._file.seek(0, 2)

        self.sections: list[SectionInfo] = []
        self.interfaces: list[InterfaceInfo] = []
        self.packets: list[PacketRecord] = []
        self.custom_blocks: list[CustomBlockRecord] = []
        self.unknown_blocks: list[UnknownBlockRecord] = []
        self.corruptions: list[Corruption] = []

        # 扫描期游标状态
        self._section: SectionInfo | None = None
        self._endian: str | None = None
        self._section_ifaces: list[InterfaceInfo] = []
        self._packet_index = 0
        self._scan_pos = 0

        try:
            self._scan()
        except BaseException:
            self._file.close()
            raise

    # ---- 生命周期 --------------------------------------------------------
    @classmethod
    def open(cls, path: str | Path, **kwargs) -> "Catalog":
        return cls(path, **kwargs)

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 基础读取小工具 --------------------------------------------------
    def _read_at(
        self, offset: int, length: int, *, limit: int | None = None
    ) -> bytes:
        if limit is not None and length > limit:
            raise BodyBufferLimitError(offset, length, limit)
        self._file.seek(offset)
        data = self._file.read(length)
        if len(data) != length:
            raise PcapngCorruptionError(
                offset, f"unexpected EOF: wanted {length} bytes, got {len(data)}"
            )
        return data

    def _structs(self, endian: str) -> dict[str, struct.Struct]:
        return {
            "IHq": blocks.ST_BE_IHQ if endian == "big" else blocks.ST_LE_IHQ,
            "5I": blocks.ST_BE_5I if endian == "big" else blocks.ST_LE_5I,
            "HHI": blocks.ST_BE_HHI if endian == "big" else blocks.ST_LE_HHI,
            "q": struct.Struct(">q" if endian == "big" else "<q"),
            "I": struct.Struct(">I" if endian == "big" else "<I"),
        }

    # ---- 主扫描状态机 ----------------------------------------------------
    def _scan(self) -> None:
        pos = 0
        while pos < self.file_size:
            before = pos
            if self._section is None:
                try:
                    pos = self._scan_or_resync_shb(pos)
                except PcapngCorruptionError as exc:
                    self._record_fatal(exc)
                    nxt = self._resync(pos)
                    if nxt is None or nxt <= pos:
                        # 无法前进（坏 SHB 的 magic 反复命中自身）：终止扫描。
                        return
                    self.corruptions[-1].recovered = True
                    pos = nxt
                continue
            try:
                pos = self._scan_block(pos)
            except PcapngCorruptionError as exc:
                # 框架级损坏：登记后从下一个 SHB 的 magic 重新同步。
                self._record_fatal(exc)
                nxt = self._resync(pos)
                if nxt is None or nxt <= pos:
                    return
                # 已定位到下一节 SHB，本次损坏被成功隔离。
                self.corruptions[-1].recovered = True
                pos = nxt
            if pos <= before:
                # 防御性前进保护：任何路径都不允许原地打转。
                raise PcapngCorruptionError(pos, "scanner failed to advance")
        self._finish_section()

    def _scan_or_resync_shb(self, pos: int) -> int:
        """节外只接受 SHB；否则搜索 byte-order magic 重新同步。"""

        magic_pos = self._find_shb_magic(pos)
        if magic_pos is None:
            if pos < self.file_size:
                self.corruptions.append(
                    Corruption(
                        pos, self.file_size,
                        "trailing bytes outside any section; no SHB magic found",
                        recovered=False, section_id=None, fatal=True,
                    )
                )
            return self.file_size
        if magic_pos != pos:
            self.corruptions.append(
                Corruption(
                    pos, magic_pos,
                    f"{magic_pos - pos} bytes before first/recovered SHB",
                    recovered=True, section_id=None, fatal=True,
                )
            )
        return self._read_shb(magic_pos)

    def _find_shb_magic(self, start: int) -> int | None:
        """在 [start, EOF) 内搜索 SHB 的 byte-order magic 两种字节排列。"""

        if start + blocks.SHB_MAGIC_OFFSET + 4 > self.file_size:
            return None
        self._file.seek(start)
        window = self._file.read(self.file_size - start)
        le = blocks.BYTE_ORDER_MAGIC_BYTES
        be = le[::-1]
        hits: list[int] = []
        for needle in (le, be):
            idx = window.find(needle)
            while idx != -1:
                candidate = start + idx - blocks.SHB_MAGIC_OFFSET
                if candidate >= start:
                    hits.append(candidate)
                idx = window.find(needle, idx + 1)
        return min(hits) if hits else None

    def _resync(self, bad_pos: int) -> int | None:
        """框架损坏后，跳过坏块区域，定位下一个 SHB。

        从坏块块头之后开始找 magic：损坏可能正好发生在文件首个/上一帧
        之后的 SHB 上，新 SHB 的 magic 距坏块起点只有 8 字节。
        """

        self._finish_section()
        return self._find_shb_magic(bad_pos + blocks.BLOCK_HEADER_SIZE)

    def _record_fatal(self, exc: PcapngCorruptionError) -> None:
        sid = self._section.section_id if self._section else None
        self.corruptions.append(
            Corruption(
                exc.offset, min(exc.offset + 8, self.file_size),
                exc.reason, recovered=False, section_id=sid, fatal=True,
            )
        )

    def _finish_section(self) -> None:
        if self._section is not None:
            self._section.end_offset = self._scan_pos
        self._section = None
        self._endian = None
        self._section_ifaces = []

    # ---- 单块分派 --------------------------------------------------------
    def _scan_block(self, pos: int) -> int:
        assert self._endian is not None
        st = self._structs(self._endian)
        header = self._read_at(pos, blocks.BLOCK_HEADER_SIZE)
        block_type = st["I"].unpack(header[0:4])[0]
        total_length = st["I"].unpack(header[4:8])[0]

        if block_type == blocks.BT_SHB:
            self._finish_section()
            return self._read_shb(pos)
        if total_length < 12 or total_length % blocks.ALIGNMENT != 0:
            raise PcapngCorruptionError(
                pos,
                f"block total length {total_length} illegal "
                f"(must be >=12 and 4-byte aligned)",
            )
        if pos + total_length > self.file_size:
            raise PcapngCorruptionError(
                pos,
                f"block total length {total_length} runs past EOF "
                f"(file size {self.file_size})",
            )
        trailer = st["I"].unpack(
            self._read_at(pos + total_length - blocks.BLOCK_TRAILER_SIZE, 4)
        )[0]
        if trailer != total_length:
            raise PcapngCorruptionError(
                pos,
                f"block trailing length {trailer} != leading length "
                f"{total_length}",
            )

        end = pos + total_length
        if block_type == blocks.BT_IDB:
            self._read_idb(pos, end)
        elif block_type == blocks.BT_EPB:
            self._read_epb(pos, end)
        elif block_type in blocks.CUSTOM_BLOCK_TYPES:
            body_start = pos + blocks.BLOCK_HEADER_SIZE
            self.custom_blocks.append(
                CustomBlockRecord(
                    self._section.section_id, block_type, pos, end,
                    body_start, end - blocks.BLOCK_TRAILER_SIZE, self._endian,
                )
            )
        else:
            self.unknown_blocks.append(
                UnknownBlockRecord(
                    self._section.section_id, block_type, pos, end, self._endian,
                )
            )
        self._scan_pos = end
        return end

    # ---- SHB -------------------------------------------------------------
    def _read_shb(self, pos: int) -> int:
        """读取 SHB 并开启新节；返回节内下一块的偏移。"""

        header = self._read_at(pos, blocks.BLOCK_HEADER_SIZE)
        endian = self._detect_endian(pos)
        st = self._structs(endian)
        total_length = st["I"].unpack(header[4:8])[0]
        if total_length < 20 or total_length % blocks.ALIGNMENT != 0:
            raise PcapngCorruptionError(
                pos, f"SHB total length {total_length} illegal",
            )
        if pos + total_length > self.file_size:
            raise PcapngCorruptionError(pos, "SHB runs past EOF")
        trailer = st["I"].unpack(
            self._read_at(pos + total_length - 4, 4)
        )[0]
        if trailer != total_length:
            raise PcapngCorruptionError(
                pos, f"SHB trailing length {trailer} != {total_length}",
            )

        body = self._read_at(pos + 8, total_length - 12)
        magic = st["I"].unpack(body[0:4])[0]
        if magic != blocks.BYTE_ORDER_MAGIC:
            raise PcapngCorruptionError(
                pos, f"bad byte-order magic 0x{magic:08x}",
            )
        major, minor = struct.unpack(
            (">" if endian == "big" else "<") + "HH", body[4:8]
        )
        (section_length,) = st["q"].unpack(body[8:16])
        # body 已排除前后各 4 字节的块类型/总长度与尾部总长度；
        # SHB 固定体 16 字节（magic 4 + 版本 4 + 节长 8），选项从 16 起。
        options_area = body[16:]
        try:
            # SHB 选项只做结构校验：comment 等允许重复，未知选项跳过。
            for code, value, _off in blocks.iter_options(
                options_area, endian, pos + 8 + 16
            ):
                _ = code, value
        except ValueError as exc:
            raise PcapngCorruptionError(pos, str(exc)) from exc

        section = SectionInfo(
            section_id=len(self.sections),
            block_offset=pos,
            block_end=pos + total_length,
            endian=endian,
            byte_order_magic=magic,
            major_version=major,
            minor_version=minor,
            section_length=section_length,
        )
        self.sections.append(section)
        self._section = section
        self._endian = endian
        self._section_ifaces = []
        self._scan_pos = pos + total_length
        return pos + total_length

    def _detect_endian(self, pos: int) -> str:
        """按 SHB 起点 +8 处的 byte-order magic 判大小端。

        SHB 的块类型 0x0A0D0D0A 自身字节序对称，真正的判据是 magic
        0x1A2B3C4D 的字节排列。
        """

        raw = self._read_at(pos + blocks.SHB_MAGIC_OFFSET, 4)
        if raw == blocks.BYTE_ORDER_MAGIC_BYTES:
            return "little"
        if raw == blocks.BYTE_ORDER_MAGIC_BYTES[::-1]:
            return "big"
        raise PcapngCorruptionError(
            pos, f"unrecognised byte-order magic bytes {raw.hex()}",
        )

    # ---- IDB -------------------------------------------------------------
    def _read_idb(self, pos: int, end: int) -> None:
        assert self._endian is not None
        st = self._structs(self._endian)
        if end - pos < 20:
            raise PcapngCorruptionError(pos, "IDB smaller than 20 bytes")
        link_type, _reserved, snaplen = st["HHI"].unpack(
            self._read_at(pos + 8, 8)
        )
        iface = InterfaceInfo(
            section_id=self._section.section_id,
            interface_id=len(self._section_ifaces),
            block_offset=pos,
            block_end=end,
            link_type=link_type,
            snaplen=snaplen,
            ts_base=10,
            ts_exponent=6,                 # 合同缺省：10^-6 秒
            ts_offset=Fraction(0),
            endian=self._endian,
        )
        options_area = self._read_at(pos + 16, end - 4 - (pos + 16))
        # 读取范围 [pos+16, end-4) 已排除尾部块总长度。
        seen_single: set[int] = set()
        try:
            for code, value, opt_off in blocks.iter_options(
                options_area, self._endian, pos + 16
            ):
                if code == blocks.OPT_IF_TSRESOL:
                    self._apply_tsresol(iface, value, opt_off, seen_single)
                elif code == blocks.OPT_IF_TSOFFSET:
                    self._apply_tsoffset(iface, value, opt_off, seen_single, st)
                elif code == blocks.OPT_IF_NAME:
                    iface.names.append(value.decode("utf-8"))
                elif code == blocks.OPT_IF_DESCRIPTION:
                    iface.descriptions.append(value.decode("utf-8"))
                # comment 与其余未知选项：合同允许，跳过。
        except PcapngContractError as exc:
            # 合同违规（如单值选项重复）：整块跳过，接口号不被占用，
            # 后续 IDB 的节内编号保持连续。
            self._skip_contract(pos, end, exc.reason)
            return
        except ValueError as exc:
            raise PcapngCorruptionError(pos, str(exc)) from exc
        except UnicodeDecodeError as exc:
            self._skip_contract(pos, end, f"non-UTF8 interface name: {exc}")
            return

        self._section_ifaces.append(iface)
        self.interfaces.append(iface)

    def _apply_tsresol(
        self,
        iface: InterfaceInfo,
        value: bytes,
        opt_off: int,
        seen_single: set[int],
    ) -> None:
        if blocks.OPT_IF_TSRESOL in seen_single:
            raise PcapngContractError(
                opt_off, "duplicate if_tsresol option (single-valued contract)",
            )
        if len(value) != 1:
            raise PcapngContractError(
                opt_off, f"if_tsresol must be 1 byte, got {len(value)}",
            )
        octet = value[0]
        if octet & 0x80:
            exponent = octet & 0x7F
            if exponent > 63:
                raise PcapngContractError(
                    opt_off, f"binary tsresol exponent {exponent} > 63",
                )
            iface.ts_base = 2
        else:
            exponent = octet
            if exponent > 18:
                raise PcapngContractError(
                    opt_off, f"decimal tsresol exponent {exponent} > 18",
                )
            iface.ts_base = 10
        iface.ts_exponent = exponent
        seen_single.add(blocks.OPT_IF_TSRESOL)

    def _apply_tsoffset(
        self,
        iface: InterfaceInfo,
        value: bytes,
        opt_off: int,
        seen_single: set[int],
        st: dict[str, struct.Struct],
    ) -> None:
        if blocks.OPT_IF_TSOFFSET in seen_single:
            raise PcapngContractError(
                opt_off, "duplicate if_tsoffset option (single-valued contract)",
            )
        if len(value) != 8:
            raise PcapngContractError(
                opt_off, f"if_tsoffset must be 8 bytes, got {len(value)}",
            )
        (raw_offset,) = st["q"].unpack(value)
        iface.ts_offset = Fraction(raw_offset)   # 有符号整秒
        seen_single.add(blocks.OPT_IF_TSOFFSET)

    # ---- EPB -------------------------------------------------------------
    def _read_epb(self, pos: int, end: int) -> None:
        assert self._endian is not None and self._section is not None
        st = self._structs(self._endian)
        if end - pos < 32:
            raise PcapngCorruptionError(pos, "EPB smaller than 32 bytes")
        # 一次读取块体（不含 8 字节块头与 4 字节尾部总长度）。EPB 受
        # snaplen 约束（标准上限 65535），不会触发 1 MiB 正文缓冲上限。
        block_body = self._read_at(pos + 8, end - pos - 12)
        iface_id, ts_high, ts_low, caplen, origlen = st["5I"].unpack(
            block_body[0:20]
        )
        body_start = pos + 28
        padded = blocks.align4(caplen)
        options_start = body_start + padded
        body_end = body_start + caplen
        if caplen > origlen:
            self._skip_contract(
                pos, end,
                f"EPB captured_len {caplen} > orig_len {origlen}; "
                "truncated packet must not masquerade as complete",
            )
            return
        if iface_id >= len(self._section_ifaces):
            self._skip_contract(
                pos, end,
                f"EPB references interface {iface_id} but section declares "
                f"{len(self._section_ifaces)} interface(s)",
            )
            return
        iface = self._section_ifaces[iface_id]
        if iface.snaplen != 0 and caplen > iface.snaplen:
            self._skip_contract(
                pos, end,
                f"EPB captured_len {caplen} exceeds snaplen {iface.snaplen}",
            )
            return
        if options_start > end - 4:
            self._skip_contract(
                pos, end,
                f"EPB layout overrun: padded body ends at {options_start}, "
                f"block body ends at {end - 4}",
            )
            return
        # 选项区在已读取的块体切片内，避免第二次系统调用。
        opt_slice_start = options_start - (pos + 8)
        options_area = block_body[opt_slice_start:]
        try:
            for code, value, opt_off in blocks.iter_options(
                options_area, self._endian, options_start
            ):
                _ = code, value, opt_off  # EPB 选项结构合法即接受，内容不影响事实
        except ValueError as exc:
            self._skip_contract(pos, end, f"EPB options malformed: {exc}")
            return

        record = PacketRecord(
            index=self._packet_index,
            section_id=self._section.section_id,
            interface_id=iface_id,
            block_offset=pos,
            block_end=end,
            body_offset=body_start,
            body_end=body_end,
            options_offset=options_start,
            options_end=end - 4,
            captured_len=caplen,
            orig_len=origlen,
            ts_high=ts_high,
            ts_low=ts_low,
            timestamp=iface.to_timestamp(ts_high, ts_low),
            truncated=caplen < origlen,
            link_type=iface.link_type,
            endian=self._endian,
        )
        self._packet_index += 1
        self.packets.append(record)

    def _skip_contract(self, pos: int, end: int, reason: str) -> None:
        """块框架合法但违反有限合同：登记并跳过该块，扫描可继续。"""

        sid = self._section.section_id if self._section else None
        self.corruptions.append(
            Corruption(
                offset=pos, end=end, reason=reason,
                recovered=True, section_id=sid, fatal=False,
            )
        )

    # ---- 正文回读 --------------------------------------------------------
    def read_body(self, record: PacketRecord) -> bytes:
        """按目录登记的精确范围回读 EPB 捕获正文（原始字节，不碰填充）。

        单次读取超过 ``body_buffer_limit``（默认 1 MiB）时抛
        :class:`BodyBufferLimitError`，且不会读取任何捕获范围之外的字节。
        """

        length = record.captured_len
        if length > self.body_buffer_limit:
            raise BodyBufferLimitError(
                record.body_offset, length, self.body_buffer_limit,
            )
        return self._read_at(record.body_offset, length)

    def read_range(self, start: int, length: int) -> bytes:
        """按绝对字节范围回读（自定义块等使用），同样受缓冲上限约束。"""

        if length > self.body_buffer_limit:
            raise BodyBufferLimitError(start, length, self.body_buffer_limit)
        return self._read_at(start, length)

    # ---- 查询辅助 --------------------------------------------------------
    def interface_of(self, record: PacketRecord) -> InterfaceInfo:
        return self.interfaces_by_uid[record.interface_uid]

    def section_of(self, record: PacketRecord) -> SectionInfo:
        return self.sections[record.section_id]

    @property
    def interfaces_by_uid(self) -> dict[tuple[int, int], InterfaceInfo]:
        return {iface.uid: iface for iface in self.interfaces}

    def listing(self) -> dict:
        """纯数据目录快照，可直接 JSON 序列化（Fraction 转为分数字符串）。"""

        return {
            "file": str(self.path),
            "file_size": self.file_size,
            "sections": [
                {
                    "section_id": s.section_id,
                    "byte_range": [s.block_offset, s.block_end],
                    "section_span": [s.block_offset, s.end_offset],
                    "endian": s.endian,
                    "version": f"{s.major_version}.{s.minor_version}",
                    "declared_section_length": s.section_length,
                }
                for s in self.sections
            ],
            "interfaces": [
                {
                    "uid": list(i.uid),
                    "byte_range": [i.block_offset, i.block_end],
                    "link_type": i.link_type,
                    "snaplen": i.snaplen,
                    "resolution_seconds": str(i.resolution),
                    "ts_base": i.ts_base,
                    "ts_exponent": i.ts_exponent,
                    "ts_offset_seconds": str(i.ts_offset),
                    "names": i.names,
                }
                for i in self.interfaces
            ],
            "packets": [
                {
                    "index": p.index,
                    "section_id": p.section_id,
                    "interface_id": p.interface_id,
                    "block_range": [p.block_offset, p.block_end],
                    "body_range": [p.body_offset, p.body_end],
                    "options_range": [p.options_offset, p.options_end],
                    "captured_len": p.captured_len,
                    "orig_len": p.orig_len,
                    "ts_high": p.ts_high,
                    "ts_low": p.ts_low,
                    "timestamp_seconds": str(p.timestamp),
                    "truncated": p.truncated,
                    "link_type": p.link_type,
                }
                for p in self.packets
            ],
            "custom_blocks": [
                {
                    "section_id": c.section_id,
                    "block_type": f"0x{c.block_type:08x}",
                    "block_range": [c.block_offset, c.block_end],
                    "body_range": [c.body_offset, c.body_end],
                }
                for c in self.custom_blocks
            ],
            "unknown_blocks": [
                {
                    "section_id": u.section_id,
                    "block_type": f"0x{u.block_type:08x}",
                    "block_range": [u.block_offset, u.block_end],
                }
                for u in self.unknown_blocks
            ],
            "corruptions": [
                {
                    "byte_range": [c.offset, c.end],
                    "reason": c.reason,
                    "recovered": c.recovered,
                    "fatal": c.fatal,
                    "section_id": c.section_id,
                }
                for c in self.corruptions
            ],
        }
