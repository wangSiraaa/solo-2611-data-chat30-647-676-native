"""有序双端地址规则产品（ordered dual-address rule product）。

IPv4 地址一律按无符号 32 位整数解释；CIDR 输入必须满足“主机位全 0”，
解析时再规范化网络位。地址域、端口、前缀长度做严格校验，非法输入直接
抛 :class:`RuleValidationError`，不做静默截断。

规则语义：显式列表顺序，第一条“全条件匹配”的规则决定结果；否则使用
末尾默认 action/category。ANY 规则的端口必须为全域 ``0..65535``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

UINT32_MAX = 0xFFFFFFFF
PORT_MIN = 0
PORT_MAX = 65535
FULL_PORT_RANGE = (PORT_MIN, PORT_MAX)
PROTOCOLS = ("ANY", "TCP", "UDP")
PORTED_PROTOCOLS = ("TCP", "UDP")
ACTIONS = ("ACCEPT", "DENY")


class RuleValidationError(ValueError):
    """规则产品格式或取值非法。"""


def parse_ipv4(text: Any) -> int:
    """严格解析点分十进制 IPv4，返回无符号 32 位整数。"""
    if not isinstance(text, str):
        raise RuleValidationError(f"IPv4 地址必须是字符串，收到 {type(text).__name__}")
    parts = text.strip().split(".")
    if len(parts) != 4:
        raise RuleValidationError(f"IPv4 地址必须恰好 4 段: {text!r}")
    value = 0
    for part in parts:
        if not part.isdigit() or (len(part) > 1 and part[0] == "0"):
            raise RuleValidationError(f"IPv4 段非法或含前导零: {text!r}")
        octet = int(part)
        if octet > 255:
            raise RuleValidationError(f"IPv4 段越界: {text!r}")
        value = (value << 8) | octet
    return value


def format_ipv4(value: int) -> str:
    if not 0 <= value <= UINT32_MAX:
        raise RuleValidationError(f"IPv4 整数越界: {value}")
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


@dataclass(frozen=True, order=True)
class Prefix:
    """规范化的 IPv4 CIDR（主机位一定为 0）。"""

    addr: int
    plen: int

    def __post_init__(self) -> None:
        if not 0 <= self.plen <= 32:
            raise RuleValidationError(f"前缀长度必须在 0..32: {self.plen}")
        if not 0 <= self.addr <= UINT32_MAX:
            raise RuleValidationError(f"地址越界: {self.addr}")
        mask = self.mask
        if self.addr & ~mask:
            raise RuleValidationError(
                f"CIDR 主机位非零，需规范化网络位: {format_ipv4(self.addr)}/{self.plen}"
            )

    @property
    def mask(self) -> int:
        if self.plen == 0:
            return 0
        return (UINT32_MAX << (32 - self.plen)) & UINT32_MAX

    @property
    def size(self) -> int:
        return 1 << (32 - self.plen)

    @property
    def first(self) -> int:
        return self.addr

    @property
    def last(self) -> int:
        return self.addr | (UINT32_MAX ^ self.mask)

    def contains(self, ip: int) -> bool:
        return (ip & self.mask) == self.addr

    def split(self) -> tuple["Prefix", "Prefix"] | None:
        if self.plen == 32:
            return None
        child = self.plen + 1
        half = 1 << (32 - child)
        return Prefix(self.addr, child), Prefix(self.first + half, child)

    def __str__(self) -> str:
        return f"{format_ipv4(self.addr)}/{self.plen}"

    @classmethod
    def parse(cls, text: Any) -> "Prefix":
        if not isinstance(text, str) or text.count("/") != 1:
            raise RuleValidationError(f"CIDR 必须是 'a.b.c.d/len': {text!r}")
        addr_text, plen_text = text.split("/")
        if not plen_text.isdigit() or (len(plen_text) > 1 and plen_text[0] == "0"):
            raise RuleValidationError(f"前缀长度非法: {text!r}")
        return cls(parse_ipv4(addr_text), int(plen_text))

    def to_dict(self) -> dict[str, Any]:
        return {"cidr": str(self), "addr": self.addr, "plen": self.plen}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    src: Prefix
    dst: Prefix
    protocol: str
    sport: tuple[int, int]
    dport: tuple[int, int]
    action: str
    category: str

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not self.rule_id:
            raise RuleValidationError("rule_id 必须是非空字符串")
        if not isinstance(self.src, Prefix) or not isinstance(self.dst, Prefix):
            raise RuleValidationError(f"{self.rule_id}: 源/目的前缀类型非法")
        if self.protocol not in PROTOCOLS:
            raise RuleValidationError(f"{self.rule_id}: 协议非法 {self.protocol!r}")
        for name, rng in (("sport", self.sport), ("dport", self.dport)):
            _check_port_range(self.rule_id, name, rng)
        if self.protocol == "ANY" and (
            self.sport != FULL_PORT_RANGE or self.dport != FULL_PORT_RANGE
        ):
            raise RuleValidationError(f"{self.rule_id}: ANY 协议规则端口必须全域 0..65535")
        if self.action not in ACTIONS:
            raise RuleValidationError(f"{self.rule_id}: action 非法 {self.action!r}")
        if not isinstance(self.category, str) or not self.category:
            raise RuleValidationError(f"{self.rule_id}: category 必须是非空字符串")

    def matches(
        self,
        src: int,
        dst: int,
        protocol: str,
        sport: int,
        dport: int,
    ) -> bool:
        if self.protocol != "ANY" and self.protocol != protocol:
            return False
        if not (self.sport[0] <= sport <= self.sport[1]):
            return False
        if not (self.dport[0] <= dport <= self.dport[1]):
            return False
        return self.src.contains(src) and self.dst.contains(dst)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "src": str(self.src),
            "dst": str(self.dst),
            "protocol": self.protocol,
            "sport": [self.sport[0], self.sport[1]],
            "dport": [self.dport[0], self.dport[1]],
            "action": self.action,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "Rule":
        _require_keys(
            obj,
            ("rule_id", "src", "dst", "protocol", "sport", "dport", "action", "category"),
            "rule",
        )
        return cls(
            rule_id=obj["rule_id"],
            src=Prefix.parse(obj["src"]),
            dst=Prefix.parse(obj["dst"]),
            protocol=obj["protocol"],
            sport=_parse_range(obj["sport"], "sport"),
            dport=_parse_range(obj["dport"], "dport"),
            action=obj["action"],
            category=obj["category"],
        )


def _check_port_range(rule_id: str, name: str, rng: tuple[int, int]) -> None:
    if not (isinstance(rng, tuple) and len(rng) == 2):
        raise RuleValidationError(f"{rule_id}: {name} 必须是 (lo, hi)")
    lo, hi = rng
    for value in (lo, hi):
        if not isinstance(value, int) or isinstance(value, bool):
            raise RuleValidationError(f"{rule_id}: {name} 端点必须为整数")
        if not PORT_MIN <= value <= PORT_MAX:
            raise RuleValidationError(f"{rule_id}: {name} 端点越界 {value}")
    if lo > hi:
        raise RuleValidationError(f"{rule_id}: {name} 下界大于上界 {rng}")


def _parse_range(value: Any, name: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise RuleValidationError(f"{name} 必须是 [lo, hi]")
    lo, hi = value
    if not isinstance(lo, int) or isinstance(lo, bool) or not isinstance(hi, int):
        raise RuleValidationError(f"{name} 端点必须为整数")
    return (lo, hi)


def _require_keys(obj: Any, keys: Iterable[str], what: str) -> None:
    if not isinstance(obj, dict):
        raise RuleValidationError(f"{what} 必须是 JSON 对象")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise RuleValidationError(f"{what} 缺少字段 {missing}")


@dataclass(frozen=True)
class RuleSet:
    """不可变有序规则产品 + 默认结果。"""

    rules: tuple[Rule, ...]
    default_action: str
    default_category: str
    name: str = "ruleset"

    def __post_init__(self) -> None:
        if not isinstance(self.rules, tuple):
            raise RuleValidationError("rules 必须是 tuple（不可变）")
        seen: set[str] = set()
        for rule in self.rules:
            if not isinstance(rule, Rule):
                raise RuleValidationError("rules 元素必须是 Rule")
            if rule.rule_id in seen:
                raise RuleValidationError(f"rule_id 重复: {rule.rule_id}")
            seen.add(rule.rule_id)
        if self.default_action not in ACTIONS:
            raise RuleValidationError(f"默认 action 非法: {self.default_action!r}")
        if not isinstance(self.default_category, str) or not self.default_category:
            raise RuleValidationError("默认 category 必须是非空字符串")

    def first_match(
        self,
        src: int,
        dst: int,
        protocol: str,
        sport: int,
        dport: int,
    ) -> tuple[Rule | None, int]:
        """返回 (命中规则或 None, 命中下标；默认身份为 len(rules))。"""
        for idx, rule in enumerate(self.rules):
            if rule.matches(src, dst, protocol, sport, dport):
                return rule, idx
        return None, len(self.rules)

    def classify(
        self,
        src: int,
        dst: int,
        protocol: str,
        sport: int,
        dport: int,
    ) -> tuple[str, str, str, int]:
        rule, idx = self.first_match(src, dst, protocol, sport, dport)
        if rule is None:
            return "DEFAULT", self.default_action, self.default_category, idx
        return rule.rule_id, rule.action, rule.category, idx

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "name": self.name,
            "rules": [rule.to_dict() for rule in self.rules],
            "default": {"action": self.default_action, "category": self.default_category},
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "RuleSet":
        _require_keys(obj, ("rules", "default"), "ruleset")
        default = obj["default"]
        _require_keys(default, ("action", "category"), "default")
        if not isinstance(obj["rules"], list):
            raise RuleValidationError("rules 必须是数组")
        return cls(
            rules=tuple(Rule.from_dict(item) for item in obj["rules"]),
            default_action=default["action"],
            default_category=default["category"],
            name=obj.get("name", "ruleset"),
        )


def load_ruleset(path: str | Path) -> RuleSet:
    raw = Path(path).read_bytes()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuleValidationError(f"规则文件不是合法 UTF-8 JSON: {exc}") from exc
    return RuleSet.from_dict(obj)


def save_ruleset(ruleset: RuleSet, path: str | Path) -> str:
    """原子写规则产品，返回 SHA-256 摘要。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (ruleset.canonical_json() + "\n").encode("utf-8")
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(target)
    return ruleset.digest


def reorder(ruleset: RuleSet, rule_ids: Sequence[str]) -> RuleSet:
    """按给定 rule_id 序列重排规则；集合必须与原来完全一致。"""
    if len(rule_ids) != len(ruleset.rules):
        raise RuleValidationError("重排序列长度与规则数不一致")
    by_id = {rule.rule_id: rule for rule in ruleset.rules}
    if len(set(rule_ids)) != len(rule_ids):
        raise RuleValidationError("重排序列含重复 rule_id")
    if set(rule_ids) != set(by_id):
        raise RuleValidationError("重排序列与规则集合不一致")
    return RuleSet(
        rules=tuple(by_id[rid] for rid in rule_ids),
        default_action=ruleset.default_action,
        default_category=ruleset.default_category,
        name=ruleset.name,
    )
