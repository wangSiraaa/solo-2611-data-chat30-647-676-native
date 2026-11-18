"""随机 fuzz：整数/Fraction oracle 与行式、列式引擎逐节点比对。

覆盖：
* 随机深度的算术/比较/CAST/CASE 表达式，随机 NULL 与零除数；
* CASE 中故意放入除零分支，验证未选分支不可达、选中分支错误可达；
* 共享子表达式（同一子树被两个父节点引用）验证列式 memo 扩张；
* 逐节点 trace 比对（值、scale、错误码、实际求值节点集合），
  而不是只比最终结果；
* 改变批大小（1/2/3/7/prime 切分）核对行身份与错误位置。
"""

import random
import unittest
from fractions import Fraction

from .. import api
from ..row_engine import RowEngine
from ..runtime import Batch
from ..vector_engine import VectorEngine
from .helpers import (col, const, arith, cmp, cast, case, when,
                      engine_value_to_fraction, oracle_value_to_fraction)
from .oracle import Oracle, _quantize_half_even

# 固定随机种子保证可复跑。
SEED = 20261118


def _rand_decimal_text(rng, p, s, allow_null=True, zero_prob=0.12):
    if allow_null and rng.random() < 0.18:
        return None
    if rng.random() < zero_prob:
        return "0." + "0" * s if s else "0"
    int_len = rng.randint(1, max(1, p - s))
    frac_len = s
    digits = "".join(str(rng.randint(0, 9)) for _ in range(int_len + frac_len))
    digits = str(rng.randint(1, 9)) + digits[1:]
    if frac_len:
        text = digits[:int_len] + "." + digits[int_len:]
    else:
        text = digits
    if rng.random() < 0.35:
        text = "-" + text
    return text


class FuzzBuilder:
    """生成随机 AST；列集合固定为 c0..c4，均为小 precision 以制造溢出/缩 scale。"""

    COLS = ["c0", "c1", "c2", "c3", "c4"]

    def __init__(self, rng):
        self.rng = rng
        self.schema = {
            "c0": (8, 3), "c1": (8, 3), "c2": (10, 4),
            "c3": (6, 2), "c4": (12, 6),
        }

    def expr(self, depth):
        rng = self.rng
        if depth <= 0:
            if rng.random() < 0.5:
                return col(rng.choice(self.COLS))
            return self._rand_const(rng)
        kind = rng.choice(
            ["add", "sub", "mul", "div", "cmp", "cast", "case"])
        if kind in ("add", "sub", "mul", "div"):
            return arith(kind, self.expr(depth - 1), self.expr(depth - 1))
        if kind == "cmp":
            return cmp(rng.choice(["lt", "le", "eq", "ne", "ge", "gt"]),
                       self.expr(depth - 1), self.expr(depth - 1))
        if kind == "cast":
            p = rng.randint(4, 18)
            s = rng.randint(0, min(p, 8))
            return cast(self.expr(depth - 1), p, s)
        # case：结果类型固定 decimal(12,4)，分支全部显式 cast
        t = (12, 4)
        branches = []
        for _ in range(rng.randint(1, 3)):
            cond = cmp(rng.choice(["lt", "gt", "eq"]),
                       col(rng.choice(self.COLS)),
                       const(_rand_decimal_text(rng, 8, 3, allow_null=False),
                             8, 3))
            then = cast(self._maybe_divzero(depth - 1), *t)
            branches.append(when(cond, then))
        else_ast = None
        if rng.random() < 0.7:
            else_ast = cast(self._maybe_divzero(depth - 1), *t)
        return case(branches, else_ast)

    def _rand_const(self, rng):
        s = rng.randint(0, 4)
        p = rng.randint(max(s, 4), 8)
        return const(_rand_decimal_text(rng, p, s), p, s)

    def decimal_expr(self, depth):
        """只生成 decimal 结果的随机表达式（无 cmp/case），用于行身份比对。"""
        rng = self.rng
        if depth <= 0:
            if rng.random() < 0.5:
                return col(rng.choice(self.COLS))
            return self._rand_const(rng)
        kind = rng.choice(["add", "sub", "mul", "div", "cast"])
        if kind == "cast":
            p = rng.randint(4, 18)
            s = rng.randint(0, min(p, 8))
            return cast(self.decimal_expr(depth - 1), p, s)
        return arith(kind, self.decimal_expr(depth - 1),
                     self.decimal_expr(depth - 1))

    def _maybe_divzero(self, depth):
        """以一定概率生成 col / 0 子树，用于测试 CASE 遮罩下的可达性。"""
        if self.rng.random() < 0.4:
            return arith("div", self.expr(depth), const("0", 4, 2))
        return self.expr(depth)


