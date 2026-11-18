"""真实流量事实仓：消费不可变规则产品，逐包记录命中身份与分类结果。

关键约束：

* 每条流量事实绑定其消费的规则产品摘要（``ruleset_digest``）与身份
  （命中 ``rule_id`` 或 ``DEFAULT``），因此规则换序/改范围/删除后重新装载会
  得到新的事实集，而旧事实仓文件保持不变（内容寻址、原子写）。
* 聚合按窗口与 category 统计**包数、字节数**，并保留原包来源（packet_id 列表
  的摘要与计数）。DENY/丢弃类别同样计入，总字节守恒可被逐包核验。
* 输入包做严格校验：非负字节数、协议 TCP/UDP、端口闭区间、整数时刻。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .rules import (
    PORT_MAX,
    PORT_MIN,
    RuleSet,
    parse_ipv4,
)

FACT_VERSION = 1


class TrafficValidationError(ValueError):
    """流量记录格式或取值非法。"""


@dataclass(frozen=True)
class Packet:
    packet_id: str
    ts: int
    src: int
    dst: int
    protocol: str
    sport: int
    dport: int
    bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, str) or not self.packet_id:
            raise TrafficValidationError("packet_id 必须是非空字符串")
        if not isinstance(self.ts, int) or isinstance(self.ts, bool) or self.ts < 0:
            raise TrafficValidationError(f"{self.packet_id}: ts 必须是非负整数")
        for name, value in (("src", self.src), ("dst", self.dst)):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFF:
                raise TrafficValidationError(f"{self.packet_id}: {name} 必须是 uint32")
        # 本期包均有 TCP/UDP 端口
        if self.protocol not in ("TCP", "UDP"):
            raise TrafficValidationError(f"{self.packet_id}: 协议必须是 TCP/UDP，收到 {self.protocol!r}")
        for name, value in (("sport", self.sport), ("dport", self.dport)):
            if not isinstance(value, int) or isinstance(value, bool) or not PORT_MIN <= value <= PORT_MAX:
                raise TrafficValidationError(f"{self.packet_id}: {name} 越界 {value}")
        if not isinstance(self.bytes, int) or isinstance(self.bytes, bool) or self.bytes < 0:
            raise TrafficValidationError(f"{self.packet_id}: 字节数必须是非负整数")

    def to_dict(self) -> dict:
        return {
            "packet_id": self.packet_id,
            "ts": self.ts,
            "src": _fmt(self.src),
            "dst": _fmt(self.dst),
            "protocol": self.protocol,
            "sport": self.sport,
            "dport": self.dport,
            "bytes": self.bytes,
        }

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "Packet":
        for key in ("packet_id", "ts", "src", "dst", "protocol", "sport", "dport", "bytes"):
            if key not in obj:
                raise TrafficValidationError(f"packet 缺少字段 {key}")
        return cls(
            packet_id=obj["packet_id"],
            ts=obj["ts"],
            src=parse_ipv4(obj["src"]),
            dst=parse_ipv4(obj["dst"]),
            protocol=obj["protocol"],
            sport=obj["sport"],
            dport=obj["dport"],
            bytes=obj["bytes"],
        )


def _fmt(value: int) -> str:
    from .rules import format_ipv4

    return format_ipv4(value)


@dataclass(frozen=True)
class Fact:
    packet: Packet
    rule_id: str
    action: str
    category: str
    rule_index: int

    @property
    def is_default(self) -> bool:
        return self.rule_id == "DEFAULT"

    def to_dict(self) -> dict:
        return {
            "packet_id": self.packet.packet_id,
            "ts": self.packet.ts,
            "src": _fmt(self.packet.src),
            "dst": _fmt(self.packet.dst),
            "protocol": self.packet.protocol,
            "sport": self.packet.sport,
            "dport": self.packet.dport,
            "bytes": self.packet.bytes,
            "rule_id": self.rule_id,
            "action": self.action,
            "category": self.category,
            "rule_index": self.rule_index,
        }


@dataclass(frozen=True)
class WindowSummary:
    window_start: int
    window_seconds: int
    category: str
    action: str
    packets: int
    bytes: int
    rule_ids: dict[str, int]
    source_packet_ids: tuple[str, ...]

    @property
    def source_digest(self) -> str:
        payload = "\n".join(self.source_packet_ids).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self, *, include_sources: bool = True) -> dict:
        out = {
            "window_start": self.window_start,
            "window_seconds": self.window_seconds,
            "category": self.category,
            "action": self.action,
            "packets": self.packets,
            "bytes": self.bytes,
            "rule_ids": dict(sorted(self.rule_ids.items())),
            "source_packet_count": len(self.source_packet_ids),
            "source_digest": self.source_digest,
        }
        if include_sources:
            out["source_packet_ids"] = list(self.source_packet_ids)
        return out


@dataclass(frozen=True)
class Analysis:
    """一次装载的产物：事实 + 聚合 + 规则产品身份。"""

    ruleset_name: str
    ruleset_digest: str
    window_seconds: int
    facts: tuple[Fact, ...]
    summaries: tuple[WindowSummary, ...]
    packets_total: int
    bytes_total: int
    identity_digest: str

    def to_dict(self) -> dict:
        return {
            "fact_version": FACT_VERSION,
            "ruleset_name": self.ruleset_name,
            "ruleset_digest": self.ruleset_digest,
            "window_seconds": self.window_seconds,
            "identity_digest": self.identity_digest,
            "packets_total": self.packets_total,
            "bytes_total": self.bytes_total,
            "facts": [fact.to_dict() for fact in self.facts],
            "summaries": [s.to_dict() for s in self.summaries],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class TrafficWarehouse:
    """消费指定不可变规则产品并生成事实产品。"""

    def __init__(self, ruleset: RuleSet, *, window_seconds: int = 60) -> None:
        if not isinstance(window_seconds, int) or isinstance(window_seconds, bool) or window_seconds <= 0:
            raise TrafficValidationError("window_seconds 必须是正整数")
        self.ruleset = ruleset
        self.window_seconds = window_seconds

    def classify_packet(self, packet: Packet) -> Fact:
        rule_id, action, category, idx = self.ruleset.classify(
            packet.src, packet.dst, packet.protocol, packet.sport, packet.dport
        )
        return Fact(packet=packet, rule_id=rule_id, action=action, category=category, rule_index=idx)

    def load(self, packets: Iterable[Packet]) -> Analysis:
        facts: list[Fact] = []
        seen_ids: set[str] = set()
        for packet in packets:
            if packet.packet_id in seen_ids:
                raise TrafficValidationError(f"packet_id 重复: {packet.packet_id}")
            seen_ids.add(packet.packet_id)
            facts.append(self.classify_packet(packet))
        summaries = aggregate(facts, self.window_seconds)
        identity = {
            "ruleset_digest": self.ruleset.digest,
            "window_seconds": self.window_seconds,
            "facts": [(f.packet.packet_id, f.rule_id, f.action, f.category, f.packet.bytes) for f in facts],
        }
        identity_digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        analysis = Analysis(
            ruleset_name=self.ruleset.name,
            ruleset_digest=self.ruleset.digest,
            window_seconds=self.window_seconds,
            facts=tuple(facts),
            summaries=tuple(summaries),
            packets_total=len(facts),
            bytes_total=sum(f.packet.bytes for f in facts),
            identity_digest=identity_digest,
        )
        _check_conservation(analysis)
        return analysis


def aggregate(facts: Sequence[Fact], window_seconds: int) -> list[WindowSummary]:
    buckets: dict[tuple[int, str, str], dict[str, Any]] = {}
    for fact in facts:
        start = (fact.packet.ts // window_seconds) * window_seconds
        key = (start, fact.category, fact.action)
        bucket = buckets.setdefault(
            key, {"packets": 0, "bytes": 0, "rule_ids": {}, "sources": []}
        )
        bucket["packets"] += 1
        bucket["bytes"] += fact.packet.bytes
        bucket["rule_ids"][fact.rule_id] = bucket["rule_ids"].get(fact.rule_id, 0) + 1
        bucket["sources"].append(fact.packet.packet_id)
    summaries = [
        WindowSummary(
            window_start=start,
            window_seconds=window_seconds,
            category=category,
            action=action,
            packets=data["packets"],
            bytes=data["bytes"],
            rule_ids=dict(data["rule_ids"]),
            source_packet_ids=tuple(data["sources"]),
        )
        for (start, category, action), data in sorted(buckets.items())
    ]
    return summaries


def _check_conservation(analysis: Analysis) -> None:
    total_packets = sum(s.packets for s in analysis.summaries)
    total_bytes = sum(s.bytes for s in analysis.summaries)
    if total_packets != analysis.packets_total:
        raise AssertionError("聚合包数不守恒")
    if total_bytes != analysis.bytes_total:
        raise AssertionError("聚合字节数不守恒")
    source_count = sum(len(s.source_packet_ids) for s in analysis.summaries)
    if source_count != analysis.packets_total:
        raise AssertionError("聚合原包来源数不守恒")
    if sum(f.packet.bytes for f in analysis.facts) != analysis.bytes_total:
        raise AssertionError("事实字节数不守恒")


def save_analysis(analysis: Analysis, path: str | Path) -> str:
    """原子写事实产品；内容相同时不触碰已有文件（旧产品保持稳定）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (analysis.canonical_json() + "\n").encode("utf-8")
    if target.exists() and target.read_bytes() == payload:
        return analysis.digest
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(target)
    return analysis.digest


def read_packets(path: str | Path) -> list[Packet]:
    target = Path(path)
    packets: list[Packet] = []
    with target.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TrafficValidationError(f"{target}:{lineno} JSON 非法: {exc}") from exc
            packets.append(Packet.from_dict(obj))
    return packets


def write_packets(packets: Iterable[Packet], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for packet in packets:
            handle.write(json.dumps(packet.to_dict(), sort_keys=True, ensure_ascii=False) + "\n")
    tmp.replace(target)


def fact_identity_map(analysis: Analysis) -> dict[str, tuple[str, str, str]]:
    """packet_id → (rule_id, action, category)，便于跨产品对比。"""
    return {
        f.packet.packet_id: (f.rule_id, f.action, f.category) for f in analysis.facts
    }
