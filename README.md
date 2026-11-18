# 可保存的十进制表达式编译器与批量执行器

Python 3.11 实现。数值路径只允许 `decimal.Decimal` 与整数/`fractions.Fraction`，
**任何数值都不经过 binary float**（有 AST 静态守卫测试兜底）。

## 目录结构

```
deccomp/
  errors.py        # CompileError（计划级，拒绝整计划）/ DataError（行级，只拒该行）
  types.py         # decimal(p,s) 与加减乘除的 precision/scale/整数位推导
  decimals.py      # Decimal 内核：严格文本解析、节点级 half-even、溢出检查
  planner.py       # AST 校验、列引用、类型推导、计划 JSON 序列化、schema 指纹
  runtime.py       # 批次加载、schema 绑定、共享单节点标量语义
  row_engine.py    # 行式递归执行器（短路语义基准，可解释执行）
  vector_engine.py # 列式向量执行器（行掩码 + 按需扩张 memo + 可达性 trace）
  output.py        # decimal128 Parquet + 独立 JSON 错误清单 + 回读核对
  explain.py       # 计划解释树
  api.py           # compile / run / explain / verify 门面
  cli.py           # 命令行入口
  tests/           # unittest 套件 + 整数/Fraction 独立 oracle
demo.py            # 题面全部自生成案例的端到端演示
```

## 类型推导规则（题面公式）

| 运算 | result scale | 所需整数位 | 原始 precision |
|---|---|---|---|
| 加减 | `max(s1,s2)` | `max(p1-s1,p2-s2)+1` | `p1+p2` |
| 乘法 | `s1+s2` | `(p1-s1)+(p2-s2)+1` | `p1+p2` |
| 除法 | `max(6, s1+p2+1)` | `(p1-s1)+s2` | `p1+p2` |

结果 precision 至多 38：原始 precision 超限时 scale 优先向 0 收缩、整数位不削减；
所需整数位本身超过 38 则编译拒绝。每个实际求值节点按自身 scale 做
`ROUND_HALF_EVEN` 后再检查 precision 溢出，而不是只在最终输出舍入。

## 语义要点

* 输入按声明 `decimal(p,s)` 严格检查可表示性，不隐式舍入；非法文本/NaN/Inf
  只拒绝对应行。
* NULL 算术与比较返回 NULL；CASE 按序取第一个条件为 true 的分支，
  false/NULL 一律跳过；分支必须显式 CAST 到相同结果类型；无命中返回 NULL。
* 未选 CASE 分支不求值，其中的除零/溢出物理不可达。
* 计划内嵌 schema 指纹（SHA-256），换 schema 执行旧计划被直接拒绝。
* 行式与列式两套引擎共用节点语义；fuzz 测试以独立 Fraction oracle
  逐节点（不是只比最终总和）核对两者的值、scale、错误码与求值节点集合。

## 使用

```bash
pip install -r requirements.txt
python3 demo.py                       # 端到端演示，产物在 demo_out/
python3 -m deccomp.cli verify         # 41 个内置测试
python3 -m deccomp.cli compile --ast expr.json --schema schema.json --out plan.json
python3 -m deccomp.cli run --plan plan.json --batch b1.jsonl b2.jsonl --out out/ --engine vector
python3 -m deccomp.cli explain --plan plan.json
```

## 规模

内置规模测试固定 20 列、108 节点计划、10000 行，按 1/256/1000/3333/10000
五种批大小切分，逐行核对行身份、数值与错误位置，每批落真实 decimal128
Parquet 并重新读取核对物理类型与数值。
