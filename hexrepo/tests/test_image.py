"""稀疏映像：段存储、同值归并、同层冲突、显式 overlay、范围读取。"""

import unittest

from heximg.catalog import Manifest, ManifestEntry, build_product
from heximg.errors import OverlayConflict, ReadRangeError
from heximg.image import SparseImage
from heximg.records import parse_hex
from heximg.synth import (
    data_record,
    eof_record,
    extended_linear_record,
)
from tests.helpers import make_random_scenario


def writes_from_lines(lines: list[str], source: str = "t"):
    return parse_hex("\n".join(lines) + "\n", source=source).writes


class SegmentStorageTests(unittest.TestCase):
    def test_adjacent_writes_merge_hole_separates(self):
        lines = [
            data_record(0x10, b"\x01\x02"),
            data_record(0x12, b"\x03"),       # 相邻 → 合并
            data_record(0x20, b"\x04"),       # 有空洞 → 另一段
            eof_record(),
        ]
        img = SparseImage.from_writes("t", writes_from_lines(lines))
        self.assertEqual(img.segment_count, 2)
        self.assertEqual(img.defined_byte_count, 4)
        spans = img.read_spans(0x10, 0x14)
        kinds = [s.is_hole for s in spans]
        # 定义(0x10..0x13)、空洞(0x13..0x20)、定义(0x20..0x21)、尾部空洞
        self.assertEqual(kinds, [False, True, False, True])
        self.assertEqual(spans[0].data, b"\x01\x02\x03")

    def test_hole_is_not_zero(self):
        lines = [
            data_record(0, b"\x00\x00"),
            data_record(4, b"\x00"),
            eof_record(),
        ]
        img = SparseImage.from_writes("t", writes_from_lines(lines))
        self.assertEqual(img.byte_at(0), 0)
        self.assertTrue(img.is_defined(0))
        self.assertIsNone(img.byte_at(2))
        self.assertFalse(img.is_defined(2))
        self.assertEqual(img.sources_at(2), ())

    def test_read_span_runs_carry_sources(self):
        lines = [
            data_record(0, b"\x01"),
            data_record(1, b"\x02"),
            eof_record(),
        ]
        img = SparseImage.from_writes("t", writes_from_lines(lines))
        spans = img.read_spans(0, 2)
        self.assertEqual(len(spans), 1)
        run_lengths = [length for length, _ in spans[0].runs]
        self.assertEqual(run_lengths, [1, 1])

    def test_read_limit_1mib(self):
        img = SparseImage.from_writes("t", writes_from_lines([eof_record()]))
        # 空映像也必须返回覆盖整个窗口的空洞段
        empty = img.read_spans(0, 1 << 20)  # 恰好 1 MiB 允许
        self.assertEqual(sum(s.length for s in empty), 1 << 20)
        self.assertTrue(all(s.is_hole for s in empty))
        with self.assertRaises(ReadRangeError):
            img.read_spans(0, (1 << 20) + 1)
        with self.assertRaises(ReadRangeError):
            img.read_spans(0, -1)
        with self.assertRaises(ReadRangeError):
            img.read_spans(1 << 32, 1)

    def test_read_past_space_end_treated_as_hole(self):
        lines = [
            extended_linear_record(0xFFFF),
            data_record(0xFFFF, b"\x7e"),
            eof_record(),
        ]
        img = SparseImage.from_writes("t", writes_from_lines(lines))
        spans = img.read_spans(0xFFFFFFFF, 4)
        self.assertEqual([s.length for s in spans], [1, 3])
        self.assertFalse(spans[0].is_hole)
        self.assertTrue(spans[1].is_hole)


class MergeAndConflictTests(unittest.TestCase):
    def test_same_value_keeps_all_sources(self):
        lines = [
            data_record(0x100, b"\x77\x88"),
            data_record(0x101, b"\x88"),
            eof_record(),
        ]
        writes = writes_from_lines(lines)
        img = SparseImage.from_writes("t", writes)
        srcs = img.sources_at(0x101)
        self.assertEqual([s.line_no for s in srcs], [1, 2])
        self.assertEqual(img.byte_at(0x101), 0x88)

    def test_same_layer_conflict_rejects_entire_product(self):
        lines = [
            data_record(0x200, b"\x01\x02\x03"),
            data_record(0x201, b"\x7f"),
            eof_record(),
        ]
        with self.assertRaises(OverlayConflict) as cm:
            SparseImage.from_writes("t", writes_from_lines(lines))
        self.assertEqual(len(cm.exception.conflicts), 1)
        self.assertEqual(cm.exception.conflicts[0].address, 0x201)
        self.assertEqual(cm.exception.layer, 0)

    def test_multiple_conflicts_reported_together(self):
        lines = [
            data_record(0, b"\x01\x02\x03\x04"),
            data_record(0, b"\x09\x02\x08\x04"),
            eof_record(),
        ]
        with self.assertRaises(OverlayConflict) as cm:
            SparseImage.from_writes("t", writes_from_lines(lines))
        self.assertEqual(sorted(c.address for c in cm.exception.conflicts),
                         [0x0, 0x2])

    def test_frozen_image_rejects_mutation(self):
        img = SparseImage.from_writes("t", writes_from_lines([eof_record()]))
        with self.assertRaises(TypeError):
            img._put(0, b"\x01", [], reject_conflicts=False, layer=0,
                     conflicts=None)


