"""确定性边界案例：舍入中点、乘法缩 scale、进位溢出、CASE 遮罩除零、
NULL 条件、输入严格性、schema 变化、计划序列化与回读核对。"""

import os
import tempfile
import unittest

from .. import api
from ..errors import CompileError, INPUT_ERROR
from ..planner import Plan
from ..row_engine import RowEngine
from ..runtime import Batch
from ..vector_engine import VectorEngine
from .helpers import col, const, arith, cmp, cast, case, when


class TypeDerivationTests(unittest.TestCase):
    def test_add_sub_scale_and_int_digits(self):
        plan = api.compile(
            arith("add", col("a"), col("b")),
            {"a": (5, 2), "b": (7, 3)})
        t = plan.result_type
        # scale=max(2,3)=3；原始 p=12 未超 38，整数位 = max(3,4)+1=5。
        self.assertEqual((t.precision, t.scale), (12, 3))

    def test_mul_scale_adds(self):
        plan = api.compile(
            arith("mul", col("a"), col("b")),
            {"a": (5, 2), "b": (7, 3)})
        t = plan.result_type
        # scale=5；整数位=(3)+(4)+1=8；p=12。
        self.assertEqual((t.precision, t.scale), (12, 5))

    def test_div_formula(self):
        plan = api.compile(
            arith("div", col("a"), col("b")),
            {"a": (10, 4), "b": (6, 2)})
        t = plan.result_type
        # scale=max(6, 4+6+1=11)=11；整数位=(10-4)+2=8；p=16。
        self.assertEqual((t.precision, t.scale), (16, 11))

    def test_overflow_precision_shrinks_scale_not_int(self):
        # 两个 decimal(20,2) 相乘：raw p=40 超限；
        # 需要整数位 18+18+1=37，scale 被压到 38-37=1。
        plan = api.compile(
            arith("mul", col("a"), col("b")),
            {"a": (20, 2), "b": (20, 2)})
        t = plan.result_type
        self.assertEqual(t.precision, 38)
        self.assertEqual(t.scale, 1)
        self.assertEqual(t.int_digits, 37)

    def test_int_digits_over_38_rejected_at_compile(self):
        # 两个 decimal(38,0) 相加：需要整数位 38+38+1=77 > 38。
        with self.assertRaises(CompileError):
            api.compile(
                arith("add", col("a"), col("b")),
                {"a": (38, 0), "b": (38, 0)})

    def test_div_int_digits_over_38_rejected_at_compile(self):
        # decimal(38,2) / decimal(38,18)：整数位=(38-2)+18=54 > 38。
        with self.assertRaises(CompileError):
            api.compile(
                arith("div", col("a"), col("b")),
                {"a": (38, 2), "b": (38, 18)})

    def test_precision_at_limit_38_accepted(self):
        plan = api.compile(col("a"), {"a": (38, 18)})
        self.assertEqual((plan.result_type.precision,
                          plan.result_type.scale), (38, 18))

    def test_bad_schema_decl_rejected(self):
        with self.assertRaises(CompileError):
            api.compile(col("a"), {"a": (39, 0)})
        with self.assertRaises(CompileError):
            api.compile(col("a"), {"a": (10, 11)})


