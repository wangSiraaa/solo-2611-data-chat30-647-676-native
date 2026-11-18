"""规模与稀疏存储：100 文件 / 20 MiB 定义 / 100 分区，高地址稀疏存储量。"""

import tempfile
import time
import unittest
from pathlib import Path

from heximg.catalog import Manifest, ManifestEntry, build_product
from heximg.delta import VIEW_UNKNOWN, compare_images
from heximg.image import SparseImage
from heximg.partition import Partition, PartitionDirectory
from heximg.repo import Repository
from heximg.synth import (
    data_record,
    eof_record,
    extended_linear_record,
    write_text,
)

FILES = 100
PER_FILE = 200 * 1024          # 200 KiB × 100 = 20 MiB 定义
BLOCK_STRIDE = 256 * 1024      # 块间隔 256 KiB，块间留 56 KiB 空洞
REGION_BASE = 0x1000_0000
CHUNK = 255


def _write_block_file(path: str, base: int, size: int) -> None:
    """按 64 KiB 页切换 ELA，页内 255 字节 data 记录。"""
    if base % 0x10000:
        raise ValueError("本测试要求块按 64 KiB 对齐")
    lines = []
    written = 0
    page = 0
    while written < size:
        upper = ((base + page * 0x10000) >> 16) & 0xFFFF
        lines.append(extended_linear_record(upper))
        page_remaining = min(0x10000, size - written)
        offset = 0
        while page_remaining:
            n = min(CHUNK, page_remaining)
            payload = bytes(((base + written + j) & 0xFF
                             for j in range(n)))
            lines.append(data_record(offset, payload))
            offset += n
            written += n
            page_remaining -= n
        page += 1
    lines.append(eof_record())
    write_text(path, lines)


class ScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="heximg_scale_")
        cls.root = Path(cls.tmp.name)
        cls.files = []
        t0 = time.time()
        for i in range(FILES):
            base = REGION_BASE + i * BLOCK_STRIDE
            p = cls.root / f"block_{i:03d}.hex"
            _write_block_file(str(p), base, PER_FILE)
            cls.files.append(str(p))
        cls.gen_seconds = time.time() - t0

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_01_build_100_files_20mib(self):
        manifest = Manifest(
            tuple(ManifestEntry(f, 0) for f in self.files),
            name="scale20",
        )
        t0 = time.time()
        product = build_product(manifest, base_dir=self.root)
        elapsed = time.time() - t0
        self.assertEqual(product.defined_byte_count,
                         FILES * PER_FILE)  # 恰好 20 MiB
        self.assertEqual(product.segment_count, FILES)  # 块间有空洞
        # 稀疏：段数只与块数相关，与 4 GiB 空间无关
        self.assertLess(product.segment_count, 200)
        self.assertLess(elapsed, 120)

    def test_02_persistence_size_scales_with_defined_bytes(self):
        manifest = Manifest(
            tuple(ManifestEntry(f, 0) for f in self.files),
            name="scale20",
        )
        product = build_product(manifest, base_dir=self.root)
        repo = Repository(self.root / "store")
        path = repo.save_product(product)
        size = path.stat().st_size
        # 数据本体 20 MiB + 来源/游程/段头开销，必须远小于按地址空间分配
        self.assertLess(size, 26 * 1024 * 1024)
        # 重开后摘要一致、可逐字节回查
        back = repo.load_product("scale20")
        self.assertEqual(back.digest(), product.digest())
        probe = REGION_BASE + 37 * BLOCK_STRIDE + 0x1234
        self.assertIsNotNone(back.byte_at(probe))
        self.assertTrue(back.sources_at(probe))
        # 块间空洞仍可区分
        hole = REGION_BASE + 37 * BLOCK_STRIDE + PER_FILE + 8
        self.assertIsNone(back.byte_at(hole))

    def test_03_diff_against_second_build_with_100_partitions(self):
        manifest = Manifest(
            tuple(ManifestEntry(f, 0) for f in self.files),
            name="scaleA",
        )
        img_a = build_product(manifest, base_dir=self.root)

        # B 产品：复用前 99 个块，最后一个块改 16 个字节
        b_dir = self.root / "variant_b"
        b_dir.mkdir(exist_ok=True)
        b_files = []
        for i in range(FILES):
            src = Path(self.files[i])
            dst = b_dir / src.name
            if i == FILES - 1:
                lines = src.read_text().splitlines()
                # 篡改第一条 data 记录的若干字节后重算校验和
                from heximg.synth import checksum
                body = bytearray(bytes.fromhex(lines[1][1:]))
                for k in range(5, 21):
                    body[k] ^= 0xA5
                body[-1] = checksum(bytes(body[:-1]))
                lines[1] = ":" + bytes(body).hex().upper()
                write_text(dst, lines)
            else:
                dst.write_bytes(src.read_bytes())
            b_files.append(str(dst))
        img_b = build_product(
            Manifest(tuple(ManifestEntry(f, 0) for f in b_files),
                     name="scaleB"),
            base_dir=b_dir,
        )

        # 100 个互不重叠的小分区，跨各块边界与空洞分布
        parts = []
        for i in range(100):
            start = REGION_BASE + i * BLOCK_STRIDE
            fill = 0xFF if i % 2 == 0 else None
            parts.append(Partition(f"p{i:03d}", start, 0x1000, fill))
        directory = PartitionDirectory(parts, revision="r1")
        self.assertEqual(len(directory.partitions), 100)

        report = compare_images(img_a, img_b, directory,
                                view=VIEW_UNKNOWN)
        self.assertEqual(len(report.partitions), 100)
        # 最后一个分区承载全部 16 字节变化，其余分区完全不变
        last = report.partitions[-1]
        self.assertEqual(last.counts["changed"], 16)
        for pd in report.partitions[:-1]:
            self.assertEqual(pd.counts["changed"], 0)
            self.assertEqual(pd.counts["added"], 0)
            self.assertEqual(pd.counts["removed"], 0)

    def test_04_high_address_sparse_footprint(self):
        # 仅 1 KiB 定义、贴近 32 位上界；存储不得与地址跨度成比例
        from heximg.records import parse_hex
        lines = [extended_linear_record(0xFFFF)]
        base = 0x100000000 - 1024  # 0xFFFFFC00：最后一个字节恰好落在 0xFFFFFFFF
        for off in range(0, 1024, CHUNK):
            lines.append(data_record(
                (base + off) & 0xFFFF,
                bytes((off + j) & 0xFF for j in range(min(CHUNK, 1024 - off))),
            ))
        lines.append(eof_record())
        p = write_text(self.root / "high.hex", lines)
        img = SparseImage.from_writes(
            "high", parse_hex(p.read_bytes(), source=str(p)).writes)
        repo = Repository(self.root / "store_high")
        path = repo.save_product(img)
        self.assertLess(path.stat().st_size, 8 * 1024)
        back = repo.load_product("high")
        self.assertEqual(back.byte_at(0xFFFFFFFF),
                         (1024 - 1) & 0xFF)
        self.assertIsNone(back.byte_at(0))  # 低地址巨大区域是空洞


if __name__ == "__main__":
    unittest.main()
