"""模式翻译与规则解析的单元测试。"""

import unittest

from ignarc.patterns import (
    compile_pattern,
    segment_match,
)
from ignarc.rules import parse_ignore_text


class TestTranslate(unittest.TestCase):
    def test_question_mark(self):
        m = compile_pattern("a?c", False)
        self.assertTrue(m.matches("abc"))
        self.assertTrue(m.matches("a中c"))       # Unicode 码点也是一个字符
        self.assertFalse(m.matches("ac"))         # 不匹配零个
        self.assertFalse(m.matches("axxc"))       # 不匹配两个
        self.assertFalse(m.matches("a/c"))        # 不匹配斜杠

    def test_star(self):
        m = compile_pattern("*.log", False)
        self.assertTrue(m.matches("a.log"))
        self.assertTrue(m.matches(".log"))        # 零字符
        self.assertFalse(m.matches("dir/a.log"))  # 不跨斜杠
        self.assertTrue(m.matches("中文.log"))

    def test_literal_regex_chars(self):
        # ( ) [ ] + . \ 等全部是普通字符
        m = compile_pattern(r"a+b(c) [d].txt", False)
        self.assertTrue(m.matches(r"a+b(c) [d].txt"))
        self.assertFalse(m.matches(r"aab(c) [d].txt"))
        m2 = compile_pattern(r"a\.txt", False)
        self.assertTrue(m2.matches(r"a\.txt"))
        self.assertFalse(m2.matches("aa.txt"))

    def test_starstar_zero_level(self):
        m = compile_pattern("**/secret.log", False)
        self.assertTrue(m.matches("secret.log"))        # 零层
        self.assertTrue(m.matches("a/secret.log"))
        self.assertTrue(m.matches("a/b/c/secret.log"))
        self.assertFalse(m.matches("secret.log/x"))

    def test_starstar_middle(self):
        m = compile_pattern("logs/**/debug.txt", False)
        self.assertTrue(m.matches("logs/debug.txt"))           # 零个目录分量
        self.assertTrue(m.matches("logs/a/debug.txt"))
        self.assertTrue(m.matches("logs/a/b/debug.txt"))
        self.assertFalse(m.matches("logs/debug.txt/bak"))

    def test_bare_starstar(self):
        m = compile_pattern("**", False)
        for p in ("a", "a/b", "a/b/c/d/e", "中文/文件"):
            self.assertTrue(m.matches(p), p)

    def test_trailing_starstar(self):
        m = compile_pattern("a/**", False)
        self.assertFalse(m.matches("a"))               # 目录自身不匹配
        self.assertTrue(m.matches("a/b"))
        self.assertTrue(m.matches("a/b/c/d"))

    def test_segment_helper_independent(self):
        self.assertTrue(segment_match("a*c?d", "axxxc中d"))
        self.assertFalse(segment_match("a*c", "a/b/c"))
        self.assertTrue(segment_match("*", "anything"))
        self.assertTrue(segment_match("?", "中"))

    def test_anchored_slash_pattern(self):
        m = compile_pattern("sub/deep/x.dat", False)
        self.assertTrue(m.matches("sub/deep/x.dat"))
        self.assertFalse(m.matches("x/sub/deep/x.dat"))

    def test_star_never_crosses_slash(self):
        m = compile_pattern("a*c", False)
        self.assertTrue(m.matches("abc"))
        self.assertFalse(m.matches("a/b/c"))
        self.assertTrue(segment_match("a*c", "abc"))
        self.assertFalse(segment_match("a*c", "a/b/c"))

    def test_double_star_inside_segment_degrades_to_stars(self):
        # 分量内部的 ** 不是目录通配符，只是两个普通 *
        m = compile_pattern("a**b", False)
        self.assertTrue(m.matches("axxxb"))
        self.assertFalse(m.matches("a/x/b"))

    def test_question_mark_on_unicode_codepoints(self):
        m = compile_pattern("中?文", False)
        self.assertTrue(m.matches("中a文"))
        self.assertTrue(m.matches("中漢文"))
        self.assertFalse(m.matches("中文"))


class TestParse(unittest.TestCase):
    def test_comments_blanks_and_actions(self):
        text = "# comment\n\n   \n*.tmp\n!keep.tmp\ndata/\n/top\n"
        rules = parse_ignore_text(text, scope_dir="", source_file=".ignore")
        self.assertEqual([r.line_no for r in rules], [4, 5, 6, 7])
        self.assertEqual([r.action for r in rules],
                         ["exclude", "include", "exclude", "exclude"])
        self.assertTrue(rules[2].dir_only)
        self.assertFalse(rules[0].dir_only)
        self.assertTrue(rules[3].anchored)
        self.assertFalse(rules[0].anchored)
        # data/ 去掉尾斜杠后不含斜杠 → 不锚定，但仍是仅目录规则
        self.assertFalse(rules[2].anchored)
        self.assertTrue(rules[2].dir_only)

    def test_embedded_hash_is_literal(self):
        rules = parse_ignore_text("a#b\n", scope_dir="", source_file=".ignore")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].pattern, "a#b")

    def test_crlf(self):
        rules = parse_ignore_text("x\r\n!y\r\n", scope_dir="", source_file=".ignore")
        self.assertEqual([r.pattern for r in rules], ["x", "y"])


if __name__ == "__main__":
    unittest.main()
