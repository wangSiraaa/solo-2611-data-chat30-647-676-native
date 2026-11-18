# 多范围值服务与整数预约数据仓

从空项目实现的 Python 3.11 工程：可独立调用的整数多范围代数，以及基于
SQLite 的真实事务预约仓库。开闭端点、相邻段、空洞和同一集合的不同表示都由
规范化统一处理。

## 目录

```
multi_range_reservation/
  ranges.py       # 规范化、并、交、差、成员、相交；无数据库依赖
  repository.py   # SQLite 预约仓：create/replace/cancel/query/verify
  cli.py          # range / reserve / replace / cancel / query / verify 入口
tests/
  test_ranges.py      # 规范形式 + 小整数域枚举核对
  test_repository.py  # 事务、请求身份、替换、取消、并发、verify
  test_model.py       # 集合模型随机对照
  test_stress.py      # 多随机种子压力 + 重开校验
  test_cli.py         # 端到端命令行
```

## 快速开始

```bash
python3 -m unittest discover -s tests -t .      # 全部测试（无需第三方库）

# 独立范围代数（不触碰数据库）
python3 -m multi_range_reservation.cli range normalize --value '"(1,3]"'
python3 -m multi_range_reservation.cli range union '"[1,2)"' '"[2,3)"'
python3 -m multi_range_reservation.cli range difference --left '"[1,8)"' --right '"[2,3) [4,5)"'
python3 -m multi_range_reservation.cli range contains --value '"[1,5)"' --point 4

# 预约仓库
python3 -m multi_range_reservation.cli reserve --db r.sqlite \
    --request-id q1 --resource room --reservation-id b1 --owner alice --ranges '"[1,5)"'
python3 -m multi_range_reservation.cli query --db r.sqlite --resource room --ranges '"[3,8)"'
python3 -m multi_range_reservation.cli replace --db r.sqlite \
    --request-id q2 --resource room --reservation-id b1 --owner alice --ranges '"[5,8)"'
python3 -m multi_range_reservation.cli cancel --db r.sqlite \
    --request-id q3 --resource room --reservation-id b1
python3 -m multi_range_reservation.cli verify --db r.sqlite
```

退出码：`0` 成功；`2` 被拒绝（冲突、校验失败、请求身份不符等）；
`query` 有冲突时返回 `3`；`verify` 失败时返回 `1`。输出均为 JSON。

## 取值表示

规范值是无空段、升序、互不相邻的半开区间元组 `[[lo, hi), ...]`，含义即其中
包含的整数集合。正因为合并了相邻段，`[1,2) ∪ [3,4)` 不会被误当成 `[1,4)`
（整数 2 是空洞），而同一集合的不同输入分段会得到相等结果。

输入可以是：

* 紧凑文本 `"[1,3) (5,8]"`（`∅` 表示空值）；
* 段对象列表，`lower`/`upper` 加开闭标志（默认两端都包含）；
* 2 元组表示闭区间，4 元组 `(lo, hi, lo_inclusive, hi_inclusive)`；
* `{"half_open": [[lo, hi], ...]}`（`--half-open` 亦可）；
* 另一个规范值。

端点限于 `[-1000000, 1000000]`。空段（如 `[4,4)`、`(2,3)`、倒置端点）合法但
不贡献成员。唯一例外是规范化输出的独占上界允许取 `1000001`，以便表示
`[..., 1000000]`。

## 关键设计取舍

**冲突检查用重扫，不用辅助区间索引。** 每次写入在同一个
`BEGIN IMMEDIATE` 事务里，重扫该资源所有 active 行并逐段求交。规模上限
（≤50 资源、≤1000 预约、每值 ≤20 段）下代价很低；更重要的是它与输入分段
无关，不会出现“索引与规范区间不一致导致误判”。替换的更新代价也与重扫对称：
一次 UPDATE 而非“删旧行 + 重建索引”。若将来要加索引，必须在同一事务内由
规范区间重建，才能保持这里的等价性与并发边界。

**replace 不先删旧行。** 先按“本次最终占用（排除自身旧行）”检查，通过后只用
一条 UPDATE 覆盖。不存在旧占用已释放、新占用未写入的可提交或可见窗口；测试里
用第二个连接在检查后、UPDATE 前尝试 `BEGIN IMMEDIATE`，确认被锁并看到旧行。

**请求身份。** 成功请求保存规范化请求指纹与完整回执 JSON。相同 request_id 且
规范化后相同请求原样复读回执；同 id 异文（含跨动作、仅分段不同但集合相同以外
的任何差异）一律拒绝。失败会回滚回执插入，因此不占用成功身份，修正后可复用
同一 id。

**仓库消费范围服务产生的结果。** 写入时以服务 `normalize` 的产出为准并落库；
读取时按落库的规范段重建集合，代数作用于重建值。注入实现的服务若改变规范化
规则（例如错误地包含上端点、填补空洞、丢掉末点），预约的放行/拒绝结果会随之
真实改变——测试分别覆盖了“错误放行”和“错误拒绝”两个方向。

**取消保留身份。** 取消只把状态置为 `cancelled` 并释放占用，owner 与占用值都
保留；同一 `reservation_id` 不可再用 create 重建，但时间点可被新的预约占用。
`reservation_id` 是全局身份（跨资源唯一）。

**空占用拒绝。** create/replace 的空规范值在进入事务前即报错，不写任何行。

## 查询

`query(resource, ranges)` 返回该资源所有 active 预约中与查询集合实际相交的
身份、各自完整占用及**实际交集**，而不是只给布尔值：

```json
{
  "resource": "room",
  "query": {"half_open": [[3, 8]], "text": "[3, 8)"},
  "conflict_count": 1,
  "available": false,
  "conflicts": [
    {"reservation_id": "b1", "owner": "alice",
     "occupation": {"half_open": [[1, 5]], "text": "[1, 5)"},
     "intersection": {"half_open": [[3, 5]], "text": "[3, 5)"}}
  ]
}
```

## 并发

两连接争同一最后可用整数点时，`BEGIN IMMEDIATE` 使检查与写入串行化，至多一方
成功，另一方得到 `OccupancyConflict`，不存在“先提交冲突再异步修复”。测试覆盖
线程和独立进程两种竞争，并在竞争后 `verify` 全库不变量。

## verify

`verify()` 在只读事务中检查：每行占用可解析且规范；active 行非空；同资源 active
行两两不相交；回执指纹与请求一致、动作/身份一致且引用已存在预约；`next_seq`
单调。返回 `{"ok": bool, "errors": [...], "info": {...}}`。

## 测试覆盖

* 代数：开闭端点等价 `(1,3] == [2,4)`、相邻段合并、空洞保留、乱序/重叠输入、
  同一集合多种表示、空段、端点边界；小整数域枚举核对并/交/差/成员/相交。
* 仓库：空洞不冲突、开闭等价占用、不同分段同一集合、替换跨过自身旧区间、
  按最终占用检查、取消后重用、全局身份、空占用拒绝、回执复读与异文拒绝、
  失败无部分写入、重开数据库状态保持。
* 随机：集合模型逐步对照（含查询交集、状态、回执），多随机种子压力，均在最后
  重开数据库核对成功记录。
