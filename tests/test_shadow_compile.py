"""遮蔽分析与编译决策单元的测试（含联合覆盖与 LPM 反例）。"""

import unittest

from netobs.compile import compile_rules, equivalence_report
from netobs.generate import build_ruleset, reorder_lpm_variant
from netobs.reference import reference_classify
from netobs.rules import Prefix, Rule, RuleSet
from netobs.shadow import RuleCatalog
from netobs.index import RuleIndex


def _r(rid, src, dst, proto, sport, dport, action="ACCEPT", category="c"):
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


class UnionShadowTests(unittest.TestCase):
    def test_two_prior_rules_jointly_shadow(self):
        rs = RuleSet(
            (
                _r("A", "10.0.0.0/8", "0.0.0.0/0", "TCP", (0, 1023), (80, 80)),
                _r("B", "10.0.0.0/8", "0.0.0.0/0", "TCP", (1024, 65535), (80, 80)),
                _r("C", "10.0.0.0/8", "0.0.0.0/0", "TCP", (0, 65535), (80, 80), "DENY"),
            ),
            "ACCEPT",
            "default",
        )
        catalog = RuleCatalog(rs)
        verdict = catalog.analyze("C")
        self.assertTrue(verdict.fully_shadowed)
        self.assertEqual(len(verdict.cover), 2)
        self.assertTrue(catalog.verify_verdict(verdict)["verified"])
        # 单条不包含：移除任一条后 C 不再完全遮蔽
        for name in ("A", "B"):
            reduced = RuleSet(
                tuple(rule for rule in rs.rules if rule.rule_id != name), "ACCEPT", "default"
            )
            self.assertFalse(RuleCatalog(reduced).analyze("C").fully_shadowed)

    def test_staggered_source_destination_union(self):
        rs = RuleSet(
            (
                _r("A", "10.0.0.0/16", "172.16.0.0/16", "TCP", (0, 65535), (25, 25)),
                _r("B", "10.1.0.0/16", "172.17.0.0/16", "TCP", (0, 65535), (25, 25)),
                _r("C", "10.0.0.0/16", "172.17.0.0/16", "TCP", (0, 65535), (25, 25)),
                _r("D", "10.1.0.0/16", "172.16.0.0/16", "TCP", (0, 65535), (25, 25)),
                _r("E", "10.0.0.0/15", "172.16.0.0/15", "TCP", (0, 65535), (25, 25), "DENY"),
            ),
            "ACCEPT",
            "default",
        )
        catalog = RuleCatalog(rs)
        verdict = catalog.analyze("E")
        self.assertTrue(verdict.fully_shadowed)
        self.assertTrue(catalog.verify_verdict(verdict)["verified"])

    def test_protocol_separation_shadow(self):
        rs = RuleSet(
            (
                _r("tcp", "10.0.0.0/8", "0.0.0.0/0", "TCP", (0, 65535), (0, 65535)),
                _r("udp", "10.0.0.0/8", "0.0.0.0/0", "UDP", (0, 65535), (0, 65535)),
                _r("any", "10.0.0.0/8", "0.0.0.0/0", "ANY", (0, 65535), (0, 65535)),
            ),
            "ACCEPT",
            "default",
        )
        catalog = RuleCatalog(rs)
        self.assertTrue(catalog.analyze("any").fully_shadowed)
        self.assertTrue(catalog.verify_verdict(catalog.analyze("any"))["verified"])

    def test_not_shadowed_returns_valid_counterexample(self):
        rs = RuleSet(
            (
                _r("A", "10.0.0.0/8", "0.0.0.0/0", "TCP", (0, 1023), (80, 80)),
                _r("B", "10.0.0.0/8", "0.0.0.0/0", "TCP", (1024, 65535), (80, 80)),
                _r("C", "10.0.0.0/8", "0.0.0.0/0", "TCP", (0, 65535), (80, 81), "DENY"),
            ),
            "ACCEPT",
            "default",
        )
        catalog = RuleCatalog(rs)
        verdict = catalog.analyze("C")
        self.assertFalse(verdict.fully_shadowed)
        self.assertIsNotNone(verdict.counterexample)
        src, dst, proto, sport, dport = verdict.counterexample
        self.assertTrue(rs.rules[2].matches(src, dst, proto, sport, dport))
        # 反例不被前序任何规则命中
        for rule in rs.rules[:2]:
            self.assertFalse(rule.matches(src, dst, proto, sport, dport))
        self.assertTrue(catalog.verify_verdict(verdict)["verified"])

    def test_shadow_verification_detects_bad_evidence(self):
        """把覆盖证据换成空片，复核必须失败。"""
        rs = build_ruleset()
        catalog = RuleCatalog(rs)
        verdict = catalog.analyze("U_C")
        self.assertTrue(verdict.fully_shadowed)
        broken = type(verdict)(
            rule_id=verdict.rule_id,
            index=verdict.index,
            fully_shadowed=True,
            cover=(),
        )
        self.assertFalse(catalog.verify_verdict(broken)["verified"])

    def test_port_boundary_case(self):
        # B4 只被 99 / 301 两个端点留下反例，其余都被 100..300 覆盖
        rs = build_ruleset()
        catalog = RuleCatalog(rs)
        verdict = catalog.analyze("B4")
        self.assertFalse(verdict.fully_shadowed)
        self.assertTrue(catalog.verify_verdict(verdict)["verified"])
        # 反例的目端口必须是边界之一
        self.assertIn(verdict.counterexample[4], (99, 301))

    def test_single_rule_containment_evidence(self):
        rs = build_ruleset()
        catalog = RuleCatalog(rs)
        verdict = catalog.analyze("N3")
        self.assertTrue(verdict.fully_shadowed)
        self.assertEqual(len(verdict.cover), 1)
        # 最早覆盖者 N1（10.2.0.0/16 × 172.16.0.0/16 × TCP 8080）已包含 N3
        self.assertEqual(verdict.cover[0][0], "N1")
        self.assertEqual(str(rs.rules[verdict.index].rule_id), "N3")


