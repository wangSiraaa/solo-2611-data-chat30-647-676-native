"""生产引擎与独立朴素解释器的选择集合交叉核对。

两套实现互不共享匹配代码；对合成树中每个可见的 (path, kind) 比较最终
include/exclude 结论与命中规则来源，再覆盖被剪枝子树中的不可见路径。
"""

import os
import shutil
import tempfile
import unittest

from ignarc.engine import RuleEngine
from ignarc.fs import AuditedFS
from ignarc.synthgen import generate_synthetic_tree
from ignarc.traversal import select

from tests.naive_interpreter import NaiveInterpreter


class CrossCheckMixin:
    def _build_tree(self, root: str) -> dict:
        return generate_synthetic_tree(root)

    def _all_physical_paths(self, root: str):
        """不走规则，枚举磁盘上的全部路径与类型（含被剪枝子树）。"""
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            for name in sorted(dirnames + filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                if os.path.islink(full):
                    kind = "link"
                elif os.path.isdir(full):
                    kind = "dir"
                else:
                    kind = "file"
                out.append((rel.replace(os.sep, "/"), kind))
        return sorted(out)

    def _assert_engines_agree(self, root: str):
        fs = AuditedFS(root)
        engine = RuleEngine.from_fs(fs)
        naive = NaiveInterpreter(dict(fs.scan_ignore_files()))

        selection = select(fs, engine)
        visible = {n.path: (n.kind, n.decision) for n in selection.nodes}

        disagreements = []
        for rel, kind in self._all_physical_paths(root):
            prod = engine.check(rel, kind)
            naive_included, naive_rule, naive_reason = naive.check(rel, kind)
            if prod.included != naive_included:
                disagreements.append(
                    f"{rel} ({kind}): engine={prod.included}/"
                    f"{prod.source_line} naive={naive_included}/"
                    f"{naive_rule.source() if naive_rule else None}"
                )
                continue
            # 都排除时，核对“起作用来源”是否一致（祖先剪枝 vs 直接命中都算）
            if not prod.included:
                prod_src = prod.source_line
                naive_src = naive_rule.source() if naive_rule else None
                if prod_src != naive_src:
                    disagreements.append(
                        f"{rel}: source mismatch engine={prod_src} naive={naive_src} "
                        f"(reasons {prod.reason} / {naive_reason})"
                    )
            if rel in visible:
                self.assertEqual(visible[rel][1].included, prod.included)

        self.assertEqual(disagreements, [])

        # 选择层与逐路径 check 的结论也要一致
        for node in selection.nodes:
            direct = engine.check(node.path, node.kind)
            self.assertEqual(direct.included, node.decision.included, node.path)
        return fs, engine, selection


class TestSmallSyntheticCrossCheck(CrossCheckMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ignarc-small-")
        self.root = os.path.join(self.tmp, "tree")
        self.info = self._build_tree(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_engines_agree_on_every_path(self):
        fs, engine, selection = self._assert_engines_agree(self.root)
        # 基本健康检查：定向案例确实产生了排除与剪枝
        self.assertGreater(len(selection.excluded), 10)
        self.assertGreater(len(selection.pruned_dirs), 0)

    def test_targeted_cases_present(self):
        fs = AuditedFS(self.root)
        engine = RuleEngine.from_fs(fs)
        # ** 零层命中
        self.assertFalse(engine.check("case_starstar/secret.log", "file").included)
        # ** 多层 + 重新包含
        self.assertTrue(
            engine.check("case_starstar/logs/deep/keep/debug.txt", "file").included
        )
        # 父目录排除，内部规则无法复活
        d = engine.check("case_override/locked/inside.log", "file")
        self.assertFalse(d.included)
        self.assertIsNotNone(d.blocking_rule)
        # 同名文件与目录：data/ 只排目录，不排同名文件
        self.assertFalse(
            engine.check("case_samename/dirholder/data", "dir").included
        )
        self.assertTrue(
            engine.check("case_samename/fileholder/data", "file").included
        )
        # 无斜杠 report 同时命中文件和目录
        self.assertFalse(
            engine.check("case_samename/fileholder/report", "file").included
        )
        self.assertFalse(
            engine.check("case_samename/dirholder/report", "dir").included
        )
        # 深度 12 的链被浅层剪枝
        deep = "case_deep/" + "/".join(f"d{i:02d}" for i in range(12))
        d = engine.check(deep + "/revive.txt", "file")
        self.assertFalse(d.included)
        self.assertEqual(d.reason,
                         "ancestor directory excluded; subtree not entered")
        # 中文
        self.assertTrue(
            engine.check("case_unicode/目录甲/秘密公开.txt", "file").included
        )
        # 正则元字符普通化
        self.assertFalse(
            engine.check("case_unicode/a+b(c) [d].txt", "file").included
        )
        # 符号链接是独立 kind，仅目录规则不命中链接
        self.assertTrue(engine.check("case_links/ok_link.txt", "link").included)


if __name__ == "__main__":
    unittest.main()
