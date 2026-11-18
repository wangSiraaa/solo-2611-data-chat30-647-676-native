"""列式输出：真实 decimal 类型的 Parquet 文件 + 独立错误清单，并回读核对。

结果列使用 pyarrow ``decimal128(p,s)``（p<=38 正是 decimal128 的范围），
写入的是精确整数系数而非 float；错误信息独立写到 JSON 清单，不与数据
混在同一文件。回读时按计划结果类型重建 arrow 类型并逐行比对数值。
"""

import json

import pyarrow as pa
import pyarrow.parquet as pq

from .decimals import Decimal, decimal_to_text
from .types import DecimalType


def _arrow_decimal_type(t: DecimalType):
    return pa.decimal128(t.precision, t.scale)


def _decimal_to_pa(value, t: DecimalType):
    if value is None:
        return None
    # pa.scalar(Decimal, decimal128) 走精确十进制路径，不经 float；
    # 传入 None 时返回 null scalar，.as_py() 即 Python None。
    return pa.scalar(value, type=_arrow_decimal_type(t)).as_py()


def write_batch_output(path_parquet, path_errors, plan, batch,
                       results, errors, engine_name, batch_index):
    """写一个批次的 Parquet 数据文件与错误清单，返回输出摘要 dict。"""
    n = batch.nrows
    result_t = plan.result_type

    arrays = {}
    # 输入列按其声明 decimal 类型落盘（错误行输入列仍保留原始解析值）。
    for name, decl in plan.schema.items():
        t = DecimalType(*decl)
        arrays[name] = pa.array(
            [_decimal_to_pa(v, t) for v in batch.columns[name]],
            type=_arrow_decimal_type(t),
        )

    if result_t == "boolean":
        result_array = pa.array(
            [None if i in errors else results[i] for i in range(n)],
            type=pa.bool_(),
        )
    else:
        result_array = pa.array(
            [None if i in errors else _decimal_to_pa(results[i], result_t)
             for i in range(n)],
            type=_arrow_decimal_type(result_t),
        )
    arrays["result"] = result_array
    arrays["row_id"] = pa.array(list(range(n)), type=pa.int64())
    arrays["status"] = pa.array(
        ["error" if i in errors else "ok" for i in range(n)],
        type=pa.string(),
    )

    table = pa.table(arrays)
    pq.write_table(table, path_parquet)

    manifest = {
        "engine": engine_name,
        "batch_index": batch_index,
        "nrows": n,
        "n_ok": n - len(errors),
        "n_errors": len(errors),
        "result_type": ("boolean" if result_t == "boolean"
                        else f"decimal({result_t.precision},{result_t.scale})"),
        "errors": [
            {
                "row_id": i,
                "code": exc.code,
                "node_id": _node_id_to_json(exc.node_id),
                "message": str(exc),
            }
            for i, exc in sorted(errors.items())
        ],
    }
    with open(path_errors, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def _node_id_to_json(node_id):
    if isinstance(node_id, tuple):
        return {"kind": node_id[0], "name": node_id[1]}
    return {"kind": "node", "id": node_id}


def read_back_and_verify(path_parquet, plan, expected_results, errors):
    """重新读取 Parquet，核对实际 arrow 物理类型与逐行数值。

    expected_results 中错误行应为 None；正常行必须与回读 Decimal 精确相等。
    返回 (ok, details)。
    """
    table = pq.read_table(path_parquet)
    details = {"schema": str(table.schema), "mismatches": []}

    # 1) 结果列物理类型必须与计划推导类型一致。
    result_field = table.schema.field("result")
    if plan.result_type == "boolean":
        if not pa.types.is_boolean(result_field.type):
            details["mismatches"].append(
                f"result 物理类型 {result_field.type} 不是 boolean")
    else:
        at = result_field.type
        if not (pa.types.is_decimal128(at)
                and at.precision == plan.result_type.precision
                and at.scale == plan.result_type.scale):
            details["mismatches"].append(
                f"result 物理类型 {at} 与计划类型 {plan.result_type} 不符")

    # 2) 输入列物理类型逐列核对。
    for name, decl in plan.schema.items():
        at = table.schema.field(name).type
        t = DecimalType(*decl)
        if not (pa.types.is_decimal128(at) and at.precision == t.precision
                and at.scale == t.scale):
            details["mismatches"].append(
                f"列 {name} 物理类型 {at} 与声明 {t} 不符")

    # 3) 逐行值精确比对（decimal 用 Decimal 相等，布尔直接相等，不做浮点比较）。
    col = table.column("result").to_pylist()
    status_col = table.column("status").to_pylist()
    is_bool_result = plan.result_type == "boolean"
    for i, got in enumerate(col):
        if i in errors:
            if status_col[i] != "error" or got is not None:
                details["mismatches"].append(
                    f"行 {i}: 错误行回读应为 NULL/error，得到 {got}/{status_col[i]}")
            continue
        want = expected_results[i]
        if want is None:
            if got is not None:
                details["mismatches"].append(
                    f"行 {i}: 期望 NULL，回读 {got}")
        elif is_bool_result:
            if got != want:
                details["mismatches"].append(
                    f"行 {i}: 期望布尔 {want}，回读 {got}")
        else:
            got_dec = got if isinstance(got, Decimal) else Decimal(str(got))
            if got_dec != want:
                details["mismatches"].append(
                    f"行 {i}: 期望 {decimal_to_text(want)}，回读 {got_dec}")

    return (not details["mismatches"]), details
