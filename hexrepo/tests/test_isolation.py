"""模块隔离：分析器只能消费公开 API，不得重新解析文件。"""

import ast
import unittest
from pathlib import Path

from heximg import delta as delta_module
from heximg.delta import compare_images
from heximg.image import SparseImage, Span
from heximg.partition import Partition, PartitionDirectory
from heximg.records import parse_hex
from heximg.synth import data_record, eof_record

PKG_DIR = Path(delta_module.__file__).parent


class ImportBoundaryTests(unittest.TestCase):
    def test_delta_never_imports_records_or_parser_internals(self):
        src = (PKG_DIR / "delta.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        forbidden_roots = ("heximg.records",)
        forbidden_names = {"parse_hex", "parse_hex_file", "DataWrite"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(
                        alias.name.startswith(forbidden_roots),
                        f"delta 模块禁止导入 {alias.name}（不得重新解析文件）",
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertFalse(
                    mod.startswith(forbidden_roots),
                    f"delta 模块禁止从 {mod} 导入",
                )
                for alias in node.names:
                    self.assertNotIn(
                        alias.name, forbidden_names,
                        f"delta 模块禁止使用解析器符号 {alias.name}",
                    )

    def test_delta_only_references_public_image_surface(self):
        """静态检查：delta 不访问 SparseImage 的下划线私有成员。"""
        src = (PKG_DIR / "delta.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and \
                    node.attr.startswith("_") and not \
                    node.attr.startswith("__"):
                # 允许枚举/常量定义处的自有私有；这里禁止形如 image._xxx
                if isinstance(node.value, ast.Name) and \
                        node.value.id in {"image_a", "image_b", "image"}:
                    self.fail(
                        f"分析器访问了映像私有成员 {node.attr}，"
                        "只能使用公开范围读取/来源 API")

    def test_analyzer_works_through_public_only_proxy(self):
        """运行时隔离：用只暴露公开 API 的代理对象跑完整分析。"""
        text_a = "\n".join([data_record(0, b"\x01\x02"), eof_record()]) + "\n"
        text_b = "\n".join([data_record(0, b"\x01\x09"), eof_record()]) + "\n"
        img_a = SparseImage.from_writes("A", parse_hex(text_a, source="a").writes)
        img_b = SparseImage.from_writes("B", parse_hex(text_b, source="b").writes)

        class PublicOnlyImage:
            """白名单代理：任何私有访问立即失败。"""

            _ALLOWED = {
                "name", "read_spans", "byte_at", "sources_at",
                "is_defined", "defined_spans", "defined_byte_count",
                "segment_count", "address_span", "digest", "frozen",
                "layer_count", "iter_bytes",
            }

            def __init__(self, inner):
                object.__setattr__(self, "_inner", inner)

            def __getattribute__(self, name):
                if name == "_inner":
                    return object.__getattribute__(self, name)
                if name.startswith("_") and name not in PublicOnlyImage._ALLOWED:
                    raise AssertionError(
                        f"分析器越权访问私有/未公开成员：{name}")
                if name not in PublicOnlyImage._ALLOWED:
                    raise AssertionError(f"代理未开放的成员：{name}")
                return getattr(object.__getattribute__(self, "_inner"), name)

        directory = PartitionDirectory(
            [Partition("p", 0, 16, fill=0)], "r1")
        report = compare_images(PublicOnlyImage(img_a),
                                PublicOnlyImage(img_b),
                                directory, view="unknown")
        # 仍得到正确结论
        changed = [r for p in report.partitions for r in p.runs
                   if r.kind == "changed"]
        self.assertEqual([r.start for r in changed], [1])

    def test_span_contract_hole_vs_defined(self):
        text = "\n".join([
            data_record(0, b"\x00"),
            data_record(4, b"\x00"),
            eof_record(),
        ]) + "\n"
        img = SparseImage.from_writes("x", parse_hex(text, source="x").writes)
        spans = img.read_spans(0, 6)
        self.assertTrue(spans[1].is_hole)
        self.assertIsNone(spans[1].data)
        self.assertFalse(spans[0].is_hole)
        self.assertIsInstance(spans[0], Span)


if __name__ == "__main__":
    unittest.main()
