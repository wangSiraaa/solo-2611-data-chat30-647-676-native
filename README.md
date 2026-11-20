# 酒店房态变更后的跨夜排房与可解释履约仓

Python 3.11 + SQLite + OR-Tools CP-SAT（`ortools>=9.15`）。两个可独立调用的模块：

| 模块 | 职责 | 入口 |
|---|---|---|
| `hotel/state.py` | 房态服务：房间、维修版本、不可变房态产品 | `RoomStateService` |
| `hotel/bookings.py` | 预订仓：修订、撤销、不可变预订切面 | `BookingStore` |
| `hotel/fulfilment.py` | 履约仓：消费明确的产品+切面，优化、发布、解释 | `Fulfilment` |
| `hotel/orchestration.py` | 可选门面，含“变更后自动发新方案”的策略 | `Hotel` |
| `hotel/oracle.py` | 独立穷举最优，仅供测试/demo | `brute_force` |

两个业务模块只共享 `hotel/db.py` 的连接与事务；可以传同一个 `Database` 实现共事务，
也可以各自重开同一文件（demo/test 均有覆盖）。

## 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests -q       # 34 项测试
.venv/bin/python demo.py                  # 生成 results/
```

## 关键语义

**房态（独立于预订）**

- 房间登记 `room_id / 等级 / 人数上限 / 整数清洁天数`，属性固定；完全相同重登记为幂等。
- 维修记录 `maintenance_id` + 正整数修订：`add / replace / revoke`。
  - 同版同文重投：不写库、返回原 `committed_version`，`replayed=true`。
  - 同版异文：`RevisionConflict`。旧版（低于 head）：`StaleRevision`。
  - `revoke` 为终态，之后任何 `add/replace` 均 `RecordRevoked`，旧版不能复活。
- 每次成功变更推进一个全局 `state version`。`product(version)` 把该版本下
  “每房间合并后的维修阻断区间 + 固定属性”持久化为不可变产品（数据库触发器禁止
  UPDATE/DELETE）。`product(v).available_intervals(room, start, end)` 可查任意子窗口，
  之后的新维修不会改变旧产品。
- 所有区间左闭右开，维修与入住共用同一时间线。

**预订仓**

- 追加式事件表 + head 表。`(booking_id, revision)` 同文幂等、异文冲突；
  低于 head 的修订拒绝；`cancelled` 后不可复活。
- `snapshot(watermark)` 生成不可变 `BookingSlice`，记录按 `booking_id` 排序；
  相同的 seq watermark 永远得到相同切面。

**履约（明确消费输入）**

- `publish(product, slice)` 只读传入的两个对象，绝不触碰最新房态表；
  产品与切面的 JSON 与 digest 固化进 `plans` 行，`plans / plan_assignments /
  plan_unsatisfied` 均有防更新触发器。
- 合法性：整段一间房（不拆分）；人数 ≤ 房间人数上限；等级在
  `[min_level, max_level]`；入住区间与维修无交集；同房相邻两单满足
  `前单离店 + 清洁天数 ≤ 后单到店`。
- 目标严格按字典序：①最大化满足预订数；②最小化升级等级差总和；
  ③按 `booking_id` 升序的 `room_id` 向量字典序最小，未满足排在所有房号之后。
  第③步逐单用 CP-SAT 求最优并固定，输入顺序不影响结果（测试中反转输入验证）。
- 发布前 `_verify` 会用同一产品重放所有约束，并校验每条升级差；
  违约则 `ProductMismatch`，不会发布一个“借用了最新总量”的方案。

**解释与日历**

- `plan.explain(id)`：满足时给出房间/等级/升级差/容量/维修与原因；
  未满足区分 `no_legal_room`（并给出每个房间被容量/等级/horizon/维修拒绝的列表）
  与 `competition`（列出冲突的预订）。
- `plan.room_calendar(room)`：房间属性、维修、已排预订、清洁窗口、物理空闲区间。

## 场景覆盖（见 `demo.py` 与测试）

| 场景 | 位置 |
|---|---|
| 每晚容量够但无连续房间 | `test_daily_capacity_...`, gallery 1 |
| 维修结束边界（左闭右开） | `test_maintenance_end_boundary_is_half_open`, gallery 2 |
| 清洁间隔 | `test_cleaning_days_...`, gallery 3 |
| 升级抢占 / 保套房 | `test_upgrade_preemption_...`, gallery 4 |
| 同价多解 + 输入顺序无关 | `test_equal_cost_ties_...`, gallery 5 |
| 维修撤销 | gallery 6, `test_products_are_immutable...` |
| 明确消费旧产品 | `test_old_product_is_actually_consumed...`, gallery 7 |
| 4 房 6 单穷举核最优 | `test_solver_matches_exhaustive_oracle`（4 组固定 + 25 组随机） |
| 重开库旧方案不变 | `test_plan_survives_database_reopen` |
| 维修变更驱动无关预订换房 | `test_maintenance_change_drives...`；demo 中 R01[5,8) 令 BK08/10/11/14/16 换房 |

## Demo 规模与产物

- 12 房（1–3 级、容量 2–4、清洁 0–2 天）、20 笔预订、30 天。
- 首方案 20/20 满足、升级和 2；对 R01 增加 `[5,8)` 维修后重算仍 20/20，
  但 6 笔预订移动，其中 5 笔自身入住区间与维修完全不相交（全局重优化，非首晚检查）。
- `results/report.json`：场景、两次方案摘要、移动明细、旧方案重开一致性、
  穷举对比、修订规则结果、落库计数。
- `results/demo_output.txt`：完整运行日志。
- `results/hotel.sqlite3`：可直接用 `sqlite3` 检查历史方案与产品。
