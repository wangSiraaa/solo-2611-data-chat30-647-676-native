"""规则解释服务的语义单元测试（不接触文件系统）。"""

import unittest

from ignarc.engine import RuleEngine
from ignarc.paths import DIR, FILE, LINK, InvalidPath


def engine(texts: dict[str, str]) -> RuleEngine:
    return RuleEngine(texts)


class TestBasics(unittest.TestCase):
    def test_default_include(self):
        e = engine({})
        d = e.check("anything/here.txt", FILE)
        self.assertTrue(d.included)
        self.assertIsNone(d.source_line)
        self.assertEqual(d.reason, "default-include (no rule matched)")

    def test_simple_exclude_include(self):
        e = engine({".ignore": "*.tmp\n!keep.tmp\n"})
        self.assertFalse(e.check("a.tmp", FILE).included)
        self.assertTrue(e.check("keep.tmp", FILE).included)
        self.assertEqual(e.check("keep.tmp", FILE).source_line, ".ignore:2")

    def test_last_match_wins_same_file(self):
        e = engine({".ignore": "x\n!x\nx\n"})
        d = e.check("x", FILE)
        self.assertFalse(d.included)
        self.assertEqual(d.source_line, ".ignore:3")

    def test_comments_and_blank_lines_keep_line_numbers(self):
        e = engine({".ignore": "# c\n\nx\n"})
        d = e.check("x", FILE)
        self.assertEqual(d.effective_rule.line_no, 3)


class TestScope(unittest.TestCase):
    def test_basename_rule_inherits_to_descendants(self):
        e = engine({".ignore": "*.log\n"})
        self.assertFalse(e.check("a.log", FILE).included)
        self.assertFalse(e.check("d/a.log", FILE).included)
        self.assertFalse(e.check("d/e/f/a.log", FILE).included)

    def test_child_rule_overrides_ancestor(self):
        e = engine({
            ".ignore": "*.log\n",
            "sub/.ignore": "!*.log\n",
        })
        self.assertFalse(e.check("a.log", FILE).included)
        self.assertTrue(e.check("sub/a.log", FILE).included)
        # 子目录规则不影响兄弟目录
        self.assertFalse(e.check("other/a.log", FILE).included)
        self.assertEqual(e.check("sub/a.log", FILE).source_line, "sub/.ignore:1")

    def test_anchored_pattern_relative_to_scope(self):
        e = engine({
            ".ignore": "/top.txt\n",
            "sub/.ignore": "deep/x.dat\n",
        })
        self.assertFalse(e.check("top.txt", FILE).included)
        self.assertTrue(e.check("sub/top.txt", FILE).included)
        self.assertFalse(e.check("sub/deep/x.dat", FILE).included)
        self.assertTrue(e.check("sub/deep/y.dat", FILE).included)

    def test_rule_does_not_constrain_its_own_directory(self):
        e = engine({"sub/.ignore": "sub/\n"})  # 作用域内没有 "sub" 分量
        self.assertTrue(e.check("sub", DIR).included)

    def test_dir_only_pattern_skips_files_and_links(self):
        e = engine({".ignore": "data/\n"})
        self.assertFalse(e.check("data", DIR).included)
        # 同名文件与符号链接不受“仅目录”规则影响
        self.assertTrue(e.check("data", FILE).included)
        self.assertTrue(e.check("data", LINK).included)


class TestPruning(unittest.TestCase):
    def setUp(self):
        self.e = engine({
            ".ignore": "locked/\n",
            "locked/.ignore": "!inside.log\n!deep/inside.log\n",
        })

    def test_excluded_parent_blocks_descendants(self):
        d = self.e.check("locked/inside.log", FILE)
        self.assertFalse(d.included)
        self.assertEqual(d.reason,
                         "ancestor directory excluded; subtree not entered")
        self.assertEqual(d.blocking_rule.source, ".ignore:1")

    def test_inner_rule_cannot_revive(self):
        d = self.e.check("locked/deep/inside.log", FILE)
        self.assertFalse(d.included)
        self.assertIsNotNone(d.blocking_rule)

    def test_reinclude_requires_enterable_parent(self):
        # 即便文件级 include 命中，祖先被剪枝仍以祖先结论为准
        d = self.e.check("locked/inside.log", FILE)
        self.assertFalse(d.included)

    def test_sibling_subtree_unaffected(self):
        self.assertTrue(self.e.check("open/inside.log", FILE).included)


