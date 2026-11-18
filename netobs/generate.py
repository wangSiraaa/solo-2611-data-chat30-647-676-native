"""合成规则与流量生成器（全部数据均为合成，不接触真实防火墙）。

生成的规则产品刻意覆盖题面要求的案例：

* ``U_A`` + ``U_B`` 联合覆盖 ``U_C``（端口切分，单条都不包含）；
* ``I1..I4`` 以 2×2 网格联合覆盖 ``I5``（源、目的交错）；
* ``P1``/``P2`` 按协议分离，联合覆盖 ANY 规则 ``P3``；
* ``B1``/``B2`` 以端口边界联合覆盖 ``B3``，``B4`` 留下 99/301 两个反例；
* 只有 ``O_ACCEPT`` 在 ``O_DENY`` 之前时才会被接受（仅换序即改变命中）；
* ``H1..H5`` 使用 32 位高位地址（128.0.0.0/1、240.0.0.0/4、/0 目的前缀等）。

总规模默认 80 条规则、100000 个包。
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .rules import (
    Rule,
    RuleSet,
    Prefix,
    reorder,
    save_ruleset,
)
from .traffic import Packet, write_packets

TARGET_RULES = 80
TARGET_PACKETS = 100_000


def _rule(
    rid: str,
    src: str,
    dst: str,
    proto: str,
    sport: tuple[int, int],
    dport: tuple[int, int],
    action: str,
    category: str,
) -> Rule:
    return Rule(
        rule_id=rid,
        src=Prefix.parse(src),
        dst=Prefix.parse(dst),
        protocol=proto,
        sport=sport,
        dport=dport,
        action=action,
        category=category,
    )


FULL = (0, 65535)


def interesting_rules() -> list[Rule]:
    rules: list[Rule] = []
    # -- 联合覆盖：源端口切分 ----------------------------------------
    rules += [
        _rule("U_A", "10.1.0.0/16", "172.16.0.0/16", "TCP", (0, 1023), (80, 80), "ACCEPT", "web"),
        _rule("U_B", "10.1.0.0/16", "172.16.0.0/16", "TCP", (1024, 65535), (80, 80), "ACCEPT", "web"),
        _rule("U_C", "10.1.0.0/16", "172.16.0.0/16", "TCP", FULL, (80, 80), "DENY", "blocked"),
    ]
    # -- 仅换序改变命中：先 DENY /16，后 ACCEPT /24 ------------------
    rules += [
        _rule("O_DENY", "10.5.0.0/16", "172.19.0.0/16", "TCP", FULL, (443, 443), "DENY", "blocked"),
        _rule("O_ACCEPT", "10.5.5.0/24", "172.19.0.0/16", "TCP", FULL, (443, 443), "ACCEPT", "web"),
    ]
    # -- 源/目的交错与包含 --------------------------------------------
    rules += [
        _rule("N1", "10.2.0.0/16", "172.16.0.0/16", "TCP", FULL, (8080, 8080), "ACCEPT", "app"),
        _rule("N2", "10.2.3.0/24", "172.16.1.0/24", "TCP", FULL, (8080, 8080), "ACCEPT", "partner"),
        _rule("N3", "10.2.3.128/25", "172.16.1.0/24", "TCP", FULL, (8080, 8080), "DENY", "blocked"),
        _rule("N4", "10.2.0.0/16", "172.16.0.0/16", "UDP", FULL, (8080, 8080), "ACCEPT", "app"),
        _rule("N5", "10.2.0.0/17", "172.16.0.0/18", "TCP", FULL, (8080, 8080), "DENY", "blocked"),
        _rule("N6", "10.2.0.0/15", "172.16.0.0/16", "TCP", FULL, (8080, 8080), "DENY", "blocked"),
    ]
    # -- 端口边界 ------------------------------------------------------
    rules += [
        _rule("B1", "10.4.0.0/16", "172.18.0.0/16", "TCP", FULL, (100, 200), "ACCEPT", "db"),
        _rule("B2", "10.4.0.0/16", "172.18.0.0/16", "TCP", FULL, (201, 300), "ACCEPT", "db"),
        _rule("B3", "10.4.0.0/16", "172.18.0.0/16", "TCP", FULL, (100, 300), "DENY", "blocked"),
        _rule("B4", "10.4.0.0/16", "172.18.0.0/16", "TCP", FULL, (99, 301), "DENY", "blocked"),
        _rule("B5", "10.4.1.0/24", "172.18.1.0/24", "TCP", (0, 1023), (5432, 5432), "ACCEPT", "db"),
        _rule("B6", "10.4.1.0/24", "172.18.1.0/24", "TCP", (1024, 65535), (5432, 5432), "ACCEPT", "db"),
        _rule("B7", "10.4.1.0/24", "172.18.1.0/24", "TCP", FULL, (5432, 5432), "DENY", "blocked"),
    ]
    # -- 协议分离 ------------------------------------------------------
    rules += [
        _rule("P1", "10.3.0.0/16", "172.17.0.0/16", "TCP", FULL, FULL, "DENY", "blocked"),
        _rule("P2", "10.3.0.0/16", "172.17.0.0/16", "UDP", FULL, FULL, "ACCEPT", "dns"),
        _rule("P3", "10.3.0.0/16", "172.17.0.0/16", "ANY", FULL, FULL, "ACCEPT", "misc"),
    ]
    # -- 2×2 源/目的交错联合覆盖 --------------------------------------
    rules += [
        _rule("I1", "10.6.0.0/16", "172.21.0.0/16", "TCP", FULL, (25, 25), "ACCEPT", "mail"),
        _rule("I2", "10.7.0.0/16", "172.22.0.0/16", "TCP", FULL, (25, 25), "ACCEPT", "mail"),
        _rule("I3", "10.6.0.0/16", "172.22.0.0/16", "TCP", FULL, (25, 25), "ACCEPT", "mail"),
        _rule("I4", "10.7.0.0/16", "172.21.0.0/16", "TCP", FULL, (25, 25), "ACCEPT", "mail"),
        # 172.21/16 与 172.22/16 无法合并为单个对齐前缀，因此目标规则拆成两条；
        # 每条都只被 I1/I4 或 I2/I3 的并集覆盖（源维交错）。
        _rule("I5A", "10.6.0.0/15", "172.21.0.0/16", "TCP", FULL, (25, 25), "DENY", "blocked"),
        _rule("I5B", "10.6.0.0/15", "172.22.0.0/16", "TCP", FULL, (25, 25), "DENY", "blocked"),
    ]
    # -- 组播/保留地址 ------------------------------------------------
    rules += [
        _rule("M1", "0.0.0.0/0", "224.0.0.0/4", "UDP", FULL, FULL, "DENY", "multicast"),
    ]
    # -- 32 位高位地址案例 -------------------------------------------
    rules += [
        _rule("H1", "203.0.113.0/24", "198.51.100.0/24", "TCP", FULL, (443, 443), "ACCEPT", "web"),
        _rule("H2", "203.0.113.0/24", "198.51.100.0/24", "TCP", FULL, (8443, 8443), "DENY", "blocked"),
        _rule("H3", "240.0.0.0/4", "0.0.0.0/0", "UDP", FULL, FULL, "DENY", "reserved"),
        _rule("H4", "128.0.0.0/1", "172.20.0.0/16", "TCP", FULL, (80, 80), "ACCEPT", "web"),
        _rule("H5", "128.0.1.0/24", "172.20.0.0/16", "TCP", FULL, (80, 80), "ACCEPT", "partner"),
    ]
    return rules


def build_ruleset(seed: int = 7) -> RuleSet:
    rules = interesting_rules()
    needed = TARGET_RULES - len(rules)
    if needed < 0:
        raise AssertionError("interesting rules 已超过目标规模")
    # 填充规则使用互不重叠的源/目的段，避免改变上面的演示案例。
    for i in range(needed):
        rules.append(
            _rule(
                f"F{i:03d}",
                f"10.{100 + i}.0.0/16",
                f"172.{100 + i}.0.0/16",
                "TCP" if i % 3 else "UDP",
                FULL,
                (20000 + i, 20000 + i),
                "ACCEPT",
                f"fill{i % 5}",
            )
        )
    return RuleSet(
        rules=tuple(rules),
        default_action="ACCEPT",
        default_category="default",
        name="synthetic-base",
    )


def reorder_lpm_variant(base: RuleSet) -> RuleSet:
    """模拟“最长前缀优先”造成的换序：把 O_ACCEPT 提到 O_DENY 之前。"""
    ids = [rule.rule_id for rule in base.rules]
    i_deny = ids.index("O_DENY")
    i_accept = ids.index("O_ACCEPT")
    ids[i_deny], ids[i_accept] = ids[i_accept], ids[i_deny]
    variant = reorder(base, ids)
    return RuleSet(
        rules=variant.rules,
        default_action=variant.default_action,
        default_category=variant.default_category,
        name="synthetic-reorder-lpm",
    )


def revised_variant(base: RuleSet) -> RuleSet:
    """范围修订：B1 的目端口从 100..200 缩到 100..150。"""
    rules = []
    for rule in base.rules:
        if rule.rule_id == "B1":
            rules.append(
                Rule(
                    rule_id=rule.rule_id,
                    src=rule.src,
                    dst=rule.dst,
                    protocol=rule.protocol,
                    sport=rule.sport,
                    dport=(100, 150),
                    action=rule.action,
                    category=rule.category,
                )
            )
        else:
            rules.append(rule)
    return RuleSet(
        rules=tuple(rules),
        default_action=base.default_action,
        default_category=base.default_category,
        name="synthetic-revised",
    )


def deleted_variant(base: RuleSet) -> RuleSet:
    """删除 TCP 拒绝规则 P1：原本被遮蔽的 ANY 规则 P3 变为可达。"""
    rules = tuple(rule for rule in base.rules if rule.rule_id != "P1")
    return RuleSet(
        rules=rules,
        default_action=base.default_action,
        default_category=base.default_category,
        name="synthetic-deleted",
    )


def _rand_ip(rng: random.Random, prefix: str, host_bits: int = 16) -> int:
    net = Prefix.parse(prefix)
    span = 1 << host_bits
    return net.first + rng.randrange(span)


def generate_packets(ruleset: RuleSet, count: int = TARGET_PACKETS, seed: int = 11) -> list[Packet]:
    rng = random.Random(seed)
    templates: list[tuple[int, int, str, int, int]] = []

    def add(src: str, dst: str, proto: str, sport: int, dport: int) -> None:
        templates.append(
            (
                Prefix.parse(src).first + rng.randrange(max(1, Prefix.parse(src).size)),
                Prefix.parse(dst).first + rng.randrange(max(1, Prefix.parse(dst).size)),
                proto,
                sport,
                dport,
            )
        )

    # 联合覆盖 / 端口边界 / 协议分离等代表性流量
    for sp in (0, 1023, 1024, 65535):
        add("10.1.2.0/24", "172.16.0.0/16", "TCP", sp, 80)
    add("10.1.2.3/32", "172.16.9.9/32", "TCP", 2000, 81)
    add("10.5.5.1/32", "172.19.0.1/32", "TCP", 3000, 443)
    add("10.5.5.200/32", "172.19.2.3/32", "TCP", 40000, 443)
    add("10.5.7.7/32", "172.19.2.3/32", "TCP", 40001, 443)
    add("10.2.3.4/32", "172.16.1.4/32", "TCP", 5000, 8080)
    add("10.2.3.200/32", "172.16.1.4/32", "TCP", 5001, 8080)
    add("10.3.0.5/32", "172.17.0.5/32", "TCP", 6000, 53)
    add("10.3.0.5/32", "172.17.0.5/32", "UDP", 6001, 53)
    for dp in (99, 100, 150, 200, 201, 300, 301):
        add("10.4.0.5/32", "172.18.0.5/32", "TCP", 7000, dp)
    for sp in (0, 1023, 1024, 65535):
        add("10.4.1.5/32", "172.18.1.5/32", "TCP", sp, 5432)
    for src, dst in (
        ("10.6.1.1/32", "172.21.1.1/32"),
        ("10.6.1.1/32", "172.22.1.1/32"),
        ("10.7.1.1/32", "172.21.1.1/32"),
        ("10.7.1.1/32", "172.22.1.1/32"),
    ):
        add(src, dst, "TCP", 8000, 25)
    # 高位地址
    add("203.0.113.7/32", "198.51.100.7/32", "TCP", 9000, 443)
    add("203.0.113.8/32", "198.51.100.7/32", "TCP", 9001, 8443)
    add("240.1.2.3/32", "8.8.8.8/32", "UDP", 9002, 5353)
    add("200.1.2.3/32", "172.20.0.5/32", "TCP", 9003, 80)
    add("255.255.255.255/32", "172.20.0.5/32", "TCP", 9004, 80)
    add("128.0.0.1/32", "172.20.0.5/32", "TCP", 9005, 80)
    add("129.1.2.3/32", "172.20.9.9/32", "TCP", 9006, 80)
    add("10.9.9.9/32", "224.0.0.1/32", "UDP", 9007, 1900)
    # 默认路径
    add("192.0.2.1/32", "203.0.113.1/32", "TCP", 10000, 12345)
    add("198.18.0.1/32", "198.19.0.1/32", "UDP", 10001, 9999)

    packets: list[Packet] = []
    filler_prefixes = [
        (rule.src, rule.dst, rule.sport[0], rule.dport[0], rule.protocol)
        for rule in ruleset.rules
        if rule.rule_id.startswith("F")
    ]
    for i in range(count):
        if i < len(templates) * 40 and i % 3 != 0:
            src, dst, proto, sport, dport = templates[i % len(templates)]
        elif filler_prefixes and i % 7 == 0:
            src_p, dst_p, sport, dport, proto = filler_prefixes[i % len(filler_prefixes)]
            src = src_p.first + rng.randrange(src_p.size)
            dst = dst_p.first + rng.randrange(dst_p.size)
        else:
            # 随机主干流量；故意包含高位地址与小样本之外的值
            roll = rng.random()
            if roll < 0.55:
                src = _rand_ip(rng, "10.0.0.0/8", 16)
                dst = _rand_ip(rng, "172.16.0.0/12", 16)
            elif roll < 0.75:
                src = rng.randrange(0, 1 << 32)
                dst = rng.randrange(0, 1 << 32)
            else:
                src = _rand_ip(rng, "128.0.0.0/1", 20)
                dst = _rand_ip(rng, "0.0.0.0/1", 20)
            proto = "TCP" if rng.random() < 0.7 else "UDP"
            sport = rng.randrange(0, 65536)
            dport = rng.choice(
                [rng.randrange(0, 65536), 80, 443, 8080, 53, 25, 5432, 8443, rng.randrange(0, 1024)]
            )
        ts = (i * 36) % 3601
        size = rng.choice([40, 60, 64, 128, 512, 1400, 1500, rng.randrange(0, 1501)])
        packets.append(
            Packet(
                packet_id=f"pkt-{i:06d}",
                ts=ts,
                src=src,
                dst=dst,
                protocol=proto,
                sport=sport,
                dport=dport,
                bytes=size,
            )
        )
    return packets


def build_all(root: str | Path, *, packet_count: int = TARGET_PACKETS) -> dict[str, Any]:
    root = Path(root)
    data = root / "data"
    artifacts = root / "artifacts"
    data.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)

    base = build_ruleset()
    variants = {
        "base": base,
        "reorder_lpm": reorder_lpm_variant(base),
        "revised": revised_variant(base),
        "deleted": deleted_variant(base),
    }
    digests = {}
    for name, ruleset in variants.items():
        digests[name] = save_ruleset(ruleset, data / f"rules_{name}.json")

    packets = generate_packets(base, count=packet_count)
    write_packets(packets, data / "packets.jsonl")

    summary = {
        "rules": {name: len(rs.rules) for name, rs in variants.items()},
        "ruleset_digests": digests,
        "packets": len(packets),
        "high_bit_packets": sum(1 for p in packets if p.src >= 0x80000000 or p.dst >= 0x80000000),
        "interesting_rule_ids": [r.rule_id for r in interesting_rules()],
        "paths": {
            "data": str(data),
            "artifacts": str(artifacts),
        },
    }
    (artifacts / "generate_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary
