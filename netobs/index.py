"""精确规则索引：源/目前缀二进制 trie，用于批量首匹配分类。

索引只负责**候选裁剪**：trie 返回所有源前缀包含该地址（目的同理）的
规则下标，交集之后再按规则顺序做完整五维匹配（协议与端口也是产品规则
自身条件）。因此索引不改变语义，也不是“最长前缀优先”。

对任意输入，候选集必包含全部可能命中的规则；按显式下标扫描即可得到
与 :meth:`RuleSet.first_match` 相同的首匹配结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from .rules import RuleSet

DEFAULT_ID = "DEFAULT"


class _Node:
    __slots__ = ("children", "rules")

    def __init__(self) -> None:
        self.children: dict[int, "_Node"] = {}
        self.rules: list[int] = []


class PrefixTrie:
    """把规则前缀插入二进制 trie；查询时收集路径上全部规则下标。"""

    def __init__(self) -> None:
        self.root = _Node()

    def insert(self, addr: int, plen: int, rule_index: int) -> None:
        node = self.root
        for bit_pos in range(31, 31 - plen, -1):
            bit = (addr >> bit_pos) & 1
            node = node.children.setdefault(bit, _Node())
        node.rules.append(rule_index)

    def lookup(self, addr: int) -> set[int]:
        out: set[int] = set(self.root.rules)
        node = self.root
        for bit_pos in range(31, -1, -1):
            node = node.children.get((addr >> bit_pos) & 1)
            if node is None:
                break
            if node.rules:
                out.update(node.rules)
        return out


@dataclass
class RuleIndex:
    ruleset: RuleSet

    def __post_init__(self) -> None:
        self._src = PrefixTrie()
        self._dst = PrefixTrie()
        for idx, rule in enumerate(self.ruleset.rules):
            self._src.insert(rule.src.addr, rule.src.plen, idx)
            self._dst.insert(rule.dst.addr, rule.dst.plen, idx)

    def candidates(self, src: int, dst: int) -> list[int]:
        left = self._src.lookup(src)
        right = self._dst.lookup(dst)
        if len(left) > len(right):
            left, right = right, left
        return sorted(idx for idx in left if idx in right)

    def first_match(
        self, src: int, dst: int, proto: str, sport: int, dport: int
    ) -> tuple[object | None, int]:
        rules = self.ruleset.rules
        for idx in self.candidates(src, dst):
            rule = rules[idx]
            if rule.matches(src, dst, proto, sport, dport):
                return rule, idx
        return None, len(rules)

    def classify(
        self, src: int, dst: int, proto: str, sport: int, dport: int
    ) -> tuple[str, str, str, int]:
        rule, idx = self.first_match(src, dst, proto, sport, dport)
        if rule is None:
            return DEFAULT_ID, self.ruleset.default_action, self.ruleset.default_category, idx
        return rule.rule_id, rule.action, rule.category, idx

    def stats(self) -> dict:
        return {
            "src_trie_nodes": _count_nodes(self._src.root),
            "dst_trie_nodes": _count_nodes(self._dst.root),
        }


def _count_nodes(root: _Node) -> int:
    total = 0
    stack = [root]
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(node.children.values())
    return total