class RoundingTests(unittest.TestCase):
    def setUp(self):
        # decimal(6,2) / decimal(4,2) => 节点 scale=max(6,2+4+1)=7，
        # 整数位=(6-2)+2=6，p=10。除以 8 制造中点场景。
        self.expr = arith("div", col("a"), const("8", 4, 2))
        self.schema = {"a": (6, 2)}

    def _run(self, rows, engine_cls):
        plan = api.compile(self.expr, self.schema)
        batch = Batch.from_rows(rows, plan)
        return engine_cls(plan).execute(batch)

    def test_half_even_midpoints_positive_and_negative(self):
        # 用 1/16 类除数制造真实的第 8 位小数中点（scale=7 时发生 half-even）。
        expr = arith("div", col("a"), const("16", 4, 2))
        plan = api.compile(expr, self.schema)
        rows = [
            {"a": "2.00"},     # 0.125 精确，无截断
            {"a": "0.02"},     # 0.00125 精确
            {"a": "0.06"},     # 0.00375 精确（scale7 下无截断点）
            {"a": "0.01"},     # 0.000625 -> scale7 中点，前位2偶 -> 0.0006250
            {"a": "0.03"},     # 0.001875 -> scale7 中点，前位7奇 -> 0.0018750
            {"a": "-0.03"},    # 负值对称 half-even
            {"a": "0.05"},     # 0.003125 -> 中点，前位2偶 -> 0.0031250
            {"a": "0.07"},     # 0.004375 -> 中点，前位7奇 -> 0.0043750
        ]
        for engine_cls in (RowEngine, VectorEngine):
            results, errors = engine_cls(plan).execute(
                Batch.from_rows(rows, plan))
            self.assertEqual(errors, {})
            texts = [str(r) for r in results]
            self.assertEqual(texts, [
                "0.1250000", "0.0012500", "0.0037500", "0.0006250",
                "0.0018750", "-0.0018750", "0.0031250", "0.0043750"])

    def test_mul_scale_shrink_type_derivation_and_exact_product(self):
        # 两个 decimal(20,2) 相乘：raw p=40 超限，scale 被压到 1，整数位 37。
        expr = arith("mul", col("a"), col("b"))
        plan = api.compile(expr, {"a": (20, 2), "b": (20, 2)})
        t = plan.result_type
        self.assertEqual((t.precision, t.scale, t.int_digits), (38, 1, 37))
        # 精确积 36 位整数，缩 scale 到 1 后仍可表示，不溢出。
        batch = Batch.from_rows(
            [{"a": "999999999999999999.99",
              "b": "999999999999999999.99"}], plan)
        results, errors = VectorEngine(plan).execute(batch)
        self.assertEqual(errors, {})
        self.assertEqual(str(results[0]),
                         "999999999999999999980000000000000000.0")

    def test_division_overflow_on_large_quotient(self):
        # x decimal(10,2) / z decimal(4,3)：scale=max(6,2+4+1)=7，
        # 整数位=(10-2)+3=11，容量 7 位整数（p14,s7）。
        plan = api.compile(
            arith("div", col("x"), col("z")),
            {"x": (10, 2), "z": (4, 3)})
        self.assertEqual((plan.result_type.precision,
                          plan.result_type.scale,
                          plan.result_type.int_digits), (14, 7, 7))
        # 商 1000.0/0.010=100000.0，6 位整数，容量内。
        ok_batch = Batch.from_rows(
            [{"x": "1000.00", "z": "0.010"}], plan)
        res, err = VectorEngine(plan).execute(ok_batch)
        self.assertEqual(err, {})
        self.assertEqual(str(res[0]), "100000.0000000")
        # 99999999.99/0.010=9999999999.0，10 位整数 > 容量 7 -> 溢出，
        # 错误节点来源即除法节点本身。
        bad = Batch.from_rows(
            [{"x": "99999999.99", "z": "0.010"}], plan)
        _, err2 = RowEngine(plan).execute(bad)
        self.assertEqual(err2[0].code, "overflow")
        self.assertEqual(err2[0].node_id, plan.root_id)

    def test_add_overflow_in_tight_type(self):
        # 两个 decimal(3,0) 相加：need_int=4，raw p=6 => decimal(6,0) 不溢出；
        # 改用相邻满值 + 满值触发声明整数位不足的真实加法溢出。
        plan = api.compile(
            arith("add",
                  cast(col("a"), 3, 0),
                  cast(col("b"), 3, 0)),
            {"a": (6, 2), "b": (6, 2)})
        # cast 后 add 的类型：p=6,s=0,int=6，不会溢出；改为 cast 到 decimal(2,0)
        plan = api.compile(
            arith("add",
                  cast(col("a"), 2, 0),
                  cast(col("b"), 2, 0)),
            {"a": (6, 2), "b": (6, 2)})
        # add 类型仍按 p=4,s=0 推导（int=3），99+99=198 恰 3 位 OK；
        # 再外层 cast 回 decimal(2,0) 必然溢出。
        expr = cast(
            arith("add", cast(col("a"), 2, 0), cast(col("b"), 2, 0)),
            2, 0)
        plan = api.compile(expr, {"a": (6, 2), "b": (6, 2)})
        batch = Batch.from_rows([{"a": "99.00", "b": "99.00"}], plan)
        _, errors = VectorEngine(plan).execute(batch)
        self.assertEqual(errors[0].code, "overflow")
        self.assertEqual(errors[0].node_id, plan.root_id)

    def test_carry_overflow_after_rounding(self):
        # 0.005 在 cast 到 scale=2 时 half-even 进位到 0.01 不溢出；
        # 构造 9.995 -> cast(decimal(3,2))：进位得 10.00，整数位 2 > 1。
        expr = cast(const("9.995", 6, 3), 3, 2)
        plan = api.compile(expr, {})
        batch = Batch.from_rows([{}], plan)
        for engine_cls in (RowEngine, VectorEngine):
            _, errors = engine_cls(plan).execute(batch)
            self.assertEqual(errors[0].code, "overflow")
            self.assertEqual(errors[0].node_id, plan.root_id)

    def test_intermediate_node_rounding_not_deferred(self):
        # 内层 (a/3) 先按节点 scale 舍入，再乘 3；若只在最终舍入会得到不同值。
        expr = arith("mul",
                     arith("div", col("a"), const("3", 2, 0)),
                     const("3", 2, 0))
        plan = api.compile(expr, {"a": (8, 4)})
        batch = Batch.from_rows([{"a": "10.0000"}], plan)
        results, errors = VectorEngine(plan).execute(batch)
        self.assertEqual(errors, {})
        # 10/3 在 div 节点 scale=max(6,4+2+1)=7 舍入为 3.3333333，*3=9.9999999
        self.assertEqual(str(results[0]), "9.9999999")


