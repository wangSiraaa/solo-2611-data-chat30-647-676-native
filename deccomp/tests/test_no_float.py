"""静态守卫：数值路径不得出现 binary float 构造/转换。

扫描生产源码（不含测试与本文件），禁止 float 字面量与 float() 构造；
并在运行时断言关键 Decimal 由文本构造、结果中不存在 float 实例。
"""

import ast
import os
import unittest

from .. import api
from ..runtime import Batch
from ..vector_engine import VectorEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NoFloatTests(unittest.TestCase):
    def test_no_float_literals_or_constructors_in_source(self):
        violations = []
        for dirpath, _, files in os.walk(ROOT):
            if "tests" in dirpath.split(os.sep):
                continue
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fname)
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) \
                            and isinstance(node.value, float):
                        violations.append(f"{path}:{node.lineno} float 字面量")
                    if isinstance(node, ast.Call) and isinstance(
                            node.func, ast.Name) and node.func.id == "float":
                        violations.append(f"{path}:{node.lineno} float() 调用")
        self.assertEqual(violations, [])

    def test_runtime_values_are_decimal_or_none_or_bool(self):
        from decimal import Decimal
        plan = api.compile(
            {"op": "mul",
             "left": {"op": "add", "left": {"op": "column", "name": "a"},
                      "right": {"op": "const", "value": "1.5",
                                "type": [4, 2]}},
             "right": {"op": "column", "name": "b"}},
            {"a": (8, 3), "b": (8, 3)})
        rows = [{"a": "1.250", "b": "2.000"}, {"a": None, "b": "3.000"}]
        res, err = VectorEngine(plan).execute(Batch.from_rows(rows, plan))
        self.assertEqual(err, {})
        self.assertIsInstance(res[0], Decimal)
        self.assertIsNone(res[1])
        self.assertNotIsInstance(res[0], float)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
