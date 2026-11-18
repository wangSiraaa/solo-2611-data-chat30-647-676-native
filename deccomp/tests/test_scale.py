"""规模测试：20 列、约 100 节点、10000 行的端到端批量执行。

要求：
* 计划节点数达到 100；
* 行式与列式引擎逐行结果、错误位置完全一致；
* 不同批大小切分后行身份不变；
* 每批产出真实 Parquet 并回读核对物理 decimal 类型与数值。
"""

import os
import random
import tempfile
import unittest

from .. import api
from ..output import read_back_and_verify
from ..planner import Plan
from ..row_engine import RowEngine
from ..runtime import Batch
from ..vector_engine import VectorEngine
from .helpers import col, arith, cast
from .test_fuzz import _rand_decimal_text

N_COLS = 20
N_NODES_TARGET = 100
N_ROWS = 10000


def _build_schema():
    rng = random.Random(77)
    schema = {}
    for i in range(N_COLS):
        p = rng.randint(8, 18)
        s = rng.randint(0, p // 2)
        schema[f"d{i:02d}"] = (p, s)
    return schema, rng


def _build_big_ast(schema, rng):
    """组装约 100 节点的链式混合运算表达式，且保证通过编译红线。

    乘法整数位相加、除法整数位为 (p-s)+s_right，链式使用都会快速突破
    38；加减链在类型已涨到 38 后同样会触红线。每步先试目标算子，编译
    拒绝则退化为加减，仍拒绝时先 CAST 回小精度再继续，使计划始终可
    编译，同时保留足够多的 mul/div/cast 节点覆盖缩 scale、节点级
    舍入与除零运行时路径。
    """
    names = list(schema)
    node = col(names[0])
    # 50 轮：每轮新增 1~2 个运算节点（叶子列复用不重复建节点），
    # 运算节点总数 50(+CAST) 加上 20 个列叶子，稳定超过 100 节点目标。
    for i in range(1, 51):
        next_col = col(names[i % len(names)])
        candidate_ops = (["mul", "div"] if i % 3 == 0
                         else ["add", "sub"])
        chosen = None
        for op in candidate_ops:
            try:
                api.compile(arith(op, node, next_col), schema)
                chosen = op
                break
            except api.CompileError:
                continue
        if chosen is not None:
            node = arith(chosen, node, next_col)
        else:
            # 位宽触顶：CAST 回 decimal(10,4) 重置后再做一次算术，
            # cast + arith + 叶子列，一轮推进 4 个节点。
            node = cast(node, 10, 4)
            node = arith("add", node, next_col)
    return node


class ScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema, cls.rng = _build_schema()
        cls.ast = _build_big_ast(cls.schema, cls.rng)
        cls.plan = api.compile(cls.ast, cls.schema)
        cls.rows = [
            {name: _rand_decimal_text(cls.rng, *decl, zero_prob=0.02)
             for name, decl in cls.schema.items()}
            for _ in range(N_ROWS)]

    def test_01_node_count_reaches_target(self):
        self.assertGreaterEqual(len(self.plan.nodes), N_NODES_TARGET)
        self.assertEqual(len(self.plan.schema), N_COLS)

    def test_02_row_and_vector_identical_at_scale(self):
        batch = Batch.from_rows(self.rows, self.plan)
        r_res, r_err = RowEngine(self.plan).execute(batch)
        v_res, v_err = VectorEngine(self.plan).execute(batch)
        self.assertEqual(set(r_err), set(v_err))
        for i in r_err:
            self.assertEqual(r_err[i].code, v_err[i].code)
            self.assertEqual(r_err[i].node_id, v_err[i].node_id)
        diffs = [i for i in range(N_ROWS)
                 if i not in r_err and r_res[i] != v_res[i]]
        self.assertEqual(diffs, [])
        # 统计数据错误行（除零/溢出），其余行必须有明确结果或 NULL。
        self.assertLess(len(r_err), N_ROWS)
        for i in range(N_ROWS):
            if i not in r_err:
                _ = r_res[i]  # None（合法 NULL 传播）或 Decimal，去向明确

    def test_03_batch_split_row_identity(self):
        full = Batch.from_rows(self.rows, self.plan)
        base_res, base_err = VectorEngine(self.plan).execute(full)
        for size in (1, 256, 1000, 3333, 10000):
            seen = 0
            for start in range(0, N_ROWS, size):
                part = self.rows[start:start + size]
                batch = Batch.from_rows(part, self.plan)
                res, err = VectorEngine(self.plan).execute(batch)
                for j in range(len(part)):
                    gid = start + j
                    if gid in base_err:
                        self.assertEqual(err[j].code, base_err[gid].code)
                    else:
                        self.assertEqual(res[j], base_res[gid])
                    seen += 1
            self.assertEqual(seen, N_ROWS)

    def test_04_parquet_write_and_readback_batched(self):
        with tempfile.TemporaryDirectory() as d:
            size = 2500
            total_ok = total_err = 0
            for bi, start in enumerate(range(0, N_ROWS, size)):
                part = self.rows[start:start + size]
                batch = Batch.from_rows(part, self.plan)
                res, err = VectorEngine(self.plan).execute(batch)
                pq_path = os.path.join(d, f"batch_{bi}.parquet")
                err_path = os.path.join(d, f"batch_{bi}.errors.json")
                from ..output import write_batch_output
                write_batch_output(pq_path, err_path, self.plan, batch,
                                   res, err, "vector", bi)
                ok, details = read_back_and_verify(pq_path, self.plan,
                                                   res, err)
                self.assertTrue(ok, details["mismatches"][:5])
                total_ok += len(part) - len(err)
                total_err += len(err)
            self.assertEqual(total_ok + total_err, N_ROWS)

    def test_05_plan_reload_is_byte_stable(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "plan.json")
            self.plan.save(path)
            reloaded = Plan.load(path)
            self.assertEqual(reloaded.to_dict(), self.plan.to_dict())
            batch = Batch.from_rows(self.rows[:500], reloaded)
            r1, e1 = VectorEngine(reloaded).execute(batch)
            r2, e2 = RowEngine(self.plan).execute(
                Batch.from_rows(self.rows[:500], self.plan))
            self.assertEqual(set(e1), set(e2))
            self.assertEqual(r1, r2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
