# 可替代工艺路线目录 + 在制品执行事件事实仓

独立工艺路线目录（有限状态机）与执行事件事实仓，Python 3.11 标准库实现，SQLite 持久化，
不控制任何真实生产设备。全部代码在 `wip/`。

## 运行

```bash
# 目录/仓库自测（42 个用例，含题面全部案例）
python3 -m unittest wip.test_wip -v

# 生成合成输入 + 全流程演示，输出到 wip/demo_out/
python3 -m wip.demo --out wip/demo_out

# CLI（目录编辑/发布、批事件导入、查询、迁移、验证、重开）
python3 -m wip.cli --db wip/run.db init
python3 -m wip.cli --db wip/run.db catalog save --file product.json --publish
python3 -m wip.cli --db wip/run.db route R v1 --steps t_mach_a,t_asm_a,t_insp --state S0
python3 -m wip.cli --db wip/run.db unit create --unit U1 --route R --version v1
python3 -m wip.cli --db wip/run.db events import --file events.json
python3 -m wip.cli --db wip/run.db unit show U1
python3 -m wip.cli --db wip/run.db unit explain U1
python3 -m wip.cli --db wip/run.db unit explain-start U1 --step t_asm_a --device dev_asm --time 10
python3 -m wip.cli --db wip/run.db unit migrate U1 --route R --version v2 --map Q=Q2
python3 -m wip.cli --db wip/run.db device show dev_cnc
python3 -m wip.cli --db wip/run.db summary
```

## 模块

| 文件 | 职责 |
|------|------|
| `wip/route.py` | 纯内存路线目录 `RouteDefinition` / `RouteCatalog`：状态机校验、合法序列、当前状态、最短剩余工时（Dijkstra，支持回路/分支/多合格终态，报废终态不可完工）。 |
| `wip/catalog_db.py` | 目录的 SQLite 持久化（版本不可变、草稿/发布、设备种类），以及 `export_product()` 导出同格式产品字典。 |
| `wip/warehouse.py` | 执行事件事实仓：日志 + 派生投影、批导入原子性、设备占用、路线绑定、迁移、计数、查询、解释。 |
| `wip/cli.py` | 全部入口的 CLI。 |
| `wip/demo.py` | 确定性合成输入与端到端演示。 |
| `wip/test_wip.py` | 42 个 `unittest` 用例。 |

## 关键语义

### 路线目录（独立可用）
- 状态：`INITIAL`（唯一）/`NORMAL`/`ACCEPTED`（可多个）/`SCRAP`（可多个）。
- 每个转换有稳定 `step_id`、起终状态、设备种类、非负整数标准耗时；允许回路、分支。
- `validate_sequence(step_ids)`：从初始状态出发，非确定地跟踪可达状态集合；序列结束必须
  能到达任一合格终态才算合法。**报废终态不计为合格完工**。
- `current_states()`、`shortest_remaining()` / `shortest_remaining_path()`：无路径返回 `None`
  （不可完工）；报废终态返回 `None`。
- 上限：50 状态、100 转换（违反直接报错）。

### 执行仓
- **事件顺序**：批内按每条事件给定的 `acceptance_seq` 排序执行，绝不按业务时刻 `time`
  重排；`acceptance_seq` 必须全批唯一且大于历史最大值。输入列表本身可乱序。
- **原子性**：先克隆投影做完整预验证，任一条非法则整批拒绝，业务投影不变，返回
  `{code, message, index, acceptance_seq, details}` 并记录到 `exec_batches`。
- **START 准入**：物料存在、非终态、无在途尝试、`step_id` 存在于当前绑定产品、
  `step.from_state == 当前状态`、设备已登记且种类匹配、设备 `[t, t+标准耗时)`
  无重叠。
- **FINISH**：只作用于在途尝试；`time >= START`；按该尝试自己记录的路线版本解释；
  以实际 `[START, END)` 复核设备重叠。
- **CANCEL**：释放设备与在途占位，不移动物料状态；取消前的实际占用区间仍参与历史重叠校验。
- **设备区间**：`[START, END)`，完工与下一开始同秒允许；标准耗时为 0 的工序不占用区间。
- **绑定与迁移**：创建时绑定产品快照；`migrate_unit(mapping)` 要求无在途尝试、当前状态
  在映射中、目标状态存在于新产品；迁移追加新绑定，后续工步用新产品，旧的已完成
  尝试仍记录旧版本。目录编辑不改变历史。
- **计数**：`accepted_count` 按物料件数计，返工回流不重复；合格/报废终态后拒绝新工步。
- **事实查询**：`device_busy`/`device_timeline`/`device_fact`、`unit_position`、
  `duration_facts`（实际加工/等待时长）、`accepted_count`/`scrap_count`/`summary`。
- **解释**：`explain_start`（为何不允许开工、哪个尝试占住设备）、`explain_unit`
  （跨版本绑定旅程与尝试历史）。
- **持久化**：`exec_journal` 保存全部操作；重开数据库时重放日志重建投影，
  `exec_units`/`exec_attempts`/`exec_bindings`/`exec_events` 为派生表。

### 执行仓与目录的边界
执行仓只接受与 `RouteDefinition.to_dict()` 同格式的普通字典（`register_product` /
`create_unit(product=...)` / `migrate_unit(product=...)`），从不读取 `cat_routes`。
两者可使用同一个 SQLite 文件也可以分开，见 `test_external_product_without_catalog`。

## 题面案例对照（`wip/test_wip.py`）
- 替代路线：`RouteCatalogTests.test_alternative_routes_validate`、`test_multiple_accepted_terminal_states`
- 合法返工：`test_rework_loop_is_legal`、`test_rework_does_not_double_count`
- 设备同秒交接：`test_device_busy_interval_semantics`、`test_zero_duration_allows_same_instant`
- 乱序 FINISH：`test_out_of_order_finish_by_business_time`
- 取消后重试：`test_cancel_releases_and_retry`、`test_cancelled_attempt_cannot_finish`
- 迁移后旧工步拒绝：`MigrationTests.test_migration_changes_admission_and_forecast`、
  `test_old_attempt_finishes_on_old_product_after_migration`
- 整批原子/解释/设备占用：`test_batch_is_atomic`、`test_device_overlap_rejected`、
  `QueryAndExplainTests.test_explain_start_reports_occupying_attempt`
- 重开数据库：`PersistenceTests`
