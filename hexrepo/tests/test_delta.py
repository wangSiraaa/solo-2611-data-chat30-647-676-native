"""差量分析：参考模型逐字节核对、两种视图、边界与目录修订绑定。"""

import unittest

from heximg.delta import (
    KIND_ADDED,
    KIND_CHANGED,
    KIND_REMOVED,
    KIND_UNCHANGED,
    VIEW_MATERIALIZED,
    VIEW_UNKNOWN,
    assert_same_view,
    compare_images,
)
from heximg.errors import PartitionError, ViewMismatch
from heximg.image import SparseImage
from heximg.partition import Partition, PartitionDirectory
from heximg.synth import data_record, eof_record
from tests.helpers import (
    ReferenceModel,
    expected_byte_classes,
    make_random_scenario,
)


def image_from(lines, name, *, source=None):
    from heximg.records import parse_hex
    text = "\n".join(lines) + "\n"
    return SparseImage.from_writes(
        name, parse_hex(text, source=source or name).writes)


def expand_runs(partition_delta):
    """把最大连续段展开为 addr -> (kind, a_hole, b_hole)。"""
    out = {}
    for run in partition_delta.runs:
        for addr in range(run.start, run.end):
            out[addr] = (run.kind, run.a_hole, run.b_hole)
    return out


def assert_runs_maximal(testcase, partition_delta):
    """相邻段的 (kind, flags) 必须不同，否则说明没有取最大连续段。

    段之间可能隔着双空洞（unknown 视图不为双空洞产段），因此只在
    无缝相邻时检查状态差异。
    """
    runs = partition_delta.runs
    for prev, cur in zip(runs, runs[1:]):
        if prev.end == cur.start:
            testcase.assertNotEqual(
                (cur.kind, cur.a_hole, cur.b_hole),
                (prev.kind, prev.a_hole, prev.b_hole),
                "相邻同类段必须合并为最大连续段",
            )
        else:
            testcase.assertGreater(cur.start, prev.end, "段不得交叠")


class DeltaReferenceTests(unittest.TestCase):
    def _run_pair_for_scenario(self, seed, view):
        # 重试找到无同层冲突的场景；冲突路径已由专门用例覆盖
        scn = make_random_scenario(seed)
        attempt = 0
        while scn.conflict_addresses and attempt < 50:
            attempt += 1
            scn = make_random_scenario(seed * 1000 + attempt)
        from heximg.catalog import build_product
        from heximg.catalog import Manifest, ManifestEntry

        # 用同一批文件造两个不同产品：A 取偶数文件，B 取全部文件的改写
        files_a = scn.files[::2]
        layers_a = scn.layers[::2]
        img_a = build_product(Manifest(
            tuple(ManifestEntry(f, l) for f, l in zip(files_a, layers_a)),
            name="A"))
        img_b = build_product(Manifest(
            tuple(ManifestEntry(f, l)
                  for f, l in zip(scn.files, scn.layers)),
            name="B"))

        # 参考模型重建 A
        from heximg.records import parse_hex_file
        ref_a = ReferenceModel()
        layer_buckets = {}
        for f, l in zip(files_a, layers_a):
            layer_buckets.setdefault(l, []).extend(
                parse_hex_file(f).writes)
        for l in sorted(layer_buckets):
            rm = ReferenceModel()
            rm.apply_layer_writes(layer_buckets[l])
            ref_a.overlay(rm)

        directory = PartitionDirectory(scn.partitions, revision="r1")
        if view == "materialized":
            # 物化视图要求每个分区都有填充策略：用确定性值补齐
            patched = [
                Partition(p.name, p.start, p.length,
                          fill=p.fill if p.fill is not None else 0x00)
                for p in scn.partitions
            ]
            directory = PartitionDirectory(patched, revision="r1-mat")
        report = compare_images(img_a, img_b, directory, view=view)
        return report, ref_a, scn.ref, directory.partitions

    def test_unknown_view_matches_reference_byte_by_byte(self):
        for seed in range(20):
            report, ref_a, ref_b, parts = self._run_pair_for_scenario(
                seed, VIEW_UNKNOWN)
            with self.subTest(seed=seed):
                for pd, part in zip(report.partitions, parts):
                    expected, flags, hole_stats = expected_byte_classes(
                        ref_a, ref_b, part, VIEW_UNKNOWN)
                    got = expand_runs(pd)
                    # unknown 视图：双空洞地址不出现在任何段中
                    for addr, (kind, ah, bh) in got.items():
                        self.assertIn(addr, expected)
                        self.assertEqual(kind, expected[addr])
                        self.assertEqual((ah, bh), flags[addr])
                    for addr, cls in expected.items():
                        if flags[addr] == (True, True):
                            self.assertNotIn(addr, got)
                        else:
                            self.assertIn(addr, got)
                    self.assertEqual(pd.hole_stats.both_hole,
                                     hole_stats["both_hole"])
                    self.assertEqual(pd.hole_stats.hole_to_defined,
                                     hole_stats["hole_to_defined"])
                    self.assertEqual(pd.hole_stats.defined_to_hole,
                                     hole_stats["defined_to_hole"])
                    assert_runs_maximal(self, pd)
                    # 计数与段长度一致
                    for kind in (KIND_ADDED, KIND_REMOVED, KIND_CHANGED,
                                 KIND_UNCHANGED):
                        self.assertEqual(
                            pd.counts[kind],
                            sum(r.length for r in pd.runs
                                if r.kind == kind))

    def test_materialized_view_matches_reference(self):
        for seed in range(20):
            report, ref_a, ref_b, parts = self._run_pair_for_scenario(
                seed, VIEW_MATERIALIZED)
            with self.subTest(seed=seed):
                for pd, part in zip(report.partitions, parts):
                    if part.fill is None:
                        continue
                    expected, flags, _ = expected_byte_classes(
                        ref_a, ref_b, part, VIEW_MATERIALIZED)
                    got = expand_runs(pd)
                    self.assertEqual(set(got), set(expected))
                    for addr, (kind, ah, bh) in got.items():
                        self.assertEqual(kind, expected[addr])
                        self.assertEqual((ah, bh), flags[addr])
                    assert_runs_maximal(self, pd)
                    # 物化读取统计：所有参与比较的空洞字节都被计数
                    mat_total = (pd.hole_stats.materialized_reads_a
                                 + pd.hole_stats.materialized_reads_b)
                    self.assertGreaterEqual(mat_total,
                                            pd.hole_stats.both_hole)

    def test_materialized_requires_fill_policy(self):
        img = image_from([eof_record()], "x")
        directory = PartitionDirectory(
            [Partition("p", 0, 16, fill=None)], revision="r1")
        with self.assertRaises(PartitionError):
            compare_images(img, img, directory, view=VIEW_MATERIALIZED)

    def test_views_must_not_be_mixed(self):
        img = image_from([eof_record()], "x")
        d = PartitionDirectory([Partition("p", 0, 16, fill=0)], "r1")
        r1 = compare_images(img, img, d, view=VIEW_UNKNOWN)
        r2 = compare_images(img, img, d, view=VIEW_MATERIALIZED)
        with self.assertRaises(ViewMismatch):
            assert_same_view(iter([r1, r2]))
        assert_same_view(iter([r1, r1]))  # 同视图不报错