class CompilationTests(unittest.TestCase):
    def test_compiled_equivalent_on_synthetic_base(self):
        rs = build_ruleset()
        compiled = compile_rules(rs)
        report = equivalence_report(rs, compiled)
        self.assertTrue(report["equivalent"], report["issues"])

    def test_cells_are_disjoint_and_cover_default(self):
        rs = build_ruleset()
        compiled = compile_rules(rs)
        # 所有单元两两不交（小规模抽检前 60 个）
        cells = compiled.cells[:60]
        for i in range(len(cells)):
            for j in range(i + 1, len(cells)):
                self.assertIsNone(cells[i].region.intersection(cells[j].region))
        # 默认单元覆盖一个确定不属于任何规则的取反
        cell = compiled.classify(0xFFFFFFFF, 0xFFFFFFFF, "TCP", 1, 1)
        self.assertEqual(cell.rule_id, "DEFAULT")

    def test_compiled_matches_first_match_on_boundaries(self):
        rs = build_ruleset()
        compiled = compile_rules(rs)
        points = [
            (0x0A010203, 0xAC100001, "TCP", 1023, 80),
            (0x0A010203, 0xAC100001, "TCP", 1024, 80),
            (0x0A050501, 0xAC130001, "TCP", 0, 443),
            (0xCB007107, 0xC6336407, "TCP", 0, 443),
            (0xF0010203, 0x08080808, "UDP", 0, 5353),
            (0x00000000, 0xE0000001, "UDP", 0, 1900),
        ]
        for point in points:
            cell = compiled.classify(*point)
            expected = reference_classify(rs, *point)
            self.assertEqual((cell.rule_id, cell.action, cell.category), expected[:3])

    def test_resource_refusal_is_explicit(self):
        from netobs.compile import CompilationResourceError

        rs = build_ruleset()
        with self.assertRaises(CompilationResourceError):
            compile_rules(rs, max_cells=3)

    def test_equivalence_report_detects_tampering(self):
        """把某单元错误地归给别的规则，报告必须报错（证明检查不是空转）。"""
        import dataclasses

        rs = build_ruleset()
        compiled = compile_rules(rs)
        self.assertTrue(equivalence_report(rs, compiled)["equivalent"])
        tampered_cells = list(compiled.cells)
        victim = next(c for c in tampered_cells if c.rule_id not in ("DEFAULT", "U_A"))
        position = tampered_cells.index(victim)
        tampered_cells[position] = dataclasses.replace(
            victim, rule_id="U_A", action="ACCEPT", category="web"
        )
        tampered = dataclasses.replace(compiled, cells=tuple(tampered_cells))
        report = equivalence_report(rs, tampered)
        self.assertFalse(report["equivalent"])
        self.assertTrue(report["issues"])


class ReorderTests(unittest.TestCase):
    def test_reorder_changes_hit_only(self):
        base = build_ruleset()
        variant = reorder_lpm_variant(base)
        point = (0x0A050501, 0xAC130001, "TCP", 1000, 443)  # 10.5.5.1 -> 172.19.0.1:443
        _, action_base, _, _ = base.classify(*point)
        _, action_variant, _, _ = variant.classify(*point)
        self.assertEqual(action_base, "DENY")
        self.assertEqual(action_variant, "ACCEPT")
        # 规则集合本身没变
        self.assertEqual({r.rule_id for r in base.rules}, {r.rule_id for r in variant.rules})
        self.assertNotEqual(base.digest, variant.digest)

    def test_index_matches_reference(self):
        rs = build_ruleset()
        index = RuleIndex(rs)
        points = [
            (0x0A010203, 0xAC100001, "TCP", 0, 80),
            (0x0A050501, 0xAC130001, "TCP", 0, 443),
            (0x0A030005, 0xAC110005, "UDP", 6000, 53),
            (0xF0010203, 0x08080808, "UDP", 0, 5353),
        ]
        for point in points:
            self.assertEqual(index.classify(*point), reference_classify(rs, *point))


if __name__ == "__main__":
    unittest.main()