class OverlayTests(unittest.TestCase):
    def test_high_layer_wins_low_layer_kept_elsewhere(self):
        low = writes_from_lines([
            data_record(0, b"\x0a\x0b\x0c"),
            eof_record(),
        ], source="low")
        high = writes_from_lines([
            data_record(1, b"\xff"),
            eof_record(),
        ], source="high")
        base = SparseImage.from_writes("b", low, layer=0)
        top = SparseImage.from_writes("h", high, layer=1)
        out = base.overlay(top, layer=1)
        self.assertEqual(out.byte_at(0), 0x0A)
        self.assertEqual(out.byte_at(1), 0xFF)
        self.assertEqual(out.byte_at(2), 0x0C)
        # 高层胜出后来源仅为高层
        self.assertEqual([s.file for s in out.sources_at(1)], ["high"])

    def test_same_value_across_layers_unions_sources(self):
        low = writes_from_lines([data_record(0, b"\x42"), eof_record()],
                                source="low")
        high = writes_from_lines([data_record(0, b"\x42"), eof_record()],
                                 source="high")
        out = (SparseImage.from_writes("b", low, layer=0)
               .overlay(SparseImage.from_writes("h", high, layer=1),
                        layer=1))
        self.assertEqual(sorted(s.file for s in out.sources_at(0)),
                         ["high", "low"])

    def test_layer_order_not_arrival_order(self):
        # 即使高层文件先被解析，裁决仍按显式层次
        high_first = writes_from_lines(
            [data_record(0, b"\x01"), eof_record()], source="high")
        low_second = writes_from_lines(
            [data_record(0, b"\x02"), eof_record()], source="low")
        low_img = SparseImage.from_writes("low", low_second, layer=0)
        high_img = SparseImage.from_writes("high", high_first, layer=2)
        out = low_img.overlay(high_img, layer=2)
        self.assertEqual(out.byte_at(0), 0x01)  # 高层值，与到达顺序无关


class CatalogAndReferenceTests(unittest.TestCase):
    def test_random_scenarios_match_reference_model(self):
        from heximg.catalog import build_product
        for seed in range(30):
            scn = make_random_scenario(seed, n_files=8, max_layers=3)
            with self.subTest(seed=seed):
                manifest = Manifest(
                    tuple(ManifestEntry(f, l)
                          for f, l in zip(scn.files, scn.layers)),
                    name="scn",
                )
                if scn.conflict_addresses:
                    with self.assertRaises(OverlayConflict):
                        build_product(manifest)
                    continue
                product = build_product(manifest)
                # 逐字节值与来源全量核对（地址域小，可全扫）
                lo, hi = product.address_span()
                for addr in range(0, hi + 4):
                    self.assertEqual(product.byte_at(addr),
                                     scn.ref.value(addr),
                                     f"seed={seed} addr={addr:#x}")
                    if scn.ref.value(addr) is not None:
                        got = sorted((s.file, s.line_no)
                                     for s in product.sources_at(addr))
                        exp = sorted((s.file, s.line_no)
                                     for s in scn.ref.sources(addr))
                        self.assertEqual(got, exp,
                                         f"seed={seed} addr={addr:#x} 来源")

    def test_overlay_helper_semantics_mirror_catalog(self):
        scn = make_random_scenario(99, n_files=5, max_layers=2)
        if scn.conflict_addresses:
            self.skipTest("该随机种子含同层冲突，已由其他用例覆盖")
        manifest = Manifest(
            tuple(ManifestEntry(f, l)
                  for f, l in zip(scn.files, scn.layers)),
            name="scn",
        )
        product = build_product(manifest)
        lo, hi = product.address_span()
        for addr in range(lo, hi):
            self.assertEqual(product.byte_at(addr), scn.ref.value(addr))


if __name__ == "__main__":
    unittest.main()