class DeltaClassificationTests(unittest.TestCase):
    def test_added_removed_changed_unchanged_all_present(self):
        # A: 0..3 = 10 20 30 40
        img_a = image_from([data_record(0, bytes([10, 20, 30, 40])),
                            eof_record()], "A")
        # B: 1..4 = 20 99 40 50（1 不变、2 变化、3 不变撤去? 逐地址看）
        img_b = image_from([data_record(1, bytes([20, 99, 40, 50])),
                            eof_record()], "B")
        d = PartitionDirectory([Partition("p", 0, 8, fill=0x00)], "r1")
        rep = compare_images(img_a, img_b, d, view=VIEW_UNKNOWN)
        pd = rep.partitions[0]
        got = {a: k for a, (k, _, _) in expand_runs(pd).items()}
        self.assertEqual(got[0], KIND_REMOVED)
        self.assertEqual(got[1], KIND_UNCHANGED)
        self.assertEqual(got[2], KIND_CHANGED)
        self.assertEqual(got[3], KIND_UNCHANGED)
        self.assertEqual(got[4], KIND_ADDED)
        # 5..7 双空洞：unknown 视图无段
        self.assertNotIn(5, got)
        self.assertEqual(pd.hole_stats.both_hole, 3)
        self.assertEqual(pd.hole_stats.defined_to_hole, 1)
        self.assertEqual(pd.hole_stats.hole_to_defined, 1)

    def test_materialized_double_hole_is_unchanged_run(self):
        img_a = image_from([data_record(0, b"\x01"), eof_record()], "A")
        img_b = image_from([data_record(0, b"\x01"), eof_record()], "B")
        d = PartitionDirectory([Partition("p", 0, 8, fill=0xAB)], "r1")
        rep = compare_images(img_a, img_b, d, view=VIEW_MATERIALIZED)
        pd = rep.partitions[0]
        got = expand_runs(pd)
        self.assertEqual(got[0], (KIND_UNCHANGED, False, False))
        self.assertEqual(got[1], (KIND_UNCHANGED, True, True))
        self.assertEqual(pd.hole_stats.materialized_reads_a, 7)
        self.assertEqual(pd.hole_stats.materialized_reads_b, 7)

    def test_zero_defined_vs_hole_distinguished_in_both_views(self):
        img_a = image_from([data_record(0, b"\x00\x00"), eof_record()], "A")
        img_b = image_from([data_record(2, b"\x00"), eof_record()], "B")
        d = PartitionDirectory([Partition("p", 0, 4, fill=0x00)], "r1")
        # 即使填充值恰好是 0x00，定义状态变化仍必须是 removed/added
        rep = compare_images(img_a, img_b, d, view=VIEW_MATERIALIZED)
        got = {a: k for a, (k, _, _) in expand_runs(rep.partitions[0]).items()}
        self.assertEqual(got[0], KIND_REMOVED)
        self.assertEqual(got[1], KIND_REMOVED)
        self.assertEqual(got[2], KIND_ADDED)


