"""批次加载、schema 绑定与共享的节点求值语义。

行式与列式两个执行器共用本模块的单节点标量语义，保证异常可达性、
节点级舍入与错误来源完全一致；区别只在分支物化与复用的组织方式。
"""

import json

from .decimals import (
    Decimal, parse_decimal_text, check_input, round_to_node,
    add as dec_add, sub as dec_sub, mul as dec_mul, div as dec_div,
    round_arith_result,
)
from .errors import DataError, CompileError, SCHEMA_MISMATCH, INPUT_ERROR
from .types import DecimalType
from .planner import fingerprint_schema

CMP_FUNCS = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "ge": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
}


class Batch:
    """一批输入：列名 -> 每行（Decimal 或 None），另保留原始文本。"""

    def __init__(self, rows_text, schema_types, fp):
        if fp != fingerprint_schema(
                {n: (t.precision, t.scale) for n, t in schema_types.items()}):
            raise CompileError(
                "批次 schema 摘要与计划绑定的摘要不一致，"
                "换 schema 不能直接执行旧计划", code=SCHEMA_MISMATCH)
        self.nrows = len(rows_text)
        self.columns = {}
        # input_errors: row_index -> DataError（输入不可表示/不可解析）
        self.input_errors = {}
        for name, t in schema_types.items():
            vals = []
            for i, row in enumerate(rows_text):
                if name not in row:
                    raise CompileError(f"第 {i} 行缺少列 {name!r}")
                raw = row[name]
                if raw is None:
                    vals.append(None)
                    continue
                try:
                    v = parse_decimal_text(raw)
                    check_input(v, t, node_id=("column", name))
                except DataError as exc:
                    self.input_errors.setdefault(i, exc)
                    vals.append(None)
                    continue
                except CompileError as exc:
                    # 解析失败（含 NaN/Infinity/非法文本）属于该行的数据错误，
                    # 只拒绝该行，不拖垮整个批次。
                    self.input_errors.setdefault(
                        i, DataError(str(exc), INPUT_ERROR,
                                     node_id=("column", name)))
                    vals.append(None)
                    continue
                vals.append(v)
            self.columns[name] = vals

    @classmethod
    def from_jsonl(cls, path, plan):
        schema_types = {name: DecimalType(*decl)
                        for name, decl in plan.schema.items()}
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return cls(rows, schema_types, plan.schema_fingerprint)

    @classmethod
    def from_rows(cls, rows, plan):
        schema_types = {name: DecimalType(*decl)
                        for name, decl in plan.schema.items()}
        return cls(rows, schema_types, plan.schema_fingerprint)


def eval_scalar_node(node, child_values, row_error_sink):
    """单节点标量语义。``child_values`` 与节点 children/when 结构对齐。

    返回 Decimal / bool / None；节点自身数据错误写入 ``row_error_sink``
    （单元素列表）并返回 None。未选分支不会调用本函数，因此未选分支的
    除零等错误物理上不可能在此抛出。
    """
    op = node.op
    try:
        if op == "const":
            return None if node.value is None else parse_decimal_text(node.value)
        if op in ("add", "sub", "mul"):
            a, b = child_values
            if a is None or b is None:
                return None
            fn = {"add": dec_add, "sub": dec_sub, "mul": dec_mul}[op]
            return round_arith_result(fn(a, b), node.type, node.id)
        if op == "div":
            a, b = child_values
            if a is None or b is None:
                return None
            return dec_div(a, b, node.type, node.id)
        if op == "cast":
            (a,) = child_values
            if a is None:
                return None
            return round_to_node(a, node.to, node.id)
        if op == "cmp":
            a, b = child_values
            if a is None or b is None:
                return None
            return CMP_FUNCS[node.cmp](a, b)
        if op == "case":
            # child_values 由行式执行器按 [(cond, then)...] + else 组织
            pairs = child_values[:-1]
            else_val = child_values[-1]
            for cond, then_val in pairs:
                if cond is True:          # False 与 NULL 均跳过
                    return then_val
            return else_val               # 无 else 或未命中 -> None
    except DataError as exc:
        row_error_sink.append(exc)
        return None
    raise CompileError(f"执行器遇到未支持的节点算子: {op}", node_id=node.id)