class CaseTests(unittest.TestCase):
    def _plan(self):
        # CASE WHEN a > 0 THEN cast(a/1, ...) ELSE cast(a/0, ...) END
        schema = {"a": (6, 2)}
        t = [10, 4]
        guarded_div_zero = arith("div", col("a"), const("0", 2, 0))
        good_div = arith("div", col("a"), const("1", 2, 0))
        expr = case(
            [when(cmp("gt", col("a"), const("0", 2, 0)),
                  cast(good_div, *t))],
            else_ast=cast(guarded_div_zero, *t))
        return api.compile(expr, schema), schema

    def test_unselected_branch_divzero_not_raised(self):
        plan, schema = self._plan()
        rows = [{"a": "5.00"}, {"a": "-2.00"}, {"a": "0.00"}]
        batch = Batch.from_rows(rows, plan)
        for engine_cls in (RowEngine, VectorEngine):
            results, errors = engine_cls(plan).execute(batch)
            # 行0 走 THEN：ELSE 里的除零不可达，行0 必须无错误且值正确。
            self.assertNotIn(0, errors)
            self.assertEqual(str(results[0]), "5.0000")
            # 行1/2 条件 false 而进入 ELSE，该分支的除零被实际求值 -> 数据错误，
            # 且错误来源必须是 ELSE 内的除法节点而不是 CASE 节点。
            self.assertEqual(errors[1].code, "div_by_zero")
            self.assertEqual(errors[2].code, "div_by_zero")
            self.assertEqual(errors[1].node_id, errors[2].node_id)
            self.assertIsInstance(errors[1].node_id, int)

        # 纯遮罩场景：所有行都命中 THEN，ELSE 除零对任何行都不可达。
        safe_rows = [{"a": "1.00"}, {"a": "2.00"}, {"a": "3.00"}]
        for engine_cls in (RowEngine, VectorEngine):
            results, errors = engine_cls(plan).execute(
                Batch.from_rows(safe_rows, plan))
            self.assertEqual(errors, {})
            self.assertEqual([str(r) for r in results],
                             ["1.0000", "2.0000", "3.0000"])

    def test_null_condition_skipped_and_no_else_returns_null(self):
        schema = {"a": (6, 2)}
        t = [8, 2]
        expr = case([
            when(cmp("gt", col("a"), const("0", 2, 0)),
                 cast(const("1", 2, 0), *t)),
            # 无 ELSE
        ])
        plan = api.compile(expr, schema)
        rows = [{"a": None}, {"a": "1.00"}, {"a": "-1.00"}]
        batch = Batch.from_rows(rows, plan)
        for engine_cls in (RowEngine, VectorEngine):
            results, errors = engine_cls(plan).execute(batch)
            self.assertEqual(errors, {})
            self.assertIsNone(results[0])   # NULL 条件跳过 -> NULL
            self.assertEqual(str(results[1]), "1.00")
            self.assertIsNone(results[2])   # false 跳过，无 ELSE -> NULL

    def test_branch_type_mismatch_rejected(self):
        schema = {"a": (6, 2)}
        expr = case([
            when(cmp("gt", col("a"), const("0", 2, 0)),
                 cast(const("1", 4, 2), 4, 2)),
        ], else_ast=cast(const("1", 6, 3), 6, 3))
        with self.assertRaises(CompileError):
            api.compile(expr, schema)

    def test_then_without_explicit_cast_rejected(self):
        schema = {"a": (6, 2)}
        expr = case([when(cmp("gt", col("a"), const("0", 2, 0)),
                          arith("add", col("a"), const("1", 2, 0)))])
        with self.assertRaises(CompileError):
            api.compile(expr, schema)


