"""归档器端到端测试：plan 审计、collect 打包、inspect、verify、热更。"""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile

from ignarc.archiver import (
    AUDIT_NAME,
    MANIFEST_NAME,
    PAYLOAD_PREFIX,
    collect,
    inspect_package,
    verify_package,
)
from ignarc.engine import RuleEngine
from ignarc.fs import AuditedFS
from ignarc.planner import build_plan
from ignarc.synthgen import generate_synthetic_tree
from ignarc.traversal import select


class ArchiveTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ignarc-arc-")
        self.root = os.path.join(self.tmp, "tree")
        self.out = os.path.join(self.tmp, "out")
        self.extract = os.path.join(self.tmp, "unpack")
        generate_synthetic_tree(self.root)
        self.fs = AuditedFS(self.root)
        self.engine = RuleEngine.from_fs(self.fs)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _all_real_files(self):
        out = []
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                out.append(rel)
        return sorted(out)


class TestPlanMetadataOnly(ArchiveTestBase):
    def test_plan_never_opens_bodies(self):
        build_fs = AuditedFS(self.root)
        eng = RuleEngine.from_fs(build_fs)
        plan = build_plan(build_fs, eng)
        ops = {e["op"] for e in plan.audit_snapshot}
        self.assertNotIn("body-read", ops)
        # 规划自身的审计里只有目录/链接元数据，没有任何正文或规则文本读取
        self.assertEqual(ops, {"metadata"})
        # 规则读取发生在引擎构建阶段，同样不涉及正文
        build_ops = {e.op for e in build_fs.audit_log}
        self.assertIn("rule-read", build_ops)
        self.assertNotIn("body-read", build_ops)
        # 被剪枝目录从未被规划遍历 scandir（预扫描使用独立标签）
        scanned = {e["path"] for e in plan.audit_snapshot
                   if e["detail"] == "scandir"}
        for pruned in plan.selection.pruned_dirs:
            self.assertNotIn(pruned, scanned)
            for s in scanned:
                self.assertFalse(s == pruned or s.startswith(pruned + "/"))

    def test_plan_and_collect_share_selection(self):
        plan = build_plan(AuditedFS(self.root),
                          RuleEngine.from_fs(AuditedFS(self.root)))
        result = collect(AuditedFS(self.root),
                         RuleEngine.from_fs(AuditedFS(self.root)), self.out)
        plan_files = {n.path for n in plan.selection.included_files}
        pkg_files = {e["path"] for e in result.manifest["files"]}
        self.assertEqual(plan_files, pkg_files)


