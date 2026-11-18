"""合成树规模指标与性能预算测试。"""

import os
import shutil
import tempfile
import time
import unittest

from ignarc.archiver import collect, verify_package
from ignarc.engine import RuleEngine
from ignarc.fs import AuditedFS
from ignarc.planner import build_plan
from ignarc.synthgen import (
    TARGET_BYTES,
    TARGET_DEPTH,
    TARGET_IGNORE_FILES,
    TARGET_ITEMS,
    generate_synthetic_tree,
)
from ignarc.traversal import select

from tests.naive_interpreter import NaiveInterpreter


class TestScaleBudget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ignarc-scale-")
        cls.root = os.path.join(cls.tmp, "tree")
        cls.out = os.path.join(cls.tmp, "out")
        cls.info = generate_synthetic_tree(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_targets_met(self):
        self.assertGreaterEqual(self.info["items"], TARGET_ITEMS)
        self.assertGreaterEqual(self.info["ignore_files"], TARGET_IGNORE_FILES)
        self.assertGreaterEqual(self.info["bytes"], TARGET_BYTES)
        # 深度 12 的链真实存在
        deep = os.path.join(self.root, "case_deep",
                            *[f"d{i:02d}" for i in range(TARGET_DEPTH)])
        self.assertTrue(os.path.isdir(deep))
        # 每份规则可达 100 行
        line_counts = []
        for dirpath, _d, files in os.walk(self.root):
            if ".ignore" in files:
                with open(os.path.join(dirpath, ".ignore"), encoding="utf-8") as fh:
                    line_counts.append(sum(1 for _ in fh))
        self.assertGreaterEqual(max(line_counts), 90)
        self.assertGreaterEqual(len(line_counts), TARGET_IGNORE_FILES)

    def test_02_plan_metadata_only_budget(self):
        fs = AuditedFS(self.root)
        eng = RuleEngine.from_fs(fs)
        t0 = time.perf_counter()
        plan = build_plan(fs, eng)
        elapsed = time.perf_counter() - t0
        self.assertNotIn("body-read", {e["op"] for e in plan.audit_snapshot})
        self.assertLess(elapsed, 30.0, f"plan too slow: {elapsed:.1f}s")

    def test_03_collect_each_body_opened_once(self):
        fs = AuditedFS(self.root)
        eng = RuleEngine.from_fs(fs)
        t0 = time.perf_counter()
        result = collect(fs, eng, self.out)
        elapsed = time.perf_counter() - t0
        n_files = len(result.manifest["files"])
        opens = [e for e in fs.audit_log if e.op == "body-read" and e.detail == "open"]
        self.assertEqual(len(opens), n_files, "每个入选文件应且只应 open 一次")
        self.assertLess(elapsed, 60.0, f"collect too slow: {elapsed:.1f}s")
        report = verify_package(result.package_path)
        self.assertTrue(report["ok"], report["errors"])

    def test_04_naive_crosscheck_on_full_tree(self):
        fs = AuditedFS(self.root)
        texts = fs.scan_ignore_files()
        eng = RuleEngine.from_fs(fs)
        naive = NaiveInterpreter(dict(texts))
        sel = select(fs, eng)
        for node in sel.nodes:
            included, rule, _ = naive.check(node.path, node.kind)
            self.assertEqual(included, node.decision.included, node.path)


if __name__ == "__main__":
    unittest.main()
