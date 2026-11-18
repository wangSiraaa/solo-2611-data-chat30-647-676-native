"""CLI 子命令冒烟测试。"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from ignarc.cli import main


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ignarc-cli-")
        self.root = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, ".ignore"), "w") as fh:
            fh.write("*.tmp\n")
        with open(os.path.join(self.root, "a.tmp"), "wb") as fh:
            fh.write(b"x")
        with open(os.path.join(self.root, "keep.txt"), "wb") as fh:
            fh.write(b"y")
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_command_lifecycle(self):
        plan_path = os.path.join(self.tmp, "plan.json")
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            code = main(["plan", "--root", self.root, "--output", plan_path,
                         "--quiet"])
        self.assertEqual(code, 0)
        plan = json.load(open(plan_path, encoding="utf-8"))
        self.assertNotIn("a.tmp", plan["included_files"])
        self.assertIn("keep.txt", plan["included_files"])

        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            code = main(["collect", "--root", self.root,
                         "--output-dir", self.out, "--quiet"])
        self.assertEqual(code, 0)
        packages = [f for f in os.listdir(self.out) if f.endswith(".pack.zip")]
        self.assertEqual(len(packages), 1)
        pkg = os.path.join(self.out, packages[0])

        with redirect_stdout(buf):
            self.assertEqual(main(["inspect", pkg]), 0)
        manifest = json.loads(buf.getvalue())
        self.assertIn("files", manifest)

        unpack = os.path.join(self.tmp, "unpack")
        with redirect_stdout(io.StringIO()):
            code = main(["verify", pkg, "--root", self.root,
                         "--extract-root", unpack])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(
            os.path.join(unpack, "payload", "keep.txt")))


if __name__ == "__main__":
    unittest.main()
