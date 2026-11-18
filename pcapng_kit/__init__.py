"""pcapng_kit —— 有限 PCAPNG 结构目录与包级流量事实仓。

模块分工：

* :mod:`pcapng_kit.catalog`  只解析 PCAPNG 块结构、字节顺序、接口时间
  分辨率/偏移，产出包的精确字节范围与有理数真实时间，不做任何业务协议
  解析。
* :mod:`pcapng_kit.traffic`  消费目录的公开包读取 API，只对以太网
  IPv4 无分片 UDP 下协议结论，其余一律 unsupported，且绝不直接扫描
  文件。

两个模块之间单向依赖：traffic -> catalog，catalog 不反向导入 traffic。
"""

from __future__ import annotations

from .catalog import (
    Catalog,
    Corruption,
    CustomBlockRecord,
    InterfaceInfo,
    PacketRecord,
    SectionInfo,
    UnknownBlockRecord,
)
from .errors import (
    BodyBufferLimitError,
    PcapngContractError,
    PcapngCorruptionError,
)
from .traffic import (
    FactStatus,
    FlowKey,
    PacketFact,
    TrafficStore,
    analyze_ethernet_ipv4_udp,
)

__all__ = [
    "Catalog",
    "Corruption",
    "CustomBlockRecord",
    "InterfaceInfo",
    "PacketRecord",
    "SectionInfo",
    "UnknownBlockRecord",
    "PcapngContractError",
    "PcapngCorruptionError",
    "BodyBufferLimitError",
    "FactStatus",
    "FlowKey",
    "PacketFact",
    "TrafficStore",
    "analyze_ethernet_ipv4_udp",
]