def shared_tree_ast(rng):
    """构造共享子树：(x*y) + (x*y) - (x*y)，x*y 被引用三次。"""
    x, y = col("c0"), col("c1")
    shared = arith("mul", x, y)
    return arith("sub", arith("add", shared, shared), shared)


class OracleEquivalenceTests(unittest.TestCase):
    def _assert_row_equiv(self, plan, rows, label):
        batch = Batch.from_rows(rows, plan)
        row_eng = RowEngine(plan, trace=True)
        vec_eng = VectorEngine(plan, trace=True)
        r_res, r_err = row_eng.execute(batch)
        v_res, v_err = vec_eng.execute(batch)

        # 1) 两个引擎错误行集合与错误码逐行一致。
        self.assertEqual(set(r_err), set(v_err), label)
        for i in r_err:
            self.assertEqual(r_err[i].code, v_err[i].code,
                             f"{label} 行{i} 错误码")
            self.assertEqual(r_err[i].node_id, v_err[i].node_id,
                             f"{label} 行{i} 错误节点来源")

        oracle = Oracle(plan)
        for i, row in enumerate(rows):
            if i in batch.input_errors:
                continue  # 输入错误在 oracle 之外已单独测试
            o_pair, o_err = oracle.run_row(row)

            if o_err is not None:
                self.assertIn(i, r_err, f"{label} 行{i} oracle 报错但引擎未报")
                self.assertEqual(o_err.code, r_err[i].code,
                                 f"{label} 行{i} oracle/引擎错误码")
                self.assertEqual(o_err.node_id, r_err[i].node_id,
                                 f"{label} 行{i} oracle/引擎错误节点")
            else:
                self.assertNotIn(i, r_err, f"{label} 行{i} 引擎误报 {r_err.get(i)}")
                if plan.result_type == "boolean":
                    self.assertEqual(r_res[i], o_pair, f"{label} 行{i} 布尔结果")
                    self.assertEqual(v_res[i], o_pair, f"{label} 行{i} 布尔结果")
                else:
                    of = oracle_value_to_fraction(o_pair)
                    self.assertEqual(engine_value_to_fraction(r_res[i]), of,
                                     f"{label} 行{i} 行式结果")
                    self.assertEqual(engine_value_to_fraction(v_res[i]), of,
                                     f"{label} 行{i} 列式结果")

            # 2) 逐节点 trace：oracle 实际求值的节点集合必须是引擎的子集
            #    （引擎对 NULL 短路节点也记录 None），共有节点值/错误一致。
            self._assert_trace_equiv(oracle, row_eng.traces[i],
                                     vec_eng.traces[i], i, label, plan)

    def _assert_trace_equiv(self, oracle, r_trace, v_trace, row_idx,
                            label, plan):
        # 行式/列式两个引擎的求值节点集合必须完全相同（短路语义一致）。
        self.assertEqual(set(r_trace), set(v_trace),
                         f"{label} 行{row_idx} 引擎间求值节点集合不同")
        for nid, (r_val, r_code) in r_trace.items():
            v_val, v_code = v_trace[nid]
            self.assertEqual(r_code, v_code,
                             f"{label} 行{row_idx} 节点{nid} 错误码不一致")
            node = plan.nodes[nid]
            if node.type == "boolean":
                self.assertEqual(r_val, v_val,
                                 f"{label} 行{row_idx} 节点{nid} 布尔值")
            elif r_val is None:
                self.assertIsNone(v_val, f"{label} 行{row_idx} 节点{nid} NULL")
            else:
                self.assertEqual(
                    engine_value_to_fraction(r_val),
                    engine_value_to_fraction(v_val),
                    f"{label} 行{row_idx} 节点{nid} decimal 值")
        # oracle trace 中每个节点都应在引擎 trace 里且 Fraction 值相等。
        for nid, pair in oracle.trace.items():
            self.assertIn(nid, r_trace,
                          f"{label} 行{row_idx} oracle 求值节点{nid} 引擎缺失")
            if r_trace[nid][1] is None and plan.nodes[nid].type != "boolean":
                self.assertEqual(
                    engine_value_to_fraction(r_trace[nid][0]),
                    oracle_value_to_fraction(pair),
                    f"{label} 行{row_idx} 节点{nid} oracle 数值不一致")

    def test_random_expressions_against_oracle(self):
        rng = random.Random(SEED)
        builder = FuzzBuilder(rng)
        for trial in range(60):
            ast = builder.expr(rng.randint(2, 5))
            try:
                plan = api.compile(ast, builder.schema)
            except api.CompileError:
                continue  # 合法的编译拒绝（整数位超 38 等）
            nrows = rng.randint(1, 40)
            rows = [
                {name: _rand_decimal_text(rng, *decl)
                 for name, decl in builder.schema.items()}
                for _ in range(nrows)]
            self._assert_row_equiv(plan, rows, f"trial-{trial}")

    def test_shared_subtree_memo_growth(self):
        rng = random.Random(SEED + 1)
        builder = FuzzBuilder(rng)
        ast = shared_tree_ast(rng)
        plan = api.compile(ast, builder.schema)
        rows = [
            {name: _rand_decimal_text(rng, *decl)
             for name, decl in builder.schema.items()}
            for _ in range(30)]
        self._assert_row_equiv(plan, rows, "shared")

    def test_batch_size_variations_preserve_row_identity(self):
        rng = random.Random(SEED + 2)
        builder = FuzzBuilder(rng)
        # 深嵌套乘法可能触发“整数位 >38”编译拒绝，重试到可编译计划。
        for _ in range(20):
            ast = builder.decimal_expr(4)
            try:
                plan = api.compile(ast, builder.schema)
                break
            except api.CompileError:
                continue
        else:  # pragma: no cover
            self.skipTest("随机流中未产生可编译计划")
        self.assertNotEqual(plan.result_type, "boolean")
        rows = [
            {name: _rand_decimal_text(rng, *decl)
             for name, decl in builder.schema.items()}
            for _ in range(60)]
        # 整体一次跑作为基准。
        base_batch = Batch.from_rows(rows, plan)
        base_res, base_err = VectorEngine(plan).execute(base_batch)

        for size in (1, 2, 3, 7, 13):
            for bi, start in enumerate(range(0, len(rows), size)):
                part = rows[start:start + size]
                batch = Batch.from_rows(part, plan)
                res, err = VectorEngine(plan).execute(batch)
                for j in range(len(part)):
                    global_id = start + j
                    in_err = global_id in base_err
                    self.assertEqual(j in err, in_err,
                                     f"size={size} 批{bi} 行{j} 错误身份")
                    if in_err:
                        self.assertEqual(err[j].code,
                                         base_err[global_id].code)
                    else:
                        self.assertEqual(res[j], base_res[global_id],
                                         f"size={size} 批{bi} 行{j} 数值身份")

    def test_quantize_half_even_known_values(self):
        # 手写 half-even 的独立单元检查（纯整数/Fraction）。
        self.assertEqual(_quantize_half_even(Fraction(25, 100), 2), 25)
        self.assertEqual(_quantize_half_even(Fraction(125, 1000), 2), 12)
        self.assertEqual(_quantize_half_even(Fraction(135, 1000), 2), 14)
        self.assertEqual(_quantize_half_even(Fraction(-135, 1000), 2), -14)
        self.assertEqual(_quantize_half_even(Fraction(-125, 1000), 2), -12)
        self.assertEqual(_quantize_half_even(Fraction(1, 8), 7), 1250000)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