class PagingTests(unittest.TestCase):
    def test_partition_spanning_multiple_pages(self):
        # 分区 400 KiB，跨过 256 KiB 分页边界；两侧在页界附近变化
        payload_a = bytes((i * 7) & 0xFF for i in range(400 * 1024))
        payload_b = bytearray(payload_a)
        for off in (0, 256 * 1024 - 1, 256 * 1024, 400 * 1024 - 1):
            payload_b[off] ^= 0x5A
        img_a = SparseImage.from_writes(
            "A", _writes(0, bytes(payload_a)))
        img_b = SparseImage.from_writes(
            "B", _writes(0, bytes(payload_b)))
        d = PartitionDirectory(
            [Partition("big", 0, 400 * 1024, fill=0xFF)], "r1")
        rep = compare_images(img_a, img_b, d, view=VIEW_UNKNOWN)
        pd = rep.partitions[0]
        self.assertEqual(pd.counts[KIND_CHANGED], 4)
        self.assertEqual(pd.counts[KIND_UNCHANGED],
                         400 * 1024 - 4)
        assert_runs_maximal(self, pd)


def _writes(addr, data, chunk=4096):
    from heximg.image import Source
    from heximg.records import DataWrite
    src = Source("mem", 1, (0, 0))
    out = []
    for i in range(0, len(data), chunk):
        out.append(DataWrite(addr + i, data[i:i + chunk], src, 1, (0, 0)))
    return out


class PartitionRevisionTests(unittest.TestCase):
    def test_partitions_must_not_overlap(self):
        with self.assertRaises(PartitionError):
            PartitionDirectory([
                Partition("a", 0, 100),
                Partition("b", 50, 100),
            ], revision="r1")

    def test_invalid_partition_rejected(self):
        with self.assertRaises(PartitionError):
            Partition("bad", 0, 0)
        with self.assertRaises(PartitionError):
            Partition("bad", 0xFFFFFFF0, 32)
        with self.assertRaises(PartitionError):
            Partition("bad", 0, 4, fill=256)

    def test_revision_changes_attribution_old_report_stays_bound(self):
        img_a = image_from([data_record(0x100, b"\x01\x02"), eof_record()],
                           "A")
        img_b = image_from([data_record(0x100, b"\x01\x09"), eof_record()],
                           "B")
        dir1 = PartitionDirectory(
            [Partition("low", 0, 0x200, fill=0)], "r1")
        dir2 = dir1.revised([
            Partition("low", 0, 0x100, fill=0),
            Partition("high", 0x100, 0x100, fill=0),
        ], revision="r2")

        rep1 = compare_images(img_a, img_b, dir1, view=VIEW_UNKNOWN)
        rep2 = compare_images(img_a, img_b, dir2, view=VIEW_UNKNOWN)

        self.assertEqual(rep1.directory_revision, "r1")
        self.assertEqual(rep2.directory_revision, "r2")
        self.assertNotEqual(rep1.directory_digest, rep2.directory_digest)
        # r1：变化落在单个分区；r2：变化归属 high 分区
        self.assertEqual(len(rep1.partitions), 1)
        self.assertEqual(len(rep2.partitions), 2)
        changed_r2 = [r for p in rep2.partitions for r in p.runs
                      if r.kind == KIND_CHANGED]
        self.assertTrue(all(r.partition == "high" for r in changed_r2))
        # 旧报告内嵌旧目录快照，不随后续修订而变
        self.assertEqual(len(rep1.directory_snapshot), 1)
        self.assertEqual(rep1.directory_snapshot[0]["name"], "low")

    def test_revised_rejects_same_revision_id(self):
        d1 = PartitionDirectory([Partition("p", 0, 8)], "r1")
        with self.assertRaises(PartitionError):
            d1.revised([Partition("p", 0, 16)], revision="r1")


if __name__ == "__main__":
    unittest.main()
