"""规则解析与严格校验测试。"""

import unittest

from netobs.rules import (
    Prefix,
    Rule,
    RuleSet,
    RuleValidationError,
    format_ipv4,
    load_ruleset,
    parse_ipv4,
    reorder,
    save_ruleset,
)


class PrefixTests(unittest.TestCase):
    def test_parse_and_format_roundtrip(self):
        cases = [
            "0.0.0.0/0",
            "10.0.0.0/8",
            "255.255.255.255/32",
            "128.0.0.0/1",
            "240.0.0.0/4",
            "203.0.113.0/24",
        ]
        for text in cases:
            prefix = Prefix.parse(text)
            self.assertEqual(str(prefix), text)
            self.assertEqual(Prefix.parse(str(prefix)), prefix)

    def test_host_bits_must_be_zero(self):
        with self.assertRaises(RuleValidationError):
            Prefix.parse("10.0.0.1/8")
        with self.assertRaises(RuleValidationError):
            Prefix.parse("192.168.1.1/24")

    def test_bad_prefix_length(self):
        for text in ("10.0.0.0/33", "10.0.0.0/-1", "10.0.0.0/8.5", "10.0.0.0/"):
            with self.assertRaises(RuleValidationError, msg=text):
                Prefix.parse(text)

    def test_ipv4_strictness(self):
        for text in ("1.2.3", "1.2.3.4.5", "256.1.1.1", "01.2.3.4", "1.2.3.-4", "a.b.c.d", ""):
            with self.assertRaises(RuleValidationError, msg=text):
                parse_ipv4(text)
        self.assertEqual(parse_ipv4("255.255.255.255"), 0xFFFFFFFF)
        self.assertEqual(format_ipv4(0xCB007107), "203.0.113.7")

    def test_range_bounds(self):
        p = Prefix.parse("240.0.0.0/4")
        self.assertEqual(p.first, 0xF0000000)
        self.assertEqual(p.last, 0xFFFFFFFF)
        self.assertEqual(Prefix.parse("0.0.0.0/0").last, 0xFFFFFFFF)
        self.assertEqual(Prefix.parse("128.0.0.0/1").first, 0x80000000)


def _rule(rid, **kw):
    base = dict(
        rule_id=rid,
        src=Prefix.parse("10.0.0.0/8"),
        dst=Prefix.parse("0.0.0.0/0"),
        protocol="TCP",
        sport=(0, 1023),
        dport=(80, 80),
        action="ACCEPT",
        category="web",
    )
    base.update(kw)
    return Rule(**base)


class RuleTests(unittest.TestCase):
    def test_any_requires_full_ports(self):
        with self.assertRaises(RuleValidationError):
            _rule("x", protocol="ANY", sport=(0, 100))
        with self.assertRaises(RuleValidationError):
            _rule("x", protocol="ANY", sport=(0, 65535), dport=(0, 100))
        _rule("ok", protocol="ANY", sport=(0, 65535), dport=(0, 65535))

    def test_port_validation(self):
        for sport in ((10, 5), (-1, 5), (0, 65536)):
            with self.assertRaises(RuleValidationError, msg=str(sport)):
                _rule("x", sport=sport)

    def test_protocol_and_action_validation(self):
        with self.assertRaises(RuleValidationError):
            _rule("x", protocol="ICMP")
        with self.assertRaises(RuleValidationError):
            _rule("x", action="LOG")

    def test_duplicate_rule_id_rejected(self):
        with self.assertRaises(RuleValidationError):
            RuleSet((_rule("a"), _rule("a")), "DENY", "z")

    def test_first_match_not_longest_prefix(self):
        rules = (
            _rule("wide", src=Prefix.parse("10.0.0.0/8"), dport=(80, 80), action="DENY"),
            _rule(
                "narrow",
                src=Prefix.parse("10.1.2.0/24"),
                dport=(80, 80),
                action="ACCEPT",
            ),
        )
        rs = RuleSet(rules, "ACCEPT", "default")
        rid, action, category, idx = rs.classify(
            parse_ipv4("10.1.2.3"), 0, "TCP", 500, 80
        )
        self.assertEqual((rid, action, idx), ("wide", "DENY", 0))

    def test_last_match_not_used(self):
        rules = (
            _rule("first", dport=(80, 80), action="DENY"),
            _rule("second", dport=(80, 80), action="ACCEPT"),
        )
        rs = RuleSet(rules, "ACCEPT", "default")
        self.assertEqual(rs.classify(parse_ipv4("10.0.0.1"), 1, "TCP", 0, 80)[0], "first")


class RulesetProductTests(unittest.TestCase):
    def test_digest_is_order_sensitive_and_stable(self):
        a = _rule("a")
        b = _rule("b", dport=(443, 443))
        rs1 = RuleSet((a, b), "ACCEPT", "default")
        rs2 = RuleSet((b, a), "ACCEPT", "default")
        self.assertNotEqual(rs1.digest, rs2.digest)
        self.assertEqual(rs1.digest, RuleSet((a, b), "ACCEPT", "default").digest)

    def test_reorder_validates_permutation(self):
        rs = RuleSet((_rule("a"), _rule("b")), "ACCEPT", "default")
        with self.assertRaises(RuleValidationError):
            reorder(rs, ["a", "a"])
        with self.assertRaises(RuleValidationError):
            reorder(rs, ["a"])
        self.assertEqual([r.rule_id for r in reorder(rs, ["b", "a"]).rules], ["b", "a"])

    def test_save_load(self):
        import tempfile, os

        rs = RuleSet((_rule("a"),), "DENY", "default", name="t")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            digest = save_ruleset(rs, path)
            loaded = load_ruleset(path)
            self.assertEqual(loaded, rs)
            self.assertEqual(loaded.digest, digest)

    def test_missing_fields_rejected(self):
        with self.assertRaises(RuleValidationError):
            RuleSet.from_dict({"rules": [], "default": {"action": "ACCEPT"}})


if __name__ == "__main__":
    unittest.main()
