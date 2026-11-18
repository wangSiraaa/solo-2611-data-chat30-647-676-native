"""边界场景：空树、无规则树、剪枝目录内规则文件的处置。"""

import os
import shutil
import tempfile
import unittest

from ignarc.archiver import collect, verify_package
from ignarc.engine import RuleEngine
from ignarc.fs import AuditedFS
from ignarc.planner import build_plan


class TestEdgeTrees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ignarc-edge-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_tree(self):
        root = os.path.join(self.tmp, "empty")
        os.makedirs(root)
        fs = AuditedFS(root)
        eng = RuleEngine.from_fs(fs)
        plan = build_plan(fs, eng)
        self.assertEqual(plan.stats["total_seen"], 0)
        result = collect(AuditedFS(root), RuleEngine.from_fs(AuditedFS(root)),
                         os.path.join(self.tmp, "out1"))
        self.assertEqual(result.manifest["files"], [])
        report = verify_package(result.package_path)
        self.assertTrue(report["ok"])

    def test_tree_without_ignore_files(self):
        root = os.path.join(self.tmp, "norules")
        os.makedirs(os.path.join(root, "a/b"))
        with open(os.path.join(root, "a/b/f.txt"), "wb") as fh:
            fh.write(b"hello")
        result = collect(AuditedFS(root), RuleEngine.from_fs(AuditedFS(root)),
                         os.path.join(self.tmp, "out2"))
        self.assertEqual({e["path"] for e in result.manifest["files"]},
                         {"a/b/f.txt"})
        basis = result.manifest["files"][0]["basis"]
        self.assertIsNone(basis["source"])
        self.assertEqual(basis["decision"], "include")

    def test_ignore_inside_pruned_subtree_is_inert(self):
        # locked/ 被根规则剪枝；locked/.ignore 随包保存为控制输入，
        # 但其规则不参与任何选择，也无法复活内部文件
        root = os.path.join(self.tmp, "pruned")
        os.makedirs(os.path.join(root, "locked/deep"))
        with open(os.path.join(root, ".ignore"), "w", encoding="utf-8") as fh:
            fh.write("locked/\n")
        with open(os.path.join(root, "locked/.ignore"), "w", encoding="utf-8") as fh:
            fh.write("!secret.txt\n!deep/secret.txt\n")
        for rel in ("locked/secret.txt", "locked/deep/secret.txt", "open.txt"):
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(b"x")
        fs = AuditedFS(root)
        eng = RuleEngine.from_fs(fs)
        plan = build_plan(fs, eng)
        seen = {n.path for n in plan.selection.nodes}
        self.assertIn("locked", seen)
        self.assertNotIn("locked/secret.txt", seen)  # 子树未进入
        self.assertIn("open.txt", seen)
        result = collect(AuditedFS(root), RuleEngine.from_fs(AuditedFS(root)),
                         os.path.join(self.tmp, "out3"))
        stored_rules = {rf["path"] for rf in result.manifest["rule_files"]}
        self.assertIn("locked/.ignore", stored_rules)  # 控制输入仍单列保留
        files = {e["path"] for e in result.manifest["files"]}
        self.assertNotIn("locked/secret.txt", files)
        self.assertNotIn("locked/deep/secret.txt", files)
        self.assertIn("open.txt", files)

    def test_decision_basis_references_real_line(self):
        root = os.path.join(self.tmp, "basis")
        os.makedirs(root)
        with open(os.path.join(root, ".ignore"), "w", encoding="utf-8") as fh:
            fh.write("# head comment\n\n*.dat\n")
        with open(os.path.join(root, "x.dat"), "wb") as fh:
            fh.write(b"d")
        fs = AuditedFS(root)
        eng = RuleEngine.from_fs(fs)
        d = eng.check("x.dat", "file")
        self.assertEqual(d.effective_rule.line_no, 3)
        self.assertEqual(d.effective_rule.raw, "*.dat")


if __name__ == "__main__":
    unittest.main()