class NullSemanticsTests(unittest.TestCase):
    def test_null_arith_and_cmp_propagate_null(self):
        schema = {"a": (6, 2), "b": (6, 2)}
        plan = api.compile(
            cmp("eq", arith("add", col("a"), col("b")), const("1", 6, 2)),
            schema)
        rows = [{"a": None, "b": "1.00"}, {"a": "1.00", "b": None},
                {"a": "2.00", "b": None}]
        batch = Batch.from_rows(rows, plan)
        results, errors = VectorEngine(plan).execute(batch)
        self.assertEqual(errors, {})
        self.assertEqual(results, [None, None, None])

    def test_div_null_operand_no_divzero(self):
        plan = api.compile(
            arith("div", const(None, 6, 2), const("0", 2, 0)), {})
        results, errors = RowEngine(plan).execute(Batch.from_rows([{}], plan))
        self.assertEqual(errors, {})
        self.assertIsNone(results[0])


class InputStrictnessTests(unittest.TestCase):
    def test_unrepresentable_input_is_row_data_error(self):
        plan = api.compile(col("a"), {"a": (4, 2)})
        rows = [{"a": "12.345"},   # scale 过大，不隐式舍入
                {"a": "123.45"},   # 整数位超 2
                {"a": "12.34"},    # 合法
                {"a": None}]
        batch = Batch.from_rows(rows, plan)
        results, errors = VectorEngine(plan).execute(batch)
        self.assertEqual(set(errors), {0, 1})
        self.assertTrue(all(e.code == "input_error" for e in errors.values()))
        self.assertEqual(str(results[2]), "12.34")
        self.assertIsNone(results[3])

    def test_nan_inf_text_rejected(self):
        plan = api.compile(col("a"), {"a": (6, 2)})
        for bad in ("NaN", "Infinity", "-Infinity", "12.3.4"):
            batch = Batch.from_rows([{"a": bad}], plan)
            self.assertEqual(set(batch.input_errors), {0}, bad)
            self.assertEqual(batch.input_errors[0].code, INPUT_ERROR)

    def test_nan_inf_constant_rejects_plan(self):
        with self.assertRaises(CompileError):
            api.compile(const("NaN", 6, 2), {})

    def test_unrepresentable_constant_rejects_whole_plan(self):
        with self.assertRaises(CompileError):
            api.compile(const("1.234", 4, 2), {})


