"""持久化仓：产品重开回查、报告重开、损坏检测。"""

import json
import tempfile
import unittest
from pathlib import Path

from heximg.catalog import Manifest, ManifestEntry
from heximg.delta import VIEW_MATERIALIZED, compare_images
from heximg.image import SparseImage
from heximg.partition import Partition, PartitionDirectory
from heximg.records import parse_hex
from heximg.repo import Repository
from heximg.synth import data_record, eof_record


def _make_product(name="dev"):
    low = parse_hex(
        "\n".join([data_record(0, b"\x01\x02\x03"), eof_record()]) + "\n",
        source="low.hex",
    )
    high = parse_hex(
        "\n".join([data_record(1, b"\xff"), eof_record()]) + "\n",
        source="high.hex",
    )
    base = SparseImage.from_writes(name, low.writes, layer=0)
    top = SparseImage.from_writes(name + "_h", high.writes, layer=1)
    return base.overlay(top, layer=1)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_product_roundtrip_preserves_bytes_and_sources(self):
        product = _make_product()
        path = self.repo.save_product(product)
        self.assertTrue(path.exists())
        back = self.repo.load_product("dev")
        self.assertEqual(back.digest(), product.digest())
        self.assertEqual(back.defined_byte_count,
                         product.defined_byte_count)
        for addr in range(3):
            self.assertEqual(back.byte_at(addr), product.byte_at(addr))
        # 来源回查在重开后仍然有效
        self.assertEqual(
            [s.file for s in back.sources_at(0)], ["low.hex"])
        self.assertEqual(
            [s.file for s in back.sources_at(1)], ["high.hex"])
        # 同值归并来源也持久化
        low_txt = "\n".join([data_record(0, b"\x01"), eof_record()]) + "\n"
        low2 = parse_hex(low_txt, source="low2.hex")
        low_again = parse_hex(low_txt, source="low.hex")
        merged = SparseImage.from_writes(
            "m", low_again.writes + low2.writes)
        self.repo.save_product(merged)
        back_m = self.repo.load_product("m")
        self.assertEqual(
            sorted(s.file for s in back_m.sources_at(0)),
            ["low.hex", "low2.hex"])

    def test_reopen_after_new_repository_instance(self):
        product = _make_product("dev")
        self.repo.save_product(product)
        other = Repository(self.tmp.name)  # 模拟重开
        back = other.load_product("dev")
        self.assertEqual(back.digest(), product.digest())
        self.assertEqual(other.list_products(), ["dev"])

    def test_corrupt_file_detected(self):
        product = _make_product("dev")
        self.repo.save_product(product)
        p = Path(self.tmp.name) / "products" / "dev.heximg"
        blob = bytearray(p.read_bytes())
        blob[-1] ^= 0x01
        p.write_bytes(bytes(blob))
        with self.assertRaises(Exception):
            self.repo.load_product("dev")

    def test_missing_product_and_report(self):
        from heximg.errors import RepositoryError
        with self.assertRaises(RepositoryError):
            self.repo.load_product("nope")
        with self.assertRaises(RepositoryError):
            self.repo.load_report("nope")

    def test_report_roundtrip_stays_bound_to_revision(self):
        img_a = SparseImage.from_writes(
            "A", parse_hex(
                "\n".join([data_record(0, b"\x01"), eof_record()]) + "\n",
                source="a.hex").writes)
        img_b = SparseImage.from_writes(
            "B", parse_hex(
                "\n".join([data_record(0, b"\x02"), eof_record()]) + "\n",
                source="b.hex").writes)
        d = PartitionDirectory([Partition("p", 0, 8, fill=0)], "r7")
        report = compare_images(img_a, img_b, d,
                                view=VIEW_MATERIALIZED)
        self.repo.save_report(report, "case7")
        reopened = self.repo.load_report("case7")
        self.assertEqual(reopened.directory_revision, "r7")
        self.assertEqual(reopened.report_digest, report.report_digest)
        self.assertEqual(reopened.view, VIEW_MATERIALIZED)
        self.assertEqual(
            reopened.partitions[0].counts,
            report.partitions[0].counts)
        # 落盘 JSON 内嵌目录快照
        doc = json.loads((Path(self.tmp.name) / "reports"
                          / "case7.json").read_text())
        self.assertEqual(doc["directory_snapshot"][0]["name"], "p")

    def test_manifest_copy_saved_alongside_product(self):
        manifest = Manifest(
            (ManifestEntry("a.hex", 0), ManifestEntry("b.hex", 1)),
            name="dev")
        product = _make_product("dev")
        self.repo.save_product(product, manifest_doc=manifest.to_dict())
        doc = json.loads((Path(self.tmp.name) / "manifests"
                          / "dev.json").read_text())
        self.assertEqual(len(doc["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
