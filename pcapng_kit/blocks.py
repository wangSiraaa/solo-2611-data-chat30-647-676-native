"""PCAPNG 有限合同里用到的块/选项常量与底层编解码小工具。

本模块只认识题目要求的四类块：

* Section Header Block (SHB)
* Interface Description Block (IDB)
* Enhanced Packet Block (EPB)
* Custom Block（自定义保留块，含可重写/不可重写两个块类型）

其余块类型一律按块总长度整体跳过，不解析正文。
"""

from __future__ import annotations

import struct
from typing import Iterator

# ---- 块类型（按文件写入端的本地字节序解释） -----------------------------
BT_SHB = 0x0A0D0D0A
BT_IDB = 0x00000001
BT_EPB = 0x00000006
BT_CB_COPYABLE = 0x00000BAD      # 可复制自定义块
BT_CB_PRIVATE = 0x00004000       # 不可复制自定义保留块
CUSTOM_BLOCK_TYPES = frozenset({BT_CB_COPYABLE, BT_CB_PRIVATE})

# ---- SHB 固定字段 --------------------------------------------------------
BYTE_ORDER_MAGIC = 0x1A2B3C4D
BYTE_ORDER_MAGIC_BYTES = struct.pack("<I", BYTE_ORDER_MAGIC)  # 4d 3c 2b 1a
SHB_MAGIC_OFFSET = 8             # SHB 内 magic 相对块起点的偏移
SHB_MIN_BODY = 12                # magic(4) + major(2) + minor(2) + section length(8)
SHB_VERSION = (1, 0)

# ---- IDB 选项 ------------------------------------------------------------
OPT_IF_NAME = 2
OPT_IF_DESCRIPTION = 3
OPT_IF_TSRESOL = 9
OPT_IF_TSOFFSET = 14

# ---- 通用选项 ------------------------------------------------------------
OPT_ENDOFOPT = 0
OPT_COMMENT = 1

# ---- 链路类型（有限集合，其余链路作为 unsupported 保留） ------------------
LINKTYPE_ETHERNET = 1
LINKTYPE_LINUX_SLL = 113

# ---- 其他常量 ------------------------------------------------------------
BLOCK_HEADER_SIZE = 8            # block type + block total length
BLOCK_TRAILER_SIZE = 4
ALIGNMENT = 4
MAX_BODY_BUFFER = 1 << 20        # 单次正文缓冲上限 1 MiB

# ---- 预编译结构（大小端各一份） ------------------------------------------
# I = 块类型/总长度, H = 版本/链路类型, q = section length / tsoffset
ST_LE_IHQ = struct.Struct("<IHq")
ST_BE_IHQ = struct.Struct(">IHq")
ST_LE_5I = struct.Struct("<IIIII")     # EPB: ifid, ts_high, ts_low, caplen, origlen
ST_BE_5I = struct.Struct(">IIIII")
ST_LE_HHI = struct.Struct("<HHI")        # IDB: linktype(u16) + reserved(u16) + snaplen(u32)
ST_BE_HHI = struct.Struct(">HHI")


def align4(length: int) -> int:
    """返回把 ``length`` 补齐到 4 字节边界后的长度。"""

    return (length + 3) & ~3


def padded_length(length: int) -> int:
    """块内变长字段（EPB 包正文 / 自定义块正文）需要补到的 4 字节长度。"""

    return align4(length)


def iter_options(
    data: bytes,
    endian: str,
    block_offset: int,
) -> Iterator[tuple[int, bytes, int]]:
    """按 PCAPNG TLV 规则遍历选项区。

    生成 ``(option_code, option_value, option_offset)``，其中
    ``option_offset`` 是该选项相对文件起点的绝对偏移。``opt_endofopt``
    后仍有非零字节属于合同违规，由调用方判断。
    """

    endian_char = "<" if endian == "little" else ">"
    header = struct.Struct(endian_char + "HH")
    pos = 0
    ended = False
    while pos < len(data):
        if pos + 4 > len(data):
            raise ValueError(
                f"option header truncated at block offset {block_offset + pos}"
            )
        code, length = header.unpack_from(data, pos)
        value_start = pos + 4
        value_end = value_start + length
        if value_end > len(data):
            raise ValueError(
                f"option value overruns option area at block offset "
                f"{block_offset + pos}"
            )
        pos = align4(value_end)
        if code == OPT_ENDOFOPT:
            ended = True
            break
        yield code, data[value_start:value_end], block_offset + value_start - 4
    if ended:
        # endofopt 之后只允许全零填充。
        tail = data[pos:]
        if tail.strip(b"\x00"):
            raise ValueError(
                f"non-zero bytes after opt_endofopt at block offset "
                f"{block_offset + pos}"
            )
    elif data:
        raise ValueError(
            f"option area missing opt_endofopt at block offset {block_offset}"
        )
