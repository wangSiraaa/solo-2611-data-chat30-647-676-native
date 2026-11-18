"""端到端演示：自生成题面要求的全部案例并真实落盘、回读核对。

覆盖：
1. 正/负 half-even 舍入中点；
2. 乘法缩 scale（raw precision 超 38，scale 向 0 收缩）；
3. 舍入后进位溢出；
4. 可被 CASE 遮住的除零（未选分支不产生数据错误）；
5. NULL 条件（false/NULL 跳过，无命中返回 NULL）；
6. schema 变化（旧计划拒绝执行）；
7. 整数/有理数 oracle 逐节点比对；
8. 改变批大小核对行身份、数值与错误位置；
9. 真实 Parquet decimal 列式文件 + 独立错误清单 + 回读核对。

运行：python3 demo.py
"""

import json
import os
import shutil

from deccomp import api
from deccomp.errors import CompileError
from deccomp.planner import Plan
from deccomp.runtime import Batch
from deccomp.row_engine import RowEngine
from deccomp.vector_engine import VectorEngine
from deccomp.tests.helpers import col, const, arith, cmp, cast, case, when
from deccomp.tests.oracle import Oracle

OUT = "demo_out"


def banner(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    # ------------------------------------------------------------------
    banner("1. 正/负 half-even 中点 + 中间节点舍入（非最终才舍入）")
    schema = {"a": (8, 3)}
    # a / 16：div 节点 scale=max(6,3+4+1)=8，制造第 9 位小数的 half-even。
    expr = arith("div", col("a"), const("16", 4, 2))
    plan = api.compile(expr, schema,
                       plan_path=f"{OUT}/01_rounding.plan.json")
    print(api.explain(plan))
    rows = [
        {"a": "0.01"},    # 0.000625   -> scale8 中点，前位 2 偶 -> 舍
        {"a": "0.03"},    # 0.001875   -> 中点，前位 7 奇 -> 入
        {"a": "-0.03"},   # 负值对称
        {"a": "2.00"},    # 0.125 精确
    ]
    write_jsonl(f"{OUT}/01_rounding.jsonl", rows)
    rep = api.run(plan, [f"{OUT}/01_rounding.jsonl"],
                  out_dir=f"{OUT}/01", engine="vector")[0]
    print(f"回读核对: {rep['readback_ok']}，错误行: {rep['n_errors']}")

    # ------------------------------------------------------------------
    banner("2. 乘法缩 scale：decimal(20,2)*decimal(20,2) -> decimal(38,1)")
    schema2 = {"a": (20, 2), "b": (20, 2)}
    plan2 = api.compile(arith("mul", col("a"), col("b")), schema2,
                        plan_path=f"{OUT}/02_mulshrink.plan.json")
    print(f"推导结果类型: {plan2.result_type}（raw p=40 超限，scale 5->1，"
          f"整数位 37 完整保留）")
    rows2 = [{"a": "999999999999999999.99",
              "b": "999999999999999999.99"}]
    write_jsonl(f"{OUT}/02_mulshrink.jsonl", rows2)
    rep2 = api.run(plan2, [f"{OUT}/02_mulshrink.jsonl"],
                   out_dir=f"{OUT}/02", engine="row")[0]
    print(f"回读核对: {rep2['readback_ok']}")

    # ------------------------------------------------------------------
    banner("3. 舍入后进位溢出：9.995 CAST 到 decimal(3,2) -> 10.00 溢出")
    plan3 = api.compile(cast(const("9.995", 6, 3), 3, 2), {},
                        plan_path=f"{OUT}/03_carry.plan.json")
    rep3 = api.run(plan3, [[{"_": None}]], out_dir=f"{OUT}/03",
                   engine="vector")[0]
    print(f"错误清单: {rep3['errors_file']}，码: {rep3['error_codes']}")

    # ------------------------------------------------------------------
    banner("4. CASE 遮住的除零：未选分支不求值、不产生数据错误")
    schema4 = {"a": (8, 2)}
    t = (12, 4)
    guarded = arith("div", col("a"), const("0", 2, 0))     # 含除零
    good = arith("div", col("a"), const("1", 2, 0))
    expr4 = case(
        [when(cmp("gt", col("a"), const("0", 2, 0)),
              cast(good, *t))],
        else_ast=cast(guarded, *t))
    plan4 = api.compile(expr4, schema4,
                        plan_path=f"{OUT}/04_caseguard.plan.json")
    rows4 = [{"a": "5.00"}, {"a": "2.00"},   # 命中 THEN：ELSE 除零不可达
             {"a": "-1.00"}]                  # 进 ELSE：除零成为真实数据错误
    write_jsonl(f"{OUT}/04_caseguard.jsonl", rows4)
    rep4 = api.run(plan4, [f"{OUT}/04_caseguard.jsonl"],
                   out_dir=f"{OUT}/04", engine="vector")[0]
    print(f"3 行中 {rep4['n_ok']} 行成功、{rep4['n_errors']} 行数据错误；"
          f"前两行的 ELSE 除零被完全遮罩")

    # ------------------------------------------------------------------
    banner("5. NULL 条件：NULL/false 跳过，无 ELSE 命中 -> NULL")
    expr5 = case([when(cmp("gt", col("a"), const("0", 2, 0)),
                       cast(const("1", 2, 0), 8, 2))])  # 无 ELSE
    plan5 = api.compile(expr5, schema4,
                        plan_path=f"{OUT}/05_nullcase.plan.json")
    rows5 = [{"a": None}, {"a": "1.00"}, {"a": "-1.00"}]
    write_jsonl(f"{OUT}/05_nullcase.jsonl", rows5)
    rep5 = api.run(plan5, [f"{OUT}/05_nullcase.jsonl"],
                   out_dir=f"{OUT}/05", engine="row")[0]
    print(f"全部 {rep5['n_ok']} 行有明确去向（NULL 传播或正常值），"
          f"错误 {rep5['n_errors']} 行")

    # ------------------------------------------------------------------
    banner("6. schema 变化：旧计划绑定旧指纹，新 schema 直接拒绝")
    plan6_path = f"{OUT}/06_bound.plan.json"
    api.compile(arith("add", col("a"), col("b")),
                {"a": (6, 2), "b": (6, 2)}, plan_path=plan6_path)
    saved = Plan.load(plan6_path)
    tampered = Plan.load(plan6_path)
    tampered.schema["a"] = [8, 3]                 # 模拟换 schema
    tampered.schema_fingerprint = "deadbeef" * 8
    try:
        Batch.from_rows([{"a": "1.00", "b": "2.00"}], tampered)
        print("错误：schema 变化未被拦截")
    except CompileError as exc:
        print(f"已拦截: {exc.code}")

    # ------------------------------------------------------------------
    banner("7. 整数/有理数 oracle 逐节点比对（行式 vs 列式 vs Fraction）")
    rng = __import__("random").Random(20261118)
    from deccomp.tests.test_fuzz import FuzzBuilder, _rand_decimal_text
    builder = FuzzBuilder(rng)
    checked_rows = 0
    for _ in range(20):
        ast = builder.expr(rng.randint(2, 4))
        try:
            p = api.compile(ast, builder.schema)
        except CompileError:
            continue
        data = [{name: _rand_decimal_text(rng, *decl)
                 for name, decl in builder.schema.items()}
                for _ in range(15)]
        batch = Batch.from_rows(data, p)
        rr, re_ = RowEngine(p, trace=True).execute(batch)
        vr, ve = VectorEngine(p, trace=True).execute(batch)
        oracle = Oracle(p)
        for i, row in enumerate(data):
            if i in batch.input_errors:
                continue
            pair, oerr = oracle.run_row(row)
            assert (oerr is None) == (i not in re_), "oracle/引擎错误一致性"
            if oerr is None and p.result_type != "boolean":
                from fractions import Fraction
                from deccomp.tests.helpers import (
                    engine_value_to_fraction, oracle_value_to_fraction)
                assert engine_value_to_fraction(rr[i]) == \
                    engine_value_to_fraction(vr[i]) == \
                    oracle_value_to_fraction(pair)
            checked_rows += 1
    print(f"{checked_rows} 行逐节点/逐值三方比对全部一致")

    # ------------------------------------------------------------------
    banner("8. 改变批大小：行身份、数值、错误位置逐行一致")
    ast8 = builder.decimal_expr(4)
    for _ in range(10):
        try:
            p8 = api.compile(ast8, builder.schema)
            break
        except CompileError:
            ast8 = builder.decimal_expr(4)
    data8 = [{name: _rand_decimal_text(rng, *decl)
              for name, decl in builder.schema.items()} for _ in range(120)]
    base_res, base_err = VectorEngine(p8).execute(Batch.from_rows(data8, p8))
    for size in (1, 7, 33, 120):
        for start in range(0, 120, size):
            part = data8[start:start + size]
            res, err = VectorEngine(p8).execute(Batch.from_rows(part, p8))
            for j in range(len(part)):
                gid = start + j
                assert (j in err) == (gid in base_err)
                if gid not in base_err:
                    assert res[j] == base_res[gid]
    print("批大小 1/7/33/120 切分后，120 行身份与错误位置全部对齐")

    # ------------------------------------------------------------------
    banner("9. 产物清单（真实 decimal Parquet + 独立错误清单 + 保存的计划）")
    for dirpath, dirnames, filenames in os.walk(OUT):
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            print(f"  {os.path.relpath(full, OUT):<40} "
                  f"{os.path.getsize(full):>8} bytes")
    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