class TestStarStarSemantics(unittest.TestCase):
    def setUp(self):
        self.e = engine({".ignore": (
            "**/secret.log\n"
            "logs/**/debug.txt\n"
            "**/*.tmp\n"
            "!logs/deep/keep/debug.txt\n"
        )})

    def test_zero_and_multi_level(self):
        self.assertFalse(self.e.check("secret.log", FILE).included)
        self.assertFalse(self.e.check("a/b/secret.log", FILE).included)
        self.assertFalse(self.e.check("logs/debug.txt", FILE).included)
        self.assertFalse(self.e.check("logs/a/b/debug.txt", FILE).included)

    def test_reinclude_deep(self):
        d = self.e.check("logs/deep/keep/debug.txt", FILE)
        self.assertTrue(d.included)
        self.assertEqual(d.source_line, ".ignore:4")

    def test_tmp_anywhere(self):
        for p in ("x.tmp", "a/b/y.tmp", "a/b/c/z.tmp"):
            self.assertFalse(self.e.check(p, FILE).included, p)


class TestPathValidation(unittest.TestCase):
    def test_forbidden_paths(self):
        e = engine({})
        for bad in ("/abs", "a/../b", "a//b", "a/b/", "..", "a/./b", "a\x00b"):
            with self.assertRaises(InvalidPath):
                e.check(bad, FILE)

    def test_unknown_kind(self):
        with self.assertRaises(InvalidPath):
            engine({}).check("a", "socket")

    def test_backslash_is_literal_filename(self):
        e = engine({".ignore": r"a\b\n"})
        self.assertFalse(e.check(r"a\b\n", FILE).included)
        self.assertTrue(e.check("a/b/n", FILE).included)


class TestUnicode(unittest.TestCase):
    def test_codepoint_exact(self):
        e = engine({".ignore": "目录甲/秘密*.txt\n!目录甲/秘密公开.txt\n"})
        self.assertFalse(e.check("目录甲/秘密计划.txt", FILE).included)
        d = e.check("目录甲/秘密公开.txt", FILE)
        self.assertTrue(d.included)
        self.assertEqual(d.source_line, ".ignore:2")


class TestChainOrdering(unittest.TestCase):
    def test_ancestor_then_local_last_match_wins(self):
        e = engine({
            ".ignore": "x\n!x\n",          # 祖先：先排后放 → 最终放
            "a/.ignore": "x\n",            # 本地更浅规则之后 → 再次排除
            "a/b/.ignore": "!x\n",         # 更深本地规则 → 最终放
        })
        # 根级路径只受根规则影响
        self.assertTrue(e.check("x", FILE).included)
        # a/ 下：本地规则在祖先之后，覆盖祖先的 include
        self.assertFalse(e.check("a/x", FILE).included)
        # a/b/ 下：更深的 include 最后命中
        self.assertTrue(e.check("a/b/x", FILE).included)
        self.assertEqual(e.check("a/b/x", FILE).source_line, "a/b/.ignore:1")

    def test_chain_caching_returns_stable_order(self):
        e = engine({
            ".ignore": "*.log\n",
            "a/.ignore": "!*.log\n",
            "a/b/.ignore": "*.log\n!keep.log\n",
        })
        first = e.check("a/b/keep.log", FILE)
        second = e.check("a/b/keep.log", FILE)  # 走缓存
        self.assertTrue(first.included)
        self.assertEqual(first.source_line, second.source_line)
        self.assertFalse(e.check("a/b/other.log", FILE).included)


class TestCachingAndDigest(unittest.TestCase):
    def test_digest_stable_and_changes(self):
        texts = {".ignore": "x\n"}
        e = engine(texts)
        d1 = e.rule_digest
        e2 = engine(dict(texts))
        self.assertEqual(d1, e2.rule_digest)
        e3 = engine({".ignore": "x\n", "a/.ignore": "y\n"})
        self.assertNotEqual(d1, e3.rule_digest)

    def test_decision_carries_digest(self):
        e = engine({".ignore": "x\n"})
        d = e.check("x", FILE)
        self.assertEqual(d.rule_digest, e.rule_digest)

    def test_check_independent_of_body_reads(self):
        # 无文件系统参与即可反复回答
        e = engine({".ignore": "*.bin\n"})
        for _ in range(3):
            self.assertFalse(e.check("a/b/c.bin", FILE).included)


if __name__ == "__main__":
    unittest.main()
