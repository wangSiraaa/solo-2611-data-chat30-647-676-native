"""合成 Intel HEX 生成工具（测试与样例共用，纯本地、不接触硬件）。

提供逐记录构造能力，覆盖：data、EOF、extended-segment-address（02）、
extended-linear-address（04）、跨 64 KiB、同值重复、0x00 字节与空洞，
以及按要求制造坏行（坏冒号、坏校验和、字节数不符、非法十六进制、
unsupported 类型、EOF 后记录、地址越界）。
"""

from __future__ import annotations

from pathlib import Path


def checksum(payload: bytes) -> int:
    """8 位二补校验和：使记录全部字节之和模 256 为 0。"""
    return (-sum(payload)) & 0xFF


def data_record(address16: int, data: bytes) -> str:
    if not (0 <= address16 <= 0xFFFF):
        raise ValueError("16 位偏移越界")
    payload = bytes([len(data), (address16 >> 8) & 0xFF,
                     address16 & 0xFF, 0x00]) + data
    return ":" + (payload + bytes([checksum(payload)])).hex().upper()


def eof_record() -> str:
    payload = bytes([0x00, 0x00, 0x00, 0x01])
    return ":" + (payload + bytes([checksum(payload)])).hex().upper()


def extended_segment_record(segment: int) -> str:
    if not (0 <= segment <= 0xFFFF):
        raise ValueError("段号越界")
    payload = bytes([0x02, 0x00, 0x00, 0x02,
                     (segment >> 8) & 0xFF, segment & 0xFF])
    return ":" + (payload + bytes([checksum(payload)])).hex().upper()


def extended_linear_record(upper16: int) -> str:
    if not (0 <= upper16 <= 0xFFFF):
        raise ValueError("高 16 位越界")
    payload = bytes([0x02, 0x00, 0x00, 0x04,
                     (upper16 >> 8) & 0xFF, upper16 & 0xFF])
    return ":" + (payload + bytes([checksum(payload)])).hex().upper()


def start_linear_unsupported(addr32: int) -> str:
    """类型 05：题目要求明确 unsupported。"""
    payload = bytes([0x04, 0x00, 0x00, 0x05]) + addr32.to_bytes(4, "big")
    return ":" + (payload + bytes([checksum(payload)])).hex().upper()


def start_segment_unsupported(cs: int, ip: int) -> str:
    """类型 03：题目要求明确 unsupported。"""
    payload = bytes([0x04, 0x00, 0x00, 0x03]) + cs.to_bytes(2, "big") \
        + ip.to_bytes(2, "big")
    return ":" + (payload + bytes([checksum(payload)])).hex().upper()


def corrupt_checksum(line: str) -> str:
    """把末字节校验和改成错误值。"""
    body = bytes.fromhex(line[1:])
    bad = (body[-1] ^ 0xFF) & 0xFF
    return ":" + (body[:-1] + bytes([bad])).hex().upper()


def corrupt_byte_count(line: str) -> str:
    """声明字节数与实际数据不符。"""
    body = bytearray(bytes.fromhex(line[1:]))
    body[0] = (body[0] + 1) & 0xFF
    # 重新计算错误校验和，确保唯一故障点是字节数
    ck = checksum(bytes(body[:-1]))
    body[-1] = ck
    return ":" + bytes(body).hex().upper()


def write_text(path: str | Path, lines: list[str]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def make_linear_sample() -> list[str]:
    """ELA（04）切换样例：两次切换互不累加，含 0x00 字节与空洞。"""
    lines = [
        extended_linear_record(0x0001),                 # 基址 0x0001_0000
        data_record(0x0000, bytes([0x10, 0x11, 0x00, 0x13])),
        data_record(0x0010, bytes([0x20, 0x21])),       # 0x10010..，中间空洞
        extended_linear_record(0x0002),                 # 替换基址（不累加）
        data_record(0x0000, bytes([0x30, 0x31, 0x32])),
        eof_record(),
    ]
    return lines


def make_segment_sample() -> list[str]:
    """ESA（02）切换样例：基址 = segment<<4，跨 64 KiB 边界。"""
    lines = [
        extended_segment_record(0x1000),                # 基址 0x0001_0000
        data_record(0x0FF0, bytes(range(0x40, 0x48))),  # 绝对 0x10FF0
        extended_segment_record(0x2000),                # 替换为 0x0002_0000
        data_record(0x0000, bytes([0xA0, 0xA1])),
        # segment<<4 落到 0xFFFF0：写入跨 64 KiB 边界 0x100000
        extended_segment_record(0xFFFF),
        data_record(0x000C, bytes([0x5A, 0x5B, 0x5C])),  # 0xFFFFC..0xFFFFE
        data_record(0x0010, bytes([0x60])),             # 0x100000
        eof_record(),
    ]
    return lines


def make_duplicate_same_value() -> list[str]:
    """同值重复提供：可归并并保留全部来源。"""
    return [
        data_record(0x0100, bytes([0x77, 0x88, 0x99])),
        data_record(0x0101, bytes([0x88])),             # 同地址同值
        data_record(0x0102, bytes([0x99, 0xAA])),       # 前字节同值，后字节新增
        eof_record(),
    ]


def make_conflict_same_layer() -> list[str]:
    """同层同地址异值：必须拒绝整份产品。"""
    return [
        data_record(0x0200, bytes([0x01, 0x02, 0x03])),
        data_record(0x0201, bytes([0x7F])),             # 0x0201: 0x02 vs 0x7F
        eof_record(),
    ]


def make_overlay_high() -> list[str]:
    """高层 overlay：部分同值、部分改值、部分新地址。"""
    return [
        data_record(0x0100, bytes([0x77, 0xCC, 0x99])),  # 0x101 改值，其余同值
        data_record(0x0300, bytes([0xDD])),               # 新地址
        eof_record(),
    ]


def make_zero_vs_hole() -> list[str]:
    """0x00 是有效定义，空洞不是；相邻 0x00 与空洞必须可区分。"""
    return [
        data_record(0x0000, bytes([0x00, 0x00])),
        # 0x0002..0x0003 留空（空洞）
        data_record(0x0004, bytes([0x00])),
        eof_record(),
    ]


SAMPLES = {
    "linear.hex": make_linear_sample,
    "segment.hex": make_segment_sample,
    "dup_same_value.hex": make_duplicate_same_value,
    "conflict_same_layer.hex": make_conflict_same_layer,
    "overlay_high.hex": make_overlay_high,
    "zero_vs_hole.hex": make_zero_vs_hole,
}


def generate_all(out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    paths = []
    for name, factory in SAMPLES.items():
        paths.append(write_text(out / name, factory()))
    return paths
