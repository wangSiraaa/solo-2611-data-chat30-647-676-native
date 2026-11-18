"""协议事实分析与流量仓（第二模块）。

本模块只消费 :class:`~pcapng_kit.catalog.Catalog` 暴露的包记录与
``read_body`` API，不接收文件路径、不做任何块扫描；目录是字节来源的
唯一入口。

支持范围刻意收窄：

* 仅以太网（LINKTYPE_ETHERNET）中的 IPv4、无分片 UDP 给出完整事实；
* 其他链路类型、ARP/TCP、分片 IPv4、非 IPv4 以太类型一律标记
  :data:`FactStatus.UNSUPPORTED`，属于正常数据而非坏文件；
* 被 snaplen 截断、字段无法全部确定的包标记
  :data:`FactStatus.INCOMPLETE`，同时给出还能确定的字段，并显式声明
  完整性；
* 任何分析都以捕获正文实际长度为界，绝不读取范围之外的字节。
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from typing import Iterator

from .blocks import LINKTYPE_ETHERNET
from .catalog import Catalog, InterfaceInfo, PacketRecord
from .errors import BodyBufferLimitError

ETH_HEADER_LEN = 14
ETHERTYPE_IPV4 = 0x0800
IPPROTO_UDP = 17
UDP_HEADER_LEN = 8
IP_MIN_HEADER_LEN = 20


class FactStatus(str, Enum):
    """单个包的协议事实状态。"""

    OK = "ok"                    # 以太网 IPv4 无分片 UDP，且正文完整
    INCOMPLETE = "incomplete"    # 本可成为 UDP 包，但截断导致字段/正文不全
    UNSUPPORTED = "unsupported"  # 合法但不在支持合同内（非坏文件）
    READ_ERROR = "read_error"    # 正文超过缓冲上限等，无法取得字节


@dataclass(frozen=True, slots=True)
class FlowKey:
    """方向流键：接口真实身份 + 源/目的 IP + 源/目的端口。

    反向包不会与正向合并；不同节里同号接口也不会合并，因为节号是键的
    一部分。
    """

    section_id: int
    interface_id: int
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int

    def as_tuple(self) -> tuple:
        return (
            self.section_id,
            self.interface_id,
            self.src_ip,
            self.dst_ip,
            self.src_port,
            self.dst_port,
        )


@dataclass(slots=True)
class PacketFact:
    """一个包的协议事实；无法确定的字段一律为 ``None`` 而非猜测值。"""

    index: int
    section_id: int
    interface_id: int
    timestamp: Fraction
    captured_len: int
    wire_len: int
    truncated: bool
    status: FactStatus
    reason: str
    flow: FlowKey | None
    ethertype: int | None
    ip_version: int | None
    ip_header_len: int | None
    ip_total_len: int | None
    ip_protocol: int | None
    src_ip: str | None
    dst_ip: str | None
    fragmented: bool | None
    udp_src_port: int | None
    udp_dst_port: int | None
    udp_len: int | None
    udp_payload_len: int | None       # 线上 UDP 正文长度（字段声明值）
    udp_payload_complete: bool        # 捕获正文是否包含完整 UDP 正文
    record: PacketRecord = field(repr=False)


@dataclass(slots=True)
class WindowStats:
    """半开时间窗 [start, end) 内的流量统计（全部为精确整数/有理数）。"""

    start: Fraction
    end: Fraction
    packet_count: int
    wire_bytes: int                   # 线上原长 orig_len 之和
    captured_bytes: int               # 实际捕获字节 captured_len 之和
    complete_udp_payload_bytes: int   # 仅累加正文完整的 UDP 包
    complete_udp_packet_count: int
    source_packet_indices: list[int]

    def as_dict(self) -> dict:
        return {
            "window": [str(self.start), str(self.end)],
            "packet_count": self.packet_count,
            "wire_bytes": self.wire_bytes,
            "captured_bytes": self.captured_bytes,
            "complete_udp_payload_bytes": self.complete_udp_payload_bytes,
            "complete_udp_packet_count": self.complete_udp_packet_count,
            "source_packet_indices": self.source_packet_indices,
        }


# ---------------------------------------------------------------------------
# 纯函数：对一段已捕获的以太网帧做有限分析
# ---------------------------------------------------------------------------
def analyze_ethernet_ipv4_udp(
    body: bytes,
    link_type: int,
    truncated: bool,
) -> dict:
    """只依据 ``body``（捕获正文，长度即 captured_len）分析以太网帧。

    返回可直接填入 :class:`PacketFact` 的字段字典。函数对边界完全保守：
    每读一个字段前先确认它整体落在 ``body`` 内，因此不可能读到捕获正文
    之外的字节。
    """

    result: dict = {
        "status": FactStatus.UNSUPPORTED,
        "reason": "",
        "flow": None,
        "ethertype": None,
        "ip_version": None,
        "ip_header_len": None,
        "ip_total_len": None,
        "ip_protocol": None,
        "src_ip": None,
        "dst_ip": None,
        "fragmented": None,
        "udp_src_port": None,
        "udp_dst_port": None,
        "udp_len": None,
        "udp_payload_len": None,
        "udp_payload_complete": False,
    }
    available = len(body)

    if link_type != LINKTYPE_ETHERNET:
        result["reason"] = f"link type {link_type} is not Ethernet"
        return result
    if available < ETH_HEADER_LEN:
        result["status"] = FactStatus.INCOMPLETE
        result["reason"] = (
            f"captured body {available} bytes shorter than Ethernet header "
            f"{ETH_HEADER_LEN}"
        )
        return result

    ethertype = int.from_bytes(body[12:14], "big")
    result["ethertype"] = ethertype
    if ethertype != ETHERTYPE_IPV4:
        result["reason"] = f"EtherType 0x{ethertype:04x} is not IPv4"
        return result

    # ---- IPv4 固定/可变头 ------------------------------------------------
    if available < ETH_HEADER_LEN + 1:
        result["status"] = FactStatus.INCOMPLETE
        result["reason"] = "IPv4 version/IHL byte not captured"
        return result
    version_ihl = body[ETH_HEADER_LEN]
    version = version_ihl >> 4
    ihl = version_ihl & 0x0F
    result["ip_version"] = version
    if version != 4:
        result["reason"] = f"IP version {version} inside IPv4 EtherType"
        return result
    if ihl < 5:
        result["status"] = FactStatus.INCOMPLETE
        result["reason"] = f"IPv4 IHL {ihl} implies header shorter than 20 bytes"
        return result
    ip_header_len = ihl * 4
    result["ip_header_len"] = ip_header_len

    # 总长度、协议、地址、分片字段都位于 20 字节固定头内。
    if available < ETH_HEADER_LEN + IP_MIN_HEADER_LEN:
        result["status"] = FactStatus.INCOMPLETE
        result["reason"] = "IPv4 fixed header not fully captured"
        return result
    ip_total_len = int.from_bytes(body[ETH_HEADER_LEN + 2: ETH_HEADER_LEN + 4], "big")
    result["ip_total_len"] = ip_total_len
    protocol = body[ETH_HEADER_LEN + 9]
    result["ip_protocol"] = protocol
    result["src_ip"] = socket.inet_ntoa(
        body[ETH_HEADER_LEN + 12: ETH_HEADER_LEN + 16]
    )
    result["dst_ip"] = socket.inet_ntoa(
        body[ETH_HEADER_LEN + 16: ETH_HEADER_LEN + 20]
    )
    flags_frag = int.from_bytes(body[ETH_HEADER_LEN + 6: ETH_HEADER_LEN + 8], "big")
    more_fragments = bool(flags_frag & 0x2000)
    frag_offset = flags_frag & 0x1FFF
    fragmented = more_fragments or frag_offset != 0
    result["fragmented"] = fragmented
    if fragmented:
        result["reason"] = "fragmented IPv4 datagram (reassembly out of scope)"
        return result
    if protocol != IPPROTO_UDP:
        result["reason"] = f"IP protocol {protocol} is not UDP"
        return result
    if available < ETH_HEADER_LEN + ip_header_len:
        result["status"] = FactStatus.INCOMPLETE
        result["reason"] = (
            f"IPv4 options/header ({ip_header_len} bytes) not fully captured"
        )
        return result

    # ---- UDP 头 ----------------------------------------------------------
    udp_start = ETH_HEADER_LEN + ip_header_len
    if available < udp_start + UDP_HEADER_LEN:
        result["status"] = FactStatus.INCOMPLETE
        result["reason"] = "UDP header not fully captured"
        return result
    udp_src = int.from_bytes(body[udp_start: udp_start + 2], "big")
    udp_dst = int.from_bytes(body[udp_start + 2: udp_start + 4], "big")
    udp_len = int.from_bytes(body[udp_start + 4: udp_start + 6], "big")
    result["udp_src_port"] = udp_src
    result["udp_dst_port"] = udp_dst
    result["udp_len"] = udp_len
    if udp_len < UDP_HEADER_LEN:
        result["status"] = FactStatus.INCOMPLETE
        result["reason"] = f"UDP length field {udp_len} smaller than UDP header"
        return result
    payload_len = udp_len - UDP_HEADER_LEN
    result["udp_payload_len"] = payload_len

    payload_complete = available >= udp_start + udp_len
    result["udp_payload_complete"] = payload_complete
    result["flow"] = {
        "src_ip": result["src_ip"],
        "dst_ip": result["dst_ip"],
        "src_port": udp_src,
        "dst_port": udp_dst,
    }
    if payload_complete and not truncated:
        result["status"] = FactStatus.OK
        result["reason"] = "complete Ethernet/IPv4/UDP datagram"
    elif payload_complete:
        # 截断发生在 UDP 正文之后（如尾部帧字节/填充缺失），UDP 事实仍完整，
        # 但显式保留 truncated 事实，状态降级为 INCOMPLETE。
        result["status"] = FactStatus.INCOMPLETE
        result["reason"] = (
            "UDP payload fully captured but frame was truncated on the wire "
            "(captured_len < orig_len)"
        )
    else:
        result["status"] = FactStatus.INCOMPLETE
        result["reason"] = (
            f"UDP payload truncated: need {udp_start + udp_len} bytes, "
            f"captured {available}"
        )
    return result


# ---------------------------------------------------------------------------
# 流量仓
# ---------------------------------------------------------------------------
class TrafficStore:
    """消费目录公开 API 构建的包级事实仓（不直接接触文件）。"""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.facts: list[PacketFact] = []
        self._flows: dict[FlowKey, list[int]] = {}

    @classmethod
    def build(cls, catalog: Catalog) -> "TrafficStore":
        """遍历目录包记录，逐包经 ``catalog.read_body`` 取字节并下事实。"""

        store = cls(catalog)
        for record in catalog.packets:
            store.facts.append(store._build_fact(record))
        return store

    def _build_fact(self, record: PacketRecord) -> PacketFact:
        try:
            body = self.catalog.read_body(record)
        except BodyBufferLimitError as exc:
            return PacketFact(
                index=record.index,
                section_id=record.section_id,
                interface_id=record.interface_id,
                timestamp=record.timestamp,
                captured_len=record.captured_len,
                wire_len=record.orig_len,
                truncated=record.truncated,
                status=FactStatus.READ_ERROR,
                reason=str(exc),
                flow=None,
                ethertype=None,
                ip_version=None,
                ip_header_len=None,
                ip_total_len=None,
                ip_protocol=None,
                src_ip=None,
                dst_ip=None,
                fragmented=None,
                udp_src_port=None,
                udp_dst_port=None,
                udp_len=None,
                udp_payload_len=None,
                udp_payload_complete=False,
                record=record,
            )

        parsed = analyze_ethernet_ipv4_udp(
            body, record.link_type, record.truncated
        )
        flow = None
        if parsed["flow"] is not None:
            f = parsed["flow"]
            flow = FlowKey(
                section_id=record.section_id,
                interface_id=record.interface_id,
                src_ip=f["src_ip"],
                dst_ip=f["dst_ip"],
                src_port=f["src_port"],
                dst_port=f["dst_port"],
            )
        fact = PacketFact(
            index=record.index,
            section_id=record.section_id,
            interface_id=record.interface_id,
            timestamp=record.timestamp,
            captured_len=record.captured_len,
            wire_len=record.orig_len,
            truncated=record.truncated,
            status=parsed["status"],
            reason=parsed["reason"],
            flow=flow,
            ethertype=parsed["ethertype"],
            ip_version=parsed["ip_version"],
            ip_header_len=parsed["ip_header_len"],
            ip_total_len=parsed["ip_total_len"],
            ip_protocol=parsed["ip_protocol"],
            src_ip=parsed["src_ip"],
            dst_ip=parsed["dst_ip"],
            fragmented=parsed["fragmented"],
            udp_src_port=parsed["udp_src_port"],
            udp_dst_port=parsed["udp_dst_port"],
            udp_len=parsed["udp_len"],
            udp_payload_len=parsed["udp_payload_len"],
            udp_payload_complete=parsed["udp_payload_complete"],
            record=record,
        )
        if flow is not None:
            self._flows.setdefault(flow, []).append(record.index)
        return fact

    # ---- 查询 ------------------------------------------------------------
    def interface_identity(self, fact: PacketFact) -> tuple[int, int, str]:
        """返回 (节号, 节内接口号, 接口名列表)，强调接口真实身份。"""

        iface: InterfaceInfo = self.catalog.interfaces_by_uid[
            (fact.section_id, fact.interface_id)
        ]
        return fact.section_id, fact.interface_id, ",".join(iface.names)

    def flow_keys(self) -> list[FlowKey]:
        return list(self._flows.keys())

    def facts_for_flow(self, key: FlowKey) -> list[PacketFact]:
        return [self.facts[i] for i in self._flows[key]]

    def __iter__(self) -> Iterator[PacketFact]:
        return iter(self.facts)

    def __len__(self) -> int:
        return len(self.facts)

    def window(
        self,
        start: Fraction,
        end: Fraction,
        *,
        flow: FlowKey | None = None,
    ) -> WindowStats:
        """半开时间窗 [start, end) 统计。

        不传 ``flow`` 时统计窗内全部包（unsupported/incomplete 也计入包数
        与字节，但其 UDP 正文不计入完整正文长度）；传入 :class:`FlowKey`
        时只统计该方向流，反向流天然被排除。
        """

        if not start <= end:
            raise ValueError("window requires start <= end")
        packet_count = 0
        wire_bytes = 0
        captured_bytes = 0
        complete_payload = 0
        complete_count = 0
        sources: list[int] = []
        for fact in self.facts:
            if not (start <= fact.timestamp < end):
                continue
            if flow is not None and fact.flow != flow:
                continue
            packet_count += 1
            wire_bytes += fact.wire_len
            captured_bytes += fact.captured_len
            sources.append(fact.index)
            if (
                fact.status is FactStatus.OK
                and fact.udp_payload_len is not None
                and fact.udp_payload_complete
            ):
                complete_payload += fact.udp_payload_len
                complete_count += 1
        return WindowStats(
            start=start,
            end=end,
            packet_count=packet_count,
            wire_bytes=wire_bytes,
            captured_bytes=captured_bytes,
            complete_udp_payload_bytes=complete_payload,
            complete_udp_packet_count=complete_count,
            source_packet_indices=sources,
        )
