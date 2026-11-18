"""产品与差量报告的本地持久化仓（纯文件，不接触硬件）。

产品采用紧凑二进制布局，只编码已定义段——空洞不占数据空间，
因此高地址稀疏映像的存储量与空洞大小无关。

磁盘布局
========
``repo_dir/``
  products/<name>.heximg   二进制映像产品（见下）
  reports/<id>.json        差量报告（绑定映像摘要与分区目录修订）
  manifests/<name>.json    接入清单副本

产品二进制（全部整数大端）::

    MAGIC "HIMG" | u8 VERSION=1 | u32 name 长度 + name | u32 层次数
    u32 来源表大小；每条：u16 file 长度+file | u32 line_no
                            | u64 range_start | u64 range_end
    u32 段数；每段：u64 start | u32 数据长度 + data
                 | u32 游程数；每游程：u32 length | u16 nsrc + u32 来源下标...
    末尾 32 字节：以上全部内容的 SHA-256（任何字节损坏都会在加载时被发现）
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

from .errors import RepositoryError
from .delta import DeltaReport
from .image import Source, SparseImage, _Run, _Segment

MAGIC = b"HIMG"
VERSION = 1


class Repository:
    """目录型本地仓。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.products_dir = self.root / "products"
        self.reports_dir = self.root / "reports"
        self.manifests_dir = self.root / "manifests"
        for d in (self.products_dir, self.reports_dir, self.manifests_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    def save_product(self, image: SparseImage, *,
                     manifest_doc: dict | None = None) -> Path:
        if not image.frozen:
            raise RepositoryError("只能持久化已冻结的映像产品")
        path = self.products_dir / f"{image.name}.heximg"
        source_index: dict[Source, int] = {}
        source_table: list[Source] = []

        def idx(src: Source) -> int:
            if src not in source_index:
                source_index[src] = len(source_table)
                source_table.append(src)
            return source_index[src]

        buf = bytearray()
        name_b = image.name.encode("utf-8")
        buf += MAGIC
        buf += struct.pack(">B", VERSION)
        buf += struct.pack(">I", len(name_b)) + name_b
        buf += struct.pack(">I", image.layer_count)

        # 来源表先占位，写完段后回填
        table_pos = len(buf)
        buf += b"\x00\x00\x00\x00"
        for seg in image._segments:
            for run in seg.runs:
                for src in run.sources:
                    idx(src)
        buf[table_pos:table_pos + 4] = struct.pack(">I", len(source_table))
        for src in source_table:
            fb = src.file.encode("utf-8")
            buf += struct.pack(">H", len(fb)) + fb
            buf += struct.pack(">I", src.line_no)
            buf += struct.pack(">QQ", src.byte_range[0], src.byte_range[1])

        buf += struct.pack(">I", len(image._segments))
        for seg in image._segments:
            buf += struct.pack(">QI", seg.start, len(seg.data))
            buf += seg.data
            buf += struct.pack(">I", len(seg.runs))
            for run in seg.runs:
                buf += struct.pack(">I", run.length)
                buf += struct.pack(">H", len(run.sources))
                for src in run.sources:
                    buf += struct.pack(">I", source_index[src])

        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(bytes(buf) + hashlib.sha256(buf).digest())
        tmp.replace(path)

        if manifest_doc is not None:
            mp = self.manifests_dir / f"{image.name}.json"
            mp.write_text(json.dumps(manifest_doc, indent=2,
                                     ensure_ascii=False), encoding="utf-8")
        return path

    def load_product(self, name: str) -> SparseImage:
        path = self.products_dir / f"{name}.heximg"
        if not path.exists():
            raise RepositoryError(f"产品不存在：{path}")
        blob = path.read_bytes()
        if len(blob) < 32:
            raise RepositoryError(f"产品文件损坏：{path}（长度不足）")
        payload, trailer = blob[:-32], blob[-32:]
        if hashlib.sha256(payload).digest() != trailer:
            raise RepositoryError(f"产品文件整体校验失败：{path}")
        try:
            return self._decode(payload, name)
        except (struct.error, IndexError, UnicodeDecodeError) as exc:
            raise RepositoryError(f"产品文件损坏：{path}（{exc}）") from None

    @staticmethod
    def _decode(blob: bytes, name: str) -> SparseImage:
        pos = 0
        if blob[:4] != MAGIC:
            raise RepositoryError("魔数不匹配")
        pos = 4
        (ver,) = struct.unpack_from(">B", blob, pos)
        pos += 1
        if ver != VERSION:
            raise RepositoryError(f"不支持的产品版本：{ver}")
        (nlen,) = struct.unpack_from(">I", blob, pos)
        pos += 4
        stored_name = blob[pos:pos + nlen].decode("utf-8")
        pos += nlen
        (layer_count,) = struct.unpack_from(">I", blob, pos)
        pos += 4
        image = SparseImage(stored_name or name)
        layer_max = layer_count - 1

        (nsrc,) = struct.unpack_from(">I", blob, pos)
        pos += 4
        sources: list[Source] = []
        for _ in range(nsrc):
            (flen,) = struct.unpack_from(">H", blob, pos)
            pos += 2
            file = blob[pos:pos + flen].decode("utf-8")
            pos += flen
            (line_no,) = struct.unpack_from(">I", blob, pos)
            pos += 4
            rs, re = struct.unpack_from(">QQ", blob, pos)
            pos += 16
            sources.append(Source(file, line_no, (rs, re)))

        (nseg,) = struct.unpack_from(">I", blob, pos)
        pos += 4
        for _ in range(nseg):
            start, dlen = struct.unpack_from(">QI", blob, pos)
            pos += 12
            data = blob[pos:pos + dlen]
            pos += dlen
            (nruns,) = struct.unpack_from(">I", blob, pos)
            pos += 4
            runs: list[_Run] = []
            for _ in range(nruns):
                rlen, nidx = struct.unpack_from(">IH", blob, pos)
                pos += 6
                idxs = struct.unpack_from(f">{nidx}I", blob, pos)
                pos += 4 * nidx
                runs.append(_Run(rlen, tuple(sources[i] for i in idxs)))
            image._segments.append(_Segment(start, bytes(data), runs))

        image._starts = [s.start for s in image._segments]
        image._frozen = True
        # 层数随文件头恢复；来源已完整保留，逐字节回查不受影响
        image._layer_max = layer_max
        return image

    def list_products(self) -> list[str]:
        return sorted(p.stem for p in self.products_dir.glob("*.heximg"))

    # ------------------------------------------------------------------ #
    def save_report(self, report: DeltaReport, report_id: str) -> Path:
        path = self.reports_dir / f"{report_id}.json"
        path.write_text(report.to_json(), encoding="utf-8")
        return path

    def load_report(self, report_id: str) -> DeltaReport:
        path = self.reports_dir / f"{report_id}.json"
        if not path.exists():
            raise RepositoryError(f"报告不存在：{path}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        return DeltaReport.from_dict(doc)

    def list_reports(self) -> list[str]:
        return sorted(p.stem for p in self.reports_dir.glob("*.json"))