class SchemaBindingTests(unittest.TestCase):
    def test_saved_plan_rejects_changed_schema(self):
        expr = arith("add", col("a"), col("b"))
        plan = api.compile(expr, {"a": (6, 2), "b": (6, 2)})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "plan.json")
            plan.save(path)
            reloaded = Plan.load(path)
            # 篡改保存计划中的 schema 摘要模拟换 schema。
            reloaded.schema_fingerprint = "0" * 64
            with self.assertRaises(CompileError):
                Batch.from_rows([{"a": "1.00", "b": "2.00"}], reloaded)

    def test_same_schema_different_batch_runs(self):
        expr = arith("add", col("a"), col("b"))
        plan = api.compile(expr, {"a": (6, 2), "b": (6, 2)})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "plan.json")
            plan.save(path)
            reloaded = Plan.load(path)
            r1, e1 = VectorEngine(reloaded).execute(
                Batch.from_rows([{"a": "1.00", "b": "2.00"}], reloaded))
            # 另一个批次行数不同，验证执行器按当前批次大小重建列向量。
            r2, e2 = RowEngine(reloaded).execute(
                Batch.from_rows(
                    [{"a": "3.00", "b": "4.00"},
                     {"a": "5.00", "b": "6.00"}], reloaded))
            self.assertEqual((str(r1[0]), e1), ("3.00", {}))
            self.assertEqual(([str(x) for x in r2], e2),
                             (["7.00", "11.00"], {}))


class PersistenceAndReadbackTests(unittest.TestCase):
    def test_run_writes_parquet_errors_and_reads_back(self):
        expr = arith("div", col("a"), col("b"))
        plan = api.compile(expr, {"a": (8, 2), "b": (8, 2)})
        rows = [{"a": "10.00", "b": "4.00"},
                {"a": "10.00", "b": "0.00"},
                {"a": None, "b": "2.00"}]
        with tempfile.TemporaryDirectory() as d:
            reports = api.run(plan, [rows], out_dir=d, engine="vector")
            rep = reports[0]
            self.assertTrue(rep["readback_ok"])
            self.assertEqual(rep["n_ok"], 2)
            self.assertEqual(rep["n_errors"], 1)
            self.assertEqual(rep["error_codes"], ["div_by_zero"])
            self.assertTrue(os.path.exists(os.path.join(d, "plan.json")))
            self.assertTrue(os.path.exists(rep["parquet"]))
            self.assertTrue(os.path.exists(rep["errors_file"]))

    def test_row_and_vector_engines_identical(self):
        expr = arith("mul",
                     arith("add", col("a"), col("b")),
                     arith("sub", col("a"), const("1.5", 4, 2)))
        plan = api.compile(expr, {"a": (10, 3), "b": (10, 3)})
        rows = [{"a": f"{i}.{i % 7:03d}", "b": f"{-i}.250"}
                for i in range(50)]
        batch = Batch.from_rows(rows, plan)
        rr, re_ = RowEngine(plan).execute(batch)
        vr, ve = VectorEngine(plan).execute(batch)
        self.assertEqual(set(re_), set(ve))
        for x, y in zip(rr, vr):
            self.assertEqual(x, y)

    def test_boolean_result_parquet_and_multi_batch(self):
        expr = cmp("ge", col("a"), const("10", 4, 2))
        plan = api.compile(expr, {"a": (6, 2)})
        b1 = [{"a": "9.99"}, {"a": "10.00"}, {"a": None}]
        b2 = [{"a": "11.00"}]
        with tempfile.TemporaryDirectory() as d:
            reports = api.run(plan, [b1, b2], out_dir=d, engine="vector")
            self.assertEqual(len(reports), 2)
            for rep in reports:
                self.assertTrue(rep["readback_ok"])
            import pyarrow.parquet as pq
            table = pq.read_table(reports[0]["parquet"])
            self.assertTrue(str(table.schema.field("result").type)
                            .startswith("bool"))
            self.assertEqual(
                table.column("result").to_pylist(),
                [False, True, None])

    def test_unknown_column_and_bad_ast_rejected(self):
        with self.assertRaises(CompileError):
            api.compile(col("missing"), {"a": (6, 2)})
        with self.assertRaises(CompileError):
            api.compile({"op": "mystery"}, {"a": (6, 2)})
        with self.assertRaises(CompileError):
            api.compile(cmp("??", col("a"), col("a")), {"a": (6, 2)})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