class TestCollectPackage(ArchiveTestBase):
    def test_manifest_fields_and_basis(self):
        result = collect(self.fs, self.engine, self.out)
        m = result.manifest
        self.assertEqual(m["manifest_version"], 1)
        self.assertEqual(m["rule_digest"], self.engine.rule_digest)
        for entry in m["files"]:
            self.assertIn("path", entry)
            self.assertIn("size", entry)
            self.assertIn("sha256", entry)
            basis = entry["basis"]
            self.assertIn(basis["decision"], ("include", "exclude"))
            self.assertTrue(basis["source"] is None or basis["line"] > 0)
        # 排除记录带依据
        excluded = {e["path"]: e for e in m["excluded"]}
        self.assertIn("case_override/a.log", excluded)
        self.assertEqual(excluded["case_override/a.log"]["basis"]["decision"],
                         "exclude")
        # 被剪枝的目录在清单里
        self.assertIn("case_override/locked", m["pruned_dirs"])

    def test_control_files_listed_not_exported_as_business(self):
        result = collect(self.fs, self.engine, self.out)
        business = {e["path"] for e in result.manifest["files"]}
        self.assertNotIn(".ignore", business)
        for cf in result.manifest["rule_files"]:
            self.assertTrue(cf["path"].endswith(".ignore"))
        with zipfile.ZipFile(result.package_path) as zf:
            names = zf.namelist()
            # 规则原文随包保存，但不在 payload 业务区
            self.assertTrue(any(n.startswith("rules/") for n in names))
            self.assertFalse(any(n.startswith(PAYLOAD_PREFIX)
                                 and n.endswith("/.ignore") for n in names))
            self.assertNotIn("payload/.ignore", names)
            self.assertIn(MANIFEST_NAME, names)
            self.assertIn(AUDIT_NAME, names)

    def test_excluded_files_never_opened(self):
        result = collect(self.fs, self.engine, self.out)
        excluded = {n.path for n in result.selection.excluded}
        opened = {e.relpath for e in self.fs.body_reads()}
        self.assertTrue(excluded.isdisjoint(opened))
        # 审计日志也被写进了包
        with zipfile.ZipFile(result.package_path) as zf:
            audit = json.loads(zf.read(AUDIT_NAME))
        leaked = {r["path"] for r in audit
                  if r["op"] == "body-read" and r["path"] in excluded}
        self.assertEqual(leaked, set())

    def test_payload_byte_identical_to_source(self):
        result = collect(self.fs, self.engine, self.out)
        with zipfile.ZipFile(result.package_path) as zf:
            for entry in result.manifest["files"]:
                packed = zf.read(PAYLOAD_PREFIX + entry["path"])
                source = (self.fs.root / entry["path"]).read_bytes()
                # 逐字节对照，而不是只比较文件数
                self.assertEqual(packed, source, entry["path"])
                self.assertEqual(hashlib.sha256(packed).hexdigest(),
                                 entry["sha256"])
                self.assertEqual(len(packed), entry["size"])

    def test_symlinks_store_text_never_followed(self):
        result = collect(self.fs, self.engine, self.out)
        links = {l["path"]: l for l in result.manifest["symlinks"]}
        self.assertIn("case_links/ok_link.txt", links)
        self.assertEqual(links["case_links/ok_link.txt"]["target"],
                         "inside/target.txt")
        # 根外链接的分析结果
        esc = links["case_links/escape_abs"]
        self.assertTrue(esc["link_info"]["escapes_root"])
        self.assertTrue(esc["link_info"]["is_absolute"])
        rel_esc = links["case_links/escape_rel"]
        self.assertTrue(rel_esc["link_info"]["escapes_root"])
        # 成环
        loop = links["case_links/loop_a"]
        self.assertTrue(loop["link_info"]["is_cycle"])
        with zipfile.ZipFile(result.package_path) as zf:
            stored = zf.read(PAYLOAD_PREFIX + "case_links/escape_abs").decode()
            self.assertEqual(stored, "/etc/hostname")
        # 目标正文绝不可能被采集：根外文件没有任何 payload 副本
        business = {e["path"] for e in result.manifest["files"]}
        self.assertNotIn("etc/hostname", business)
        # 被排除目录 inside/ 内的文件不入选，但指向它的链接文本仍保留
        self.assertNotIn("case_links/inside/target.txt", business)

    def test_verify_internal_and_live(self):
        result = collect(self.fs, self.engine, self.out)
        report = verify_package(
            result.package_path,
            AuditedFS(self.root), RuleEngine.from_fs(AuditedFS(self.root)),
            extract_root=self.extract,
        )
        self.assertTrue(report["ok"], report["errors"])
        # 解包字节与来源逐字节相等已在 verify 内部断言；再抽查落地文件
        sample = result.manifest["files"][0]["path"]
        unpacked = os.path.join(self.extract, PAYLOAD_PREFIX, sample)
        self.assertTrue(os.path.isfile(unpacked))
        self.assertEqual(
            open(unpacked, "rb").read(),
            (self.fs.root / sample).read_bytes(),
        )

    def test_verify_detects_tampering(self):
        result = collect(self.fs, self.engine, self.out)
        # 篡改包内某个 payload 成员
        target = result.manifest["files"][0]
        tampered = os.path.join(self.out, "tampered.zip")
        with zipfile.ZipFile(result.package_path) as zin, \
             zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == PAYLOAD_PREFIX + target["path"]:
                    data = data + b"TAMPER"
                zout.writestr(item, data)
        report = verify_package(tampered)
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["manifest_hashes"])

    def test_inspect_offline(self):
        result = collect(self.fs, self.engine, self.out)
        m = inspect_package(result.package_path)
        self.assertEqual(m["package_id"], result.package_id)
        self.assertIn("files", m)


