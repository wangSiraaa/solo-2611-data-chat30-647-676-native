"""命令行端到端测试。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from heximg.cli import main
from heximg.partition import Partition, PartitionDirectory
from heximg.synth import (
    corrupt_checksum,
    data_record,
    eof_record,
    generate_all,
    write_text,
)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.samples = self.root / "hex"
        generate_all(self.samples)
        self.repo = self.root / "store"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_validate_ok_and_bad_checksum(self):
        rc, out, _ = self._run([
            "validate", str(self.samples / "linear.hex")])
        self.assertEqual(rc, 0)
        self.assertIn("[ OK ]", out)

        bad = self.root / "bad.hex"
        write_text(bad, [corrupt_checksum(data_record(0, b"\x01")),
                         eof_record()])
        rc, out, err = self._run(["validate", str(bad)])
        self.assertEqual(rc, 2)
        self.assertIn("校验和", out + err)

    def test_build_read_sources_diff_report_flow(self):
        base = self.samples / "dup_same_value.hex"
        high = self.samples / "overlay_high.hex"
        rc, out, _ = self._run([
            "build", "--file", str(base), "--layer", "0",
            "--file", str(high), "--layer", "1",
            "--name", "app", "--repo", str(self.repo),
        ])
        self.assertEqual(rc, 0, out)
        self.assertIn("已保存产品", out)

        rc, out, _ = self._run([
            "read", "--repo", str(self.repo), "--name", "app",
            "--start", "0x100", "--length", "0x10",
        ])
        self.assertEqual(rc, 0)
        self.assertIn("[定义]", out)

        rc, out, _ = self._run([
            "sources", "--repo", str(self.repo), "--name", "app",
            "--address", "0x100",
        ])
        self.assertEqual(rc, 0)
        self.assertIn("原字节范围", out)

        # 第二个产品用于差量
        rc, _, _ = self._run([
            "build", "--file", str(base), "--layer", "0",
            "--name", "base", "--repo", str(self.repo),
        ])
        self.assertEqual(rc, 0)

        d = PartitionDirectory(
            [Partition("p0", 0, 0x400, fill=0xFF)], "r1")
        dpath = self.root / "parts.json"
        d.save(dpath)

        rc, out, _ = self._run([
            "diff", "--repo", str(self.repo),
            "--a", "base", "--b", "app",
            "--dir", str(dpath), "--view", "unknown",
            "--id", "case1",
        ])
        self.assertEqual(rc, 0, out)
        self.assertIn("差量报告已保存", out)

        rc, out, _ = self._run([
            "report", "--repo", str(self.repo), "--id", "case1"])
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["directory_revision"], "r1")
        self.assertEqual(doc["view"], "unknown")

    def test_materialized_view_from_cli_and_partition_check(self):
        base = self.samples / "zero_vs_hole.hex"
        rc, _, _ = self._run([
            "build", "--file", str(base), "--name", "z",
            "--repo", str(self.repo)])
        self.assertEqual(rc, 0)
        dpath = self.root / "parts.json"
        PartitionDirectory(
            [Partition("p0", 0, 0x10, fill=0x00)], "r1").save(dpath)

        rc, out, _ = self._run([
            "partitions", "--dir", str(dpath)])
        self.assertEqual(rc, 0)
        self.assertIn("r1", out)

        rc, out, _ = self._run([
            "diff", "--repo", str(self.repo),
            "--a", "z", "--b", "z",
            "--dir", str(dpath), "--view", "materialized",
            "--id", "mat1",
        ])
        self.assertEqual(rc, 0, out)

    def test_conflict_build_fails_with_nonzero_rc(self):
        bad = self.samples / "conflict_same_layer.hex"
        rc, out, err = self._run([
            "build", "--file", str(bad), "--name", "bad",
            "--repo", str(self.repo)])
        self.assertEqual(rc, 2)
        self.assertIn("同层冲突", out + err)
        # 冲突产品不得落盘
        self.assertFalse((self.repo / "products" / "bad.heximg").exists())


if __name__ == "__main__":
    unittest.main()
