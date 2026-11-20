# 供应商阶梯采购承诺与门店到货短缺的订单分配

Python 3.11、零第三方依赖（标准库 + SQLite）实现两个各自持有原始状态的子系统：

| 子系统 | 模块 | 原始状态 |
| --- | --- | --- |
| 供应商报价承诺服务 | `ordercommit/supplier.py` | 规则版本、不可变报价、已接受供货承诺 |
| 门店到货分配仓 | `ordercommit/warehouse.py` | 真实承诺、真实到货（绑定承诺）、只追加的发放记录 |

分配仓**只消费真实承诺与真实到货**，绝不从报价推导"已收到实物"；中央库存不是独立账本，
而是从 `到货 - 发放` 原始记录独立重算。

## 目录

```
ordercommit/
  errors.py          业务异常（稳定错误码）
  schema.py          两套独立 SQLite DDL
  pricing.py         全量阶梯价与包装/最低量/上限纯函数
  supplier.py        报价服务：登记、报价、接受、接受时额度重核
  planner.py         小数量全量穷举 + 逐 SKU 子问题缓存
  execution.py       计划 → 真实不可变报价 → 接受 → 承诺 → 分配仓登记
  warehouse.py       需求/到货/优先级分配/更正取消/库存稽核
  audit.py           裸 SQL 独立稽核（不经过仓库类任何方法）
  synthetic_data.py  合成数据（3 供应商 / 5 SKU / 20 需求）
  scenario.py        八个规定案例端到端编排
  __main__.py        CLI：quote/accept/plan/receive/add-demand/stock/explain/audit/demo
tests/               41 个 unittest（含双连接并发防超发、朴素枚举交叉验证、篡改稽核）
```

## 快速开始

```bash
python3 -m ordercommit demo --out data/run        # 建库、跑全部案例、出 JSON
python3 -m unittest discover -s tests -v          # 41 个测试
python3 -m ordercommit audit --warehouse-db data/run/warehouse.db   # 裸 SQL 守恒稽核
python3 -m ordercommit export-data                # 合成输入落 data/synthetic_input.json
```

`data/run/` 产出：`supplier.db`、`warehouse.db`、`scenario_results.json`、`synthetic_input.json`。

单步 CLI（在 demo 产出的库上）：

```bash
python3 -m ordercommit quote   --supplier-db data/run/supplier.db \
    --supplier S1 --request-no R1 --item SKU-A=8 --at 2026-11-15T10:00:00
python3 -m ordercommit accept  --supplier-db data/run/supplier.db \
    --quote Q-R1 --commitment C-R1 --at 2026-11-15T10:00:00
python3 -m ordercommit receive --warehouse-db data/run/warehouse.db \
    --receipt R99 --commitment C-MAIN-S1 --item SKU-A=2
python3 -m ordercommit stock   --warehouse-db data/run/warehouse.db
python3 -m ordercommit explain --warehouse-db data/run/warehouse.db --kind receipt --key R-01
```

## 关键业务规则落地

### 报价服务

* **全量阶梯价**：采购总量落在哪档，**全部件数**按该档单价计费（`pricing.SkuRule`），
  不是分段累进；零采购不收费用，正采购须同时满足包装倍数、最低量、可供上限。
* **完整规则版本**：每个版本带 `[effective_from, effective_to)` 时刻区间，报价快照引用版本，
  报价落库后规则再变也不影响已出报价。
* **请求号幂等**：请求体做规范化 SHA-256（与行顺序无关）。同号同文返回原报价；
  同号异文抛 `DUPLICATE_REQUEST`。
* **接受才产生承诺**：`OPEN` 报价在接受事务内重新核对三件事——窗口未过期、报价仍 OPEN、
  同版本下全部已接受承诺的累计件数 + 本次 ≤ 可供上限。任一不满足整体回滚，
  **不产生任何承诺**；过期接受抛 `QUOTE_EXPIRED`，额度不足抛 `OVER_CAPACITY`，
  凭旧报价也不能超卖。
* 报价服务不感知门店需求，采购员可直接用 `quote/accept`。

### 采购计划（穷举）

目标字典严格按序：**可行（覆盖需求且不超逐 SKU 超买上限）→ 真实总报价最小 →
超买件数最小 → 按 `(供应商ID, SKU)` 排序的数量向量字典序定序**。

