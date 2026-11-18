# 受限 JSON 路径服务与可更新文档检索仓

两个独立调用入口：

| 入口 | 作用 | 命令 |
| --- | --- | --- |
| 路径引擎 | 对任意 JSON 输入运行结构化路径，不依赖仓库 | `python3 -m jsonpath_service` |
| 检索仓 | 持久化文档、修订、命名查询/模式、候选索引与命中证据 | `python3 -m docrepo` |

两者共用同一个路径引擎（`jsonpath_service/engine.py`）。检索仓不做第二套语义：
`search` 只把索引当作**候选**，对每个候选都调用同一引擎求精确命中。

```
jsonpath_service/     路径引擎（纯函数，无仓库依赖）
  engine.py           key / index / array_wildcard / object_wildcard / filter
  codec.py            路径与布尔表达式 <-> JSON（两个入口共用）
  __main__.py         CLI 入口
docrepo/
  repository.py       SQLite 事务、修订、命名查询/模式、索引、search/explain/scan
  common.py           标量编码与“安全等值条件”提取
  indexing.py         单遍 trie 标量提取（写入/建索引用）
  __main__.py         CLI 入口
tests/                引擎、仓库、端到端、CLI 测试（unittest）
benchmarks/           10000 × 100KiB × 50 查询基准 + 参考核验
examples/demo.py      可运行演示
data/                 持久库与基准报告产物
```

## 路径语义

路径是**结构化步骤数组**，不是 JSONPath 文本：

* `Key(name)`：对象成员；对象键保持原文，不做大小写转换。
* `Index(n)`：数组位置，负数从尾部计。
* `ArrayWildcard()`：数组每个元素，保持数组顺序。
* `ObjectWildcard()`：对象每个成员，按键的 Unicode 码点序。
* `Filter(expr)`：仅作用于当前数组元素。

每个步骤对**当前命中序列**逐项求值。wildcard 保留重复命中（同值出现多次即多条命中）。

**strict**：遇到类型错误 / 缺键 / 下标越界立即抛出 `PathError`，带
`reason`、`step_index`、`hit_path`、`hit_value_type`，可精确定位。
**lax**：只丢弃该失败分支；不自动拆数组，不把标量包装成数组。

**三种空/空值必须可区分**：

| 输入 | 表现 | 命中 |
| --- | --- | --- |
| 缺字段 `{}` | `a` 无命中，值“不存在” | `[]` |
| 显式 null `{"a": null}` | `a` 一条命中，值 `null` | `[("a", null)]` |
| 空数组 `{"a": []}` | `a` 一条命中（值是 `[]`）；`a[*]` 无命中 | `[("a", [])]` |

## filter 与三值逻辑

原子只有 `exists(relative_path)` 与 `eq(relative_path, scalar)`，可组合
`and` / `or` / `not`。

* 相对路径从**当前元素**开始，不能越出该元素（没有父级/根语法），
  支持 key、index（可为负）、wildcard。
* `eq` 只在相对路径**恰命中一个标量**时可比较：
  * 0 条命中 → `unknown`
  * 多条命中 → `unknown`
  * 恰一条但不是标量 → `unknown`
  * 标量但 JSON 类型不同 → `false`（布尔与整数不混同）
  * `null` 只与 `null` 相等
* `and` / `or` / `not` 采用三值逻辑；**只有 true 的元素被保留**。
* `exists` 只看是否命中，命中 `null` 也算 true。
* 条件只作用于单个元素，不同数组元素的条件不会被拼成一次匹配。

## 检索仓

* 稳定 `doc_id`，每文档递增 `revision`。
* `replace` / `delete` 在单个 `BEGIN IMMEDIATE` 事务内完成；
  `expected_revision` 做乐观并发控制，旧快照写入会抛 `StaleRevision` 且不改变任何数据。
* 命名查询（`register_query`）与命名模式（`register_schema`）持久化，关闭重开仍在。
* 标量等值候选索引（`index_equal`）：
  * 只为注册查询中**安全的 `eq` 合取条件**建原子；`and` 展开，`or`/`not`/`exists` 不建约束。
  * 原子形如 `prefix + array_wildcard + relative_path`（filter 对元素位置做存在量化）。
  * 每次写入**重建该文档的全部索引行**，删除时清除，因此字段删除不会留下旧标签。
  * **索引只减少候选集**：`search` 对每个候选调用同一引擎精确核验；
    索引缺失/过期只会退化为全扫，不会改变结果。
  * 命中证据（原路径）总是从当前 payload 重新计算，数组位置移动不会继承旧位置证据。
* `explain` 输出候选数、精确命中、索引计划与证据路径；`scan_all` 是无索引参考。

### 保守提取与 unknown 传播

`or`、`not`、`exists` 不产生候选约束；`eq` 的相对路径含通配符或负下标时也跳过。
这只会让候选集变大（更接近全扫），绝不会漏掉真实命中，因此 unknown 传播
与复杂谓词的候选完整性由“候选 ⊇ 真命中 + 精确复核”共同保证。
strict 模式为保证逐文档报告可定位错误，总是全扫（索引不影响其证据完整性）。

## 快速开始

```bash
# 路径引擎
python3 -m jsonpath_service \
  --doc '{"a":[{"x":1},{"y":2},{"x":null}]}' \
  --path '[{"key":"a"},{"wildcard":"array"},{"key":"x"}]'          # lax
python3 -m jsonpath_service --doc '{"a":[1]}' --path '[{"key":"a"},{"key":"b"}]' --strict

# 检索仓
DB=/tmp/demo.db
python3 -m docrepo replace --db $DB --doc-id d1 \
  --doc '{"items":[{"kind":"alpha","n":1},{"kind":"beta"}]}'
python3 -m docrepo register-query --db $DB --name alpha \
  --path '[{"key":"items"},{"filter":{"eq":[[{"key":"kind"}],"alpha"]}}]'
python3 -m docrepo register-schema --db $DB --name customer --definition '{"required":["items"]}'
python3 -m docrepo ensure-indexes --db $DB
python3 -m docrepo search  --db $DB --name alpha
python3 -m docrepo explain --db $DB --name alpha --compare-scan
python3 -m docrepo scan    --db $DB --path '[{"key":"items"},{"wildcard":"array"}]'
python3 -m docrepo delete  --db $DB --doc-id d1 --expected-revision 1
```

## 测试与基准

```bash
python3 -m unittest discover -s tests -t .      # 单元 + 端到端 + CLI
python3 -m examples.demo                        # 可运行演示

# 10000 文档 × ~100KiB、50 查询、深度 20
python3 -m benchmarks.run_benchmark --rebuild
```

基准对每个查询比较**完整结果**（doc_id、revision、原路径、值）的 SHA-256 摘要，
并比较 strict 错误集合，而不是只比条数；参考值由同一引擎一次性逐文档全扫得到，
缓存于 `data/reference_digests.json`（按文档内容与查询定义的指纹失效）。
产物：`data/benchmark.db`、`data/benchmark_report.json`。

## 规模与复杂度

* 文档：10000 个，每个约 100 KiB。
* 查询：50 个命名查询，含深度 20 的链式路径。
* 单遍 trie 提取让每次写入只遍历文档一次即可更新全部已知原子。
* 索引原子按查询需求惰性构建；未就绪时自动全扫，保证正确性优先。