class TestRuleRevision(ArchiveTestBase):
    def test_revision_builds_new_selection_new_package(self):
        r1 = collect(self.fs, self.engine, self.out, note="rev1")
        pkg1_files = {e["path"] for e in r1.manifest["files"]}
        pkg1_digest = r1.rule_digest

        # 修订规则：在同目录规则文件末尾追加重新包含（更浅的规则赢不了本地规则）
        target_ignore = os.path.join(self.root, "case_override", ".ignore")
        with open(target_ignore, "a", encoding="utf-8") as fh:
            fh.write("!a.log\n")

        fs2 = AuditedFS(self.root)
        eng2 = RuleEngine.from_fs(fs2)
        self.assertNotEqual(eng2.rule_digest, pkg1_digest)
        r2 = collect(fs2, eng2, self.out, note="rev2")
        pkg2_files = {e["path"] for e in r2.manifest["files"]}

        # 新选择反映修订；旧包文件保留且仍可离线 inspect
        self.assertIn("case_override/a.log", pkg2_files)
        self.assertNotIn("case_override/a.log", pkg1_files)
        self.assertTrue(os.path.exists(r1.package_path))
        old = inspect_package(r1.package_path)
        self.assertEqual(old["rule_digest"], pkg1_digest)
        self.assertNotEqual(r1.package_path, r2.package_path)

        # 用当前规则校验旧包应报告选择集合漂移，而不是静默通过
        report = verify_package(r1.package_path, fs2, eng2)
        self.assertFalse(report["ok"])
        self.assertTrue(any("selection set differs" in e for e in report["errors"]))

    def test_engine_reload_invalidates_cache(self):
        eng = RuleEngine.from_fs(AuditedFS(self.root))
        before = eng.check("case_override/a.log", "file")
        self.assertFalse(before.included)
        with open(os.path.join(self.root, "case_override", ".ignore"),
                  "a", encoding="utf-8") as fh:
            fh.write("!a.log\n")
        fs = AuditedFS(self.root)
        eng2 = RuleEngine.from_fs(fs)
        eng._fs = fs  # 让同一引擎具备 reload 能力
        new_digest = eng.reload()
        self.assertEqual(new_digest, eng2.rule_digest)
        after = eng.check("case_override/a.log", "file")
        self.assertTrue(after.included)
        # 旧 Decision 仍带着旧 digest，可识别为过期
        self.assertNotEqual(before.rule_digest, after.rule_digest)


class TestRootSafety(ArchiveTestBase):
    def test_open_body_rejects_symlink_and_escape(self):
        fs = AuditedFS(self.root)
        with self.assertRaises(Exception):
            fs.open_body("case_links/ok_link.txt")
        with self.assertRaises(Exception):
            fs.open_body("case_links/escape_abs")

    def test_outside_secret_never_readable_through_tree(self):
        # 在根外放一个“秘密”，链接试图指向它；采集产物里不得出现其内容
        secret = os.path.join(self.tmp, "outside_secret.txt")
        with open(secret, "wb") as fh:
            fh.write(b"TOP-SECRET-OUTSIDE-ROOT")
        result = collect(self.fs, self.engine, self.out)
        with zipfile.ZipFile(result.package_path) as zf:
            blob = b"".join(
                zf.read(n) for n in zf.namelist()
                if n.startswith(PAYLOAD_PREFIX)
            )
        self.assertNotIn(b"TOP-SECRET-OUTSIDE-ROOT", blob)


if __name__ == "__main__":
    unittest.main()
