"""合成 PCAPNG 样例与独立包真值生成。

真值文件与抓包文件完全独立写出：每个包的预期时间由本模块用
:class:`fractions.Fraction` 直接从接口合同重算（不经过目录代码），
正文保存 SHA-256 而不是再抄一份字节，测试据此逐包核对目录与仓库。
"""

from __future__ import annotations

import hashlib
import json
import random
import struct
from fractions import Fraction
from pathlib import Path

from .blocks import (
    BT_EPB,
    LINKTYPE_ETHERNET,
    LINKTYPE_LINUX_SLL,
)
from .writer import InterfaceSpec, PcapNGWriter

# ---------------------------------------------------------------------------
# 帧构造（以太网/IPv4/UDP，另有 ARP、TCP、Linux SLL 用于 unsupported）
# ---------------------------------------------------------------------------
def _ipv4_checksum(header: bytes) -> int:
    total = 0
    for i in range(0, len(header), 2):
        total += (header[i] << 8) | header[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def build_udp_frame(
    src_ip: str,
    dst_ip: str,
    src_port: int,
    dst_port: int,
    payload: bytes,
    *,
    ihl_words: int = 5,
) -> bytes:
    """构造以太网 II + IPv4（可带选项头）+ UDP 帧。"""

    udp_len = 8 + len(payload)
    udp_header = struct.pack(">HHHH", src_port, dst_port, udp_len, 0)
    ip_total = ihl_words * 4 + udp_len
    src = socket_inet(src_ip)
    dst = socket_inet(dst_ip)
    ip_header = bytearray(
        struct.pack(
            ">BBHHHBBH",
            (4 << 4) | ihl_words,
            0,
            ip_total,
            0x1234,
            0,
            64,
            17,
            0,
        )
        + src
        + dst
    )
    if ihl_words > 5:
        ip_header += b"\x01" * ((ihl_words - 5) * 4)   # NOP 风格选项
    checksum = _ipv4_checksum(bytes(ip_header))
    ip_header[10:12] = struct.pack(">H", checksum)
    ethernet = (
        b"\x02\x00\x00\x00\x00\x02"
        + b"\x02\x00\x00\x00\x00\x01"
        + struct.pack(">H", 0x0800)
    )
    return bytes(ethernet) + bytes(ip_header) + udp_header + payload


def build_arp_frame() -> bytes:
    ethernet = (
        b"\x02\x00\x00\x00\x00\x03"
        + b"\xff\xff\xff\xff\xff\xff"
        + struct.pack(">H", 0x0806)
    )
    arp = struct.pack(">HHBBH", 1, 0x0800, 6, 4, 1)
    arp += b"\x02\x00\x00\x00\x00\x03" + socket_inet("10.0.0.3")
    arp += b"\x00" * 6 + socket_inet("10.0.0.4")
    return ethernet + arp


def build_tcp_frame(src_ip: str, dst_ip: str, sport: int, dport: int) -> bytes:
    src = socket_inet(src_ip)
    dst = socket_inet(dst_ip)
    ip_total = 40
    ip_header = bytearray(
        struct.pack(">BBHHHBBH", 0x45, 0, ip_total, 0x2222, 0, 64, 6, 0)
        + src
        + dst
    )
    ip_header[10:12] = struct.pack(">H", _ipv4_checksum(bytes(ip_header)))
    tcp = struct.pack(">HHIIHHHH", sport, dport, 1, 1, 0x10, 8192, 0, 0)
    return (
        b"\x02\x00\x00\x00\x00\x05"
        + b"\x02\x00\x00\x00\x00\x06"
        + struct.pack(">H", 0x0800)
        + bytes(ip_header)
        + tcp
    )


def build_sll_udp_frame(payload: bytes) -> bytes:
    # Linux cooked capture v1：16 字节伪链路头，再接 IPv4/UDP。
    inner = build_udp_frame("10.1.1.1", "10.1.1.2", 4444, 5555, payload)
    sll = struct.pack(">HHH", 0, 3, 0x0800) + b"\x02\x00\x00\x00\x00\x07" + b"\x00\x00"
    return sll + inner[14:]


def socket_inet(text: str) -> bytes:
    return bytes(int(part) for part in text.split("."))


# ---------------------------------------------------------------------------
# 接口合同
# ---------------------------------------------------------------------------
def interface_specs(rng: random.Random) -> list[InterfaceSpec]:
    """每节固定 20 个接口，覆盖两种时间基、负偏移、非以太网链路。"""

    specs: list[InterfaceSpec] = []
    for iface_id in range(20):
        if iface_id == 2:
            # 专门制造 snaplen 截断的接口。
            snaplen = 64
        else:
            snaplen = 0
        link = LINKTYPE_LINUX_SLL if iface_id == 19 else LINKTYPE_ETHERNET
        specs.append(
            InterfaceSpec(
                link_type=link,
                snaplen=snaplen,
                ts_base=10,
                ts_exponent=6,
                ts_offset=0,
                name=f"if{iface_id}",
                duplicate_name=(iface_id == 5),
                description=f"synthetic interface {iface_id}",
            )
        )
    return specs


def expected_timestamp(
    raw: int, ts_base: int, ts_exponent: int, ts_offset: int
) -> Fraction:
    """与目录实现独立的真值时间换算（纯 Fraction）。"""

    if ts_base == 10:
        return Fraction(raw, 10**ts_exponent) + ts_offset
    return Fraction(raw, 1 << ts_exponent) + ts_offset


# ---------------------------------------------------------------------------
# 样例生成
# ---------------------------------------------------------------------------
SECTION_PLAN = [
    # endian,    ts_base, ts_exponent, ts_offset
    ("little", 10, 6, 0),
    ("big", 10, 9, -37),          # 纳秒 + 负整秒偏移
    ("little", 2, 24, 5),         # 2^-24 秒分辨率 + 正偏移
    ("big", 2, 32, -123456),      # 2^-32 秒 + 负偏移
    ("little", 10, 3, 99),        # 毫秒
]
PACKETS_PER_SECTION = 60


def generate_sample(out_dir: str | Path, *, seed: int = 20261118) -> dict:
    """生成交付样例（5 节 × 20 接口 × 60 包 + 损坏尾长度 + 恢复节）。"""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pcap_path = out_dir / "sample.pcapng"
    truth_path = out_dir / "sample.truth.json"
    rng = random.Random(seed)

    truth: dict = {
        "seed": seed,
        "sections": [],
        "packets": [],
        "custom_blocks": [],
        "unknown_blocks": [],
        "corruptions_planned": [],
    }
    with PcapNGWriter(pcap_path) as writer:
        for section_id, (endian, base, exponent, offset) in enumerate(SECTION_PLAN):
            writer.start_section(endian)
            specs = interface_specs(rng)
            for spec in specs:
                spec.ts_base = base
                spec.ts_exponent = exponent
                spec.ts_offset = offset
                writer.write_interface(spec)
            section_truth = {
                "section_id": section_id,
                "endian": endian,
                "ts_base": base,
                "ts_exponent": exponent,
                "ts_offset": offset,
                "interface_count": len(specs),
            }
            truth["sections"].append(section_truth)

            # 每接口一条独立时间轴；raw 值保持在 2^63 以内。
            raw_cursor = [rng.randrange(1_000_000, 50_000_000) for _ in specs]
            for seq in range(PACKETS_PER_SECTION):
                iface_id = seq % len(specs)
                # 前两个包强制同刻，覆盖“同刻多包”。
                if seq < 2:
                    iface_id = 0
                    raw = raw_cursor[0]
                else:
                    raw_cursor[iface_id] += rng.randrange(1, 5000)
                    raw = raw_cursor[iface_id]
                frame, kind, flow = _make_frame(rng, section_id, iface_id, seq)
                full_hash = hashlib.sha256(frame).hexdigest()

                truncate_to = None
                if iface_id == 2 and seq % 3 == 0:
                    # snaplen 截断：部分落在 IP 头、部分落在 UDP 正文。
                    truncate_to = rng.randrange(20, 60)
                epb = writer.write_packet(
                    iface_id,
                    raw,
                    frame,
                    snaplen=specs[iface_id].snaplen,
                    truncate_to=truncate_to,
                )
                truth["packets"].append(
                    {
                        "section_id": section_id,
                        "interface_id": iface_id,
                        "kind": kind,
                        "flow": flow,
                        "ts_raw": raw,
                        "expected_timestamp": str(
                            expected_timestamp(raw, base, exponent, offset)
                        ),
                        "full_frame_sha256": full_hash,
                        "captured_sha256": hashlib.sha256(
                            frame[: epb["captured_len"]]
                        ).hexdigest(),
                        "full_frame_len": len(frame),
                        "captured_len": epb["captured_len"],
                        "orig_len": epb["orig_len"],
                        "body_range": epb["body_range"],
                        "block_range": epb["block_range"],
                        "truncated": epb["truncated"],
                    }
                )

                if seq == 7:
                    start, end = writer.write_custom_block(b"pcapng-kit custom")
                    truth["custom_blocks"].append(
                        {"section_id": section_id, "block_range": [start, end]}
                    )
                if seq == 8:
                    start, end = writer.write_unknown_block(0x00000005, b"nrb stats-ish")
                    truth["unknown_blocks"].append(
                        {"section_id": section_id, "block_range": [start, end]}
                    )

        # ---- 框架损坏：尾长度故意写错，随后再写一个可恢复的小收尾节 ------
        corrupt_start = writer.tell()
        # 总长度保持 4 字节对齐，确保只有“前后长度不一致”这一项损坏。
        bad_body = writer._pack("IIIII", 0, 1, 2, 4, 4) + b"\x00" * 4
        writer.write_corrupt_trailer(BT_EPB, bad_body, bad_trailer=0xDEADBEEF)
        truth["corruptions_planned"].append(
            {
                "offset": corrupt_start,
                "reason_contains": "trailing length",
                "recovered": True,
            }
        )
        writer.start_section("big")
        writer.write_interface(
            InterfaceSpec(ts_base=10, ts_exponent=6, ts_offset=0, name="recovered")
        )
        frame = build_udp_frame("192.0.2.1", "192.0.2.2", 6000, 7000, b"recovered")
        epb = writer.write_packet(0, 4242, frame)
        truth["sections"].append(
            {
                "section_id": 5,
                "endian": "big",
                "ts_base": 10,
                "ts_exponent": 6,
                "ts_offset": 0,
                "interface_count": 1,
                "recovered": True,
            }
        )
        truth["packets"].append(
            {
                "section_id": 5,
                "interface_id": 0,
                "kind": "udp",
                "flow": {
                    "src_ip": "192.0.2.1",
                    "dst_ip": "192.0.2.2",
                    "src_port": 6000,
                    "dst_port": 7000,
                },
                "ts_raw": 4242,
                "expected_timestamp": str(expected_timestamp(4242, 10, 6, 0)),
                "full_frame_sha256": hashlib.sha256(frame).hexdigest(),
                "captured_sha256": hashlib.sha256(frame).hexdigest(),
                "full_frame_len": len(frame),
                "captured_len": epb["captured_len"],
                "orig_len": epb["orig_len"],
                "body_range": epb["body_range"],
                "block_range": epb["block_range"],
                "truncated": epb["truncated"],
            }
        )

    with open(truth_path, "w", encoding="utf-8") as handle:
        json.dump(truth, handle, indent=2, ensure_ascii=False)
    return {"pcap": pcap_path, "truth": truth_path, "truth_obj": truth}


def _make_frame(
    rng: random.Random, section_id: int, iface_id: int, seq: int
) -> tuple[bytes, str, dict | None]:
    payload = bytes(rng.randrange(1, 256) for _ in range(rng.randrange(10, 100)))
    src_ip = f"10.{section_id}.{iface_id}.{seq % 250 + 1}"
    dst_ip = f"10.{section_id}.{iface_id}.250"
    sport = 10000 + (seq % 4000)
    dport = 53

    if iface_id == 19:
        return build_sll_udp_frame(payload), "sll_udp", None
    roll = seq % 10
    if roll == 0:
        return build_arp_frame(), "arp", None
    if roll == 1:
        return build_tcp_frame(src_ip, dst_ip, sport, dport), "tcp", None
    ihl_words = 6 if seq % 17 == 0 else 5      # 部分 IPv4 包带选项
    frame = build_udp_frame(
        src_ip, dst_ip, sport, dport, payload, ihl_words=ihl_words
    )
    flow = {
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": sport,
        "dst_port": dport,
    }
    return frame, "udp", flow


# ---------------------------------------------------------------------------
# 大规模生成（5 节 × 20 接口 × 100000 包）
# ---------------------------------------------------------------------------
def generate_scale_sample(
    out_dir: str | Path,
    *,
    sections: int = 5,
    interfaces: int = 20,
    packets: int = 100_000,
    seed: int = 20261118,
) -> dict:
    """规模样例：同样多节/多接口，包总数 ``packets``，另存精简真值。"""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pcap_path = out_dir / "scale.pcapng"
    truth_path = out_dir / "scale.truth.jsonl"
    rng = random.Random(seed + 1)
    per_section = packets // sections

    with PcapNGWriter(pcap_path) as writer, open(truth_path, "w") as truth:
        for section_id in range(sections):
            endian, base, exponent, offset = SECTION_PLAN[section_id % len(SECTION_PLAN)]
            writer.start_section(endian)
            specs = []
            for iface_id in range(interfaces):
                spec = InterfaceSpec(
                    snaplen=(64 if iface_id == 2 else 0),
                    link_type=(
                        LINKTYPE_LINUX_SLL if iface_id == interfaces - 1
                        else LINKTYPE_ETHERNET
                    ),
                    name=f"if{iface_id}",
                    ts_base=base,
                    ts_exponent=exponent,
                    ts_offset=offset,
                )
                writer.write_interface(spec)
                specs.append(spec)
            cursors = [rng.randrange(10**6, 5 * 10**7) for _ in specs]
            for seq in range(per_section):
                iface_id = rng.randrange(interfaces)
                cursors[iface_id] += rng.randrange(1, 5000)
                raw = cursors[iface_id]
                frame, kind, flow = _make_frame(rng, section_id, iface_id, seq)
                truncate_to = (
                    rng.randrange(20, 60)
                    if iface_id == 2 and rng.random() < 0.5
                    else None
                )
                epb = writer.write_packet(
                    iface_id, raw, frame,
                    snaplen=specs[iface_id].snaplen,
                    truncate_to=truncate_to,
                )
                record = {
                    "section_id": section_id,
                    "interface_id": iface_id,
                    "kind": kind,
                    "flow": flow,
                    "ts_raw": raw,
                    "expected_timestamp": str(
                        expected_timestamp(raw, base, exponent, offset)
                    ),
                    "full_frame_sha256": hashlib.sha256(frame).hexdigest(),
                    "captured_sha256": hashlib.sha256(
                        frame[: epb["captured_len"]]
                    ).hexdigest(),
                    "full_frame_len": len(frame),
                    "captured_len": epb["captured_len"],
                    "orig_len": epb["orig_len"],
                    "body_range": epb["body_range"],
                    "truncated": epb["truncated"],
                }
                truth.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"pcap": pcap_path, "truth": truth_path}
