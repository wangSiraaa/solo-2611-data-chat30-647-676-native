"""独立参考实现：小地址/端口域完整枚举 + 32 位高位用例核对。

参考语义用显式规则循环写死（不调用索引、不调用编译单元），作为交叉验证的
“第二实现”：

* :func:`reference_classify` —— 纯五维逐条比较；
* :func:`enumerate_all` —— 对给定离散取值集合做**完整笛卡尔积**枚举，
  同时核对 ``RuleSet``、``RuleIndex``、``CompiledRules`` 三者结果；
* :func:`small_domain_ruleset` —— 地址只落在 ``0..7``、端口只落在
  ``0..5`` 的小域规则，支持真正穷举；
* :func:`check_byte_conservation` —— 逐包核对归因与聚合守恒。

本模块只复用 :mod:`netobs.rules` 的数据结构，不依赖任何流量样本。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Iterable, Sequence

from .compile import CompiledRules, compile_rules
from .index import RuleIndex
from .rules import Prefix, Rule, RuleSet
from .traffic import Analysis, Fact, Packet

DEFAULT_ID = "DEFAULT"


def reference_classify(
    ruleset: RuleSet, src: int, dst: int, protocol: str, sport: int, dport: int
) -> tuple[str, str, str, int]:
    """显式首匹配参考实现（故意不用任何加速结构）。"""
    for index in range(len(ruleset.rules)):
        rule = ruleset.rules[index]
        if rule.protocol != "ANY" and rule.protocol != protocol:
            continue
        if sport < rule.sport[0] or sport > rule.sport[1]:
            continue
        if dport < rule.dport[0] or dport > rule.dport[1]:
            continue
        if not (rule.src.first <= src <= rule.src.last):
            continue
        if not (rule.dst.first <= dst <= rule.dst.last):
            continue
        return rule.rule_id, rule.action, rule.category, index
    return DEFAULT_ID, ruleset.default_action, ruleset.default_category, len(ruleset.rules)


@dataclass
class EnumerationResult:
    points: int
    mismatches: list[str]
    compiled_cells: int

    @property
    def ok(self) -> bool:
        return not self.mismatches


def enumerate_all(
    ruleset: RuleSet,
    sources: Sequence[int],
    destinations: Sequence[int],
    protocols: Sequence[str],
    sports: Sequence[int],
    dports: Sequence[int],
    *,
    compiled: CompiledRules | None = None,
    index: RuleIndex | None = None,
    limit_mismatches: int = 20,
) -> EnumerationResult:
    """完整枚举五维笛卡尔积，逐点比较三套实现。"""
    index = index if index is not None else RuleIndex(ruleset)
    compiled = compiled if compiled is not None else compile_rules(ruleset)
    ranks = {rule.rule_id: i for i, rule in enumerate(ruleset.rules)}
    mismatches: list[str] = []
    points = 0
    for src, dst, proto, sport, dport in itertools.product(
        sources, destinations, protocols, sports, dports
    ):
        points += 1
        expected = reference_classify(ruleset, src, dst, proto, sport, dport)
        actual_rule = ruleset.classify(src, dst, proto, sport, dport)
        actual_index = index.classify(src, dst, proto, sport, dport)
        cell = compiled.classify(src, dst, proto, sport, dport)
        actual_compiled = (
            cell.rule_id,
            cell.action,
            cell.category,
            ranks.get(cell.rule_id, len(ruleset.rules)),
        )
        if actual_rule != expected:
            _add(mismatches, limit_mismatches, f"RuleSet {(src, dst, proto, sport, dport)}: {actual_rule} != {expected}")
        if actual_index != expected:
            _add(mismatches, limit_mismatches, f"RuleIndex {(src, dst, proto, sport, dport)}: {actual_index} != {expected}")
        if actual_compiled != expected:
            _add(mismatches, limit_mismatches, f"Compiled {(src, dst, proto, sport, dport)}: {actual_compiled} != {expected}")
    return EnumerationResult(points=points, mismatches=mismatches, compiled_cells=len(compiled.cells))


def _add(items: list[str], limit: int, message: str) -> None:
    if len(items) < limit:
        items.append(message)


def small_domain_ruleset() -> tuple[RuleSet, dict[str, Sequence[int]]]:
    """地址 0..7、端口 0..5 的小域规则集（可完整穷举）。

    前缀显式给出网络位，全部落在 32 位地址的极低位，便于把“地址整数 0..7”
    与真实 CIDR 语义对应起来。
    """

    def pref(addr: int, plen: int) -> Prefix:
        return Prefix(addr, plen)

    def rule(rid, src, dst, proto, sport, dport, action, category) -> Rule:
        return Rule(rid, src, dst, proto, sport, dport, action, category)

    ALL = pref(0, 29)  # 0..7
    P01 = pref(0, 31)  # 0..1
    P23 = pref(2, 31)  # 2..3
    P03 = pref(0, 30)  # 0..3
    P45 = pref(4, 31)  # 4..5
    P67 = pref(6, 31)  # 6..7

    rules = (
        # 前序两条联合遮蔽 S3（源端口切分）
        rule("S1", ALL, ALL, "TCP", (0, 2), (1, 2), "ACCEPT", "a"),
        rule("S2", ALL, ALL, "TCP", (3, 5), (1, 2), "ACCEPT", "b"),
        rule("S3", ALL, ALL, "TCP", (0, 5), (1, 2), "DENY", "c"),
        # 源/目的交错 2×2 联合遮蔽 S8
        rule("S4", P01, P01, "UDP", (0, 5), (3, 3), "ACCEPT", "d"),
        rule("S5", P01, P23, "UDP", (0, 5), (3, 3), "ACCEPT", "d"),
        rule("S6", P23, P01, "UDP", (0, 5), (3, 3), "ACCEPT", "d"),
        rule("S7", P23, P23, "UDP", (0, 5), (3, 3), "ACCEPT", "d"),
        rule("S8", P03, P03, "UDP", (0, 5), (3, 3), "DENY", "e"),
        # 协议分离：TCP/UDP 联合遮蔽 ANY（ANY 规则端口必须全域）
        rule("S9", P45, P45, "TCP", (0, 65535), (0, 65535), "DENY", "f"),
        rule("S10", P45, P45, "UDP", (0, 65535), (0, 65535), "ACCEPT", "g"),
        rule("S11", P45, P45, "ANY", (0, 65535), (0, 65535), "ACCEPT", "h"),
        # 仅换序：宽 ACCEPT 在前吞掉窄 DENY
        rule("S12", P67, P67, "TCP", (0, 5), (5, 5), "ACCEPT", "i"),
        rule("S13", P67, P67, "TCP", (0, 5), (5, 5), "DENY", "j"),
        # 未被遮蔽：留下反例
        rule("S14", ALL, P67, "TCP", (0, 1), (0, 5), "DENY", "k"),
    )
    ruleset = RuleSet(rules, default_action="DENY", default_category="z", name="small-domain")
    domain = {
        "sources": list(range(8)),
        "destinations": list(range(8)),
        "protocols": ["TCP", "UDP"],
        "sports": list(range(6)),
        "dports": list(range(6)),
    }
    return ruleset, domain


def verify_facts_against_reference(ruleset: RuleSet, facts: Iterable[Fact]) -> list[str]:
    issues: list[str] = []
    for fact in facts:
        expected = reference_classify(
            ruleset,
            fact.packet.src,
            fact.packet.dst,
            fact.packet.protocol,
            fact.packet.sport,
            fact.packet.dport,
        )
        actual = (fact.rule_id, fact.action, fact.category, fact.rule_index)
        if actual != expected:
            issues.append(f"{fact.packet.packet_id}: {actual} != {expected}")
    return issues


def check_byte_conservation(
    analysis: Analysis, packets: Sequence[Packet], ruleset: RuleSet
) -> list[str]:
    """逐包核对归因与聚合守恒（包含 DENY 类别）。"""
    issues: list[str] = []
    by_id = {p.packet_id: p for p in packets}
    if len(by_id) != len(packets):
        issues.append("输入包 packet_id 存在重复")
    fact_ids = [f.packet.packet_id for f in analysis.facts]
    if sorted(fact_ids) != sorted(by_id):
        issues.append("事实未一一覆盖输入包")

    fact_bytes = 0
    for fact in analysis.facts:
        original = by_id.get(fact.packet.packet_id)
        if original is None:
            issues.append(f"事实含未知包 {fact.packet.packet_id}")
            continue
        if original != fact.packet:
            issues.append(f"事实篡改了原包内容 {fact.packet.packet_id}")
        expected = reference_classify(
            ruleset,
            original.src,
            original.dst,
            original.protocol,
            original.sport,
            original.dport,
        )
        if (fact.rule_id, fact.action, fact.category, fact.rule_index) != expected:
            issues.append(f"归因不符 {fact.packet.packet_id}")
        fact_bytes += original.bytes

    summary_packets = sum(s.packets for s in analysis.summaries)
    summary_bytes = sum(s.bytes for s in analysis.summaries)
    if summary_packets != len(packets):
        issues.append(f"聚合包数 {summary_packets} != 输入 {len(packets)}")
    if summary_bytes != fact_bytes:
        issues.append(f"聚合字节 {summary_bytes} != 逐包 {fact_bytes}")
    if summary_bytes != sum(p.bytes for p in packets):
        issues.append("聚合字节与原包总量不符")

    facts_by_id = {f.packet.packet_id: f for f in analysis.facts}
    for summary in analysis.summaries:
        if summary.packets != len(summary.source_packet_ids):
            issues.append(f"窗口 {summary.window_start}/{summary.category} 来源数不符")
        for pid in summary.source_packet_ids:
            fact = facts_by_id.get(pid)
            if fact is None:
                issues.append(f"汇总来源 {pid} 不存在")
            elif fact.category != summary.category or fact.action != summary.action:
                issues.append(f"汇总来源 {pid} 类别不一致")
    return issues


def high_bit_points() -> list[tuple[int, int, str, int, int]]:
    """显式 32 位高位五元组，防止仅验证小样本。"""
    return [
        (0xFFFFFFFF, 0x00000000, "TCP", 0, 80),
        (0x80000000, 0x7FFFFFFF, "TCP", 65535, 80),
        (0xF0000000, 0xFFFFFFFF, "UDP", 12345, 5353),
        (0xCB007107, 0xC6336407, "TCP", 9000, 443),  # 203.0.113.7 -> 198.51.100.7
        (0xCB007108, 0xC6336407, "TCP", 9001, 8443),
        (0xF0010203, 0x08080808, "UDP", 9002, 5353),  # 240.1.2.3 -> 8.8.8.8 (H3)
        (0x00000000, 0xE0000001, "UDP", 9007, 1900),  # 0.0.0.0 -> 224.0.0.1 (M1)
        (0x80000001, 0xAC140005, "TCP", 9005, 80),    # 128.0.0.1 -> 172.20.0.5 (H4)
        (0xCA000001, 0xD1030001, "TCP", 10000, 12345),  # 202.0.0.1 -> 209.3.0.1 默认
    ]