* 每个 `(SKU, 需求量, 超买上限)` 子问题枚举跨供应商的全部合法包装量组合并 `lru_cache`；
* 跨 SKU 做笛卡尔积，用 `(成本, 超买, 向量)` 三元组一次比较完成三层优选；
* 无可行解时抛 `INFEASIBLE_PLAN` 并带逐 SKU 缺口（最大可购量、缺口数、各供应商候选网格），
  **不拿拆开的单价拼出从未得到的承诺**；
* `tests/test_planner.py` 用一份独立的朴素全枚举交叉验证最优解。
* 真实成本只取自执行阶段供应商返回的不可变报价/承诺；`actual_cost == estimated_cost`
  作为结果字段输出，但二者来源不同。

### 分配仓

* 到货必须绑定已登记的真实承诺；同一 `receipt_id` 全仓只处理一次（重送抛
  `RECEIPT_ALREADY_PROCESSED`）；同 SKU 累计到货不得超过承诺数量。
* 分配在单事务内完成，排序键：**优先级降序 → 最迟收货日升序 → 需求 ID**；
  同需求可部分到货；实物在到货内按 `(收货时刻, receipt_id)` FIFO 出，
  保证同一箱实物不可能同时进两个门店。未发实物入中央库存。
* `w_allocations` 只追加、从不改写删除；已完成发放不会被新需求/新报价重算撤销。
* 更正需求：已发数量保留，新数量不得低于已发量，只对未发库存重排；
  取消需求：已发实物保留，**不会凭空退回供应商**。
* 并发：所有写事务 `BEGIN IMMEDIATE`，需求台账用条件更新
  `allocated_qty + n <= demand.qty`（版本条件更新），两个连接争同一库存时后者阻塞后
  读到已提交状态，天然不超发；`tests` 中有两个真实多线程连接争抢用例。

## 八个规定案例（`scenario_results.json`）

1. **多买反而便宜**：SKU-A 买 8 件 96 元、买 10 件 80 元；同时验证同号复读/异文拒绝。
2. **包装使需求不能恰好满足**：SKU-B 需求 8 件，供应商包装 5/3/4，穷举强制超买 1 件
   （9 件 = 72 元）凑整。
3. **低价过期**：早鸟版本 70 元的报价过期后接受被拒且无承诺；活动版本切换后同数量
   回升到 130 元。
4. **两门店争短缺**：SKU-D 承诺 16 件只到 8 件，优先级 5 的 D09/D10 先各得 4 件，
   D11/D12/D20 全部欠着，无中央余额可挪用。
5. **到货重送**：同 receipt_id 第二次送达被拒；累计到货超承诺数量被拒；同承诺多张
   合法到货可累计但不超量。
6. **更正/取消 + 守恒**：更正保留已发量、拒绝改到低于已发量；取消不退回实物；
   仓库类稽核与裸 SQL 独立稽核同时通过。
7. **无可行计划**：构造超过总可供的需求，返回逐 SKU 缺口而非伪造承诺。
8. **不超卖**：第一张报价吃掉全部额度后，第二张本身合法的报价在接受时被
   `OVER_CAPACITY` 拒绝。

## 设计取舍

* **全量搜索 vs MILP**：题面规模（3×5、每 SKU ≤30 件）下合法采购量是包装倍数的稀疏网格，
  逐 SKU 候选极少，全量枚举可完整复现、可解释，且用独立朴素枚举测试兜底；
  候选数随供应商数指数增长，更大规模应换动态规划或 MILP 求解器。
* **子问题缓存**：`lru_cache` 键只依赖不可变目录快照 `(供应商, SKU, 上界)`，
  重复规划不重算网格；接受时仍以数据库实时额度为准，缓存不参与额度裁决。
* **库存串行事务 vs 乐观锁**：SQLite 单库多连接下 `BEGIN IMMEDIATE` 把写事务串行化，
  配合台账条件更新做最后一道版本条件防线，吞吐让位于正确性与可审计性；
  所有库存事实都是只追加的原始记录，中央余额随时可重算、可交叉验证。
* **两库分离**：报价服务与分配仓各自独立 SQLite 文件、独立表前缀（`s_` / `w_`），
  分配仓侧的承诺只是外部事实登记，不回写报价服务，杜绝从报价造实物。
