# 票联服务与运输收入事实仓（虚构规则，整数分）

Python 3.11，仅标准库；不连接任何真实航空或支付系统。两个系统使用**物理独立的 SQLite 库**，
只通过“完整票联切面 + 使用事件”对接：售票端无收入仓即可服务柜台，收入端不能改变票联资格。

## 运行

```bash
python3 -m airline                      # 8 个端到端场景（含全部指定案例）
PYTHONPATH=. python3 tests/test_edge.py # 边界/持久化/并发补充测试
```

## 模块

| 文件 | 职责 |
| --- | --- |
| `airline/allocation.py` | 纯函数整数分摊（最大余数法：下取整 → 余数降序 → coupon_id 定序），售票分摊与收入仓基数重算共用 |
| `airline/ticketing.py` | 票联服务：出票/使用/退票/重签、请求号幂等、状态机、仅追加日志与重放 |
| `airline/revenue.py` | 收入仓：切面版本化、使用证据、纯函数评估、冻结批次与可重读解释 |
| `airline/reconcile.py` | 独立对账：从切面重算运输/现金两条恒等式，不信任服务响应 |
| `airline/scenarios.py` | 交付入口编排（`python -m airline`） |

## 入口

- 售票：`TicketingService(path).issue_ticket / use_coupon / refund_coupon / reissue`
- 切面：`service.snapshot()`；日志：`service.export_journal()`；重放：`replay_journal(journal, path)`
- 证据：`RevenueWarehouse(path).import_snapshot(snap)`、`import_usage_event(eid, cid, seg)`
- 收入解释：`build_batch()`（生成冻结批次）、`explain_batch(id)`（重读旧批次）、`explain_current()`

## 关键规则落点

- **状态机**：`OPEN -> USED | REFUNDED | EXCHANGED`，每联至多一条终结记录（`coupon_terminations`
  主键约束），已乘坐航段不会被退票抹掉（USED 拒退，且终结不可逆）。
- **重签**：只原子终结所选 OPEN 联（条件 `UPDATE ... WHERE status='OPEN'`），其余旧联身份/状态/
  原票值不变；新票价 = 所选旧联原剩余票值 + 补收 − 退还，须非负；补收与退还不得同时为正；
  不做整票作废重建。`exchange_parents` 保留新联→全部父联的祖先链。
- **旧联不重新分摊**：每联 `original_value` 只在其出生的出票/重签中按当时权重分摊一次。
- **请求号幂等**：请求体做规范化 SHA-256 哈希；同号同文返回首次响应，同号异文抛 `RequestConflict`，
  幂等命中不占操作配额。
- **上限**：30 票、每票 6 联、100 次成功操作（构造器可注入，测试用）。

## 三个组织选择

### 1. 终态竞争：BEGIN IMMEDIATE + 条件 UPDATE，而非先查后判

每个写操作开 `BEGIN IMMEDIATE` 立即取库级写锁，两连接写事务严格串行；真正的裁决放在
`UPDATE coupons SET status=? WHERE coupon_id=? AND status='OPEN'` 的 `rowcount` 上。
后到事务在锁释放后读到的是已提交的新状态，条件不满足则 `rowcount=0`、整事务回滚，
因此“同时使用/退同一联”最多一个成功，且失败方得到明确业务异常而不是数据库约束错误。
失败业务不写请求日志（回滚），成功请求同时落 `requests`（幂等表）与 `journal`（仅追加日志）。

### 2. 旧切面：版本化不可变 + 批次冻结，晚到证据只产生新批次

- 切面按内容哈希去重后整体存为不可变版本（`snapshots.raw_json`），收入端永不回写票联；
- 批次绑定明确的双侧切面：`(snapshot_id, event_cutoff_seq)`，逐联决策、诊断、金额全部物化进
  `batch_items/batch_diagnostics`，重读只做反序列化，旧批次不随后续证据或新切面变化；
- 晚到使用证据到达后，先前 `MISSING_EVIDENCE` 的联在**新批次**里转为 `RECOGNIZED`，
  旧批次保持原样（场景 6 逐字段断言）。
- 证据规则：只认切面内 `USED` + 唯一且航段一致的证据；缺证据/航段不符/同联多证据冲突/
  证据指向切面外，分别落 `MISSING_EVIDENCE / SEGMENT_MISMATCH / DUPLICATE_CONFLICT /
  UNMATCHED_EVIDENCE` 诊断。“出票成功”永远不产生运输收入。

### 3. 分摊恢复：沿真实祖先链重算运输基数，补收不进运输收入

`compute_transport_bases`（纯函数）：

- 出生联基数 = 原票值（首次付款来源）；
- 重签新联基数 = 对“全部父联基数之和”按本次新联权重重新跑同一套整数分摊；
- 因此一条根票链上**存活联（OPEN/USED）基数守恒于首次出票票价**，补收/退还只改变现金票值，
  被排除在运输口径外，金额汇总中 `surcharges` 独立列示。

对账（`reconcile.py`）从切面独立重算两条恒等式：

```
运输口径：首次付款 = 存活联运输基数存量 + 退票退款 + 退票手续费 + 重签退还
现金口径：首次付款 + 补收 = 存活联现金票值存量 + 退票退款 + 退票手续费 + 重签退还
```

EXCHANGED 联的基数已转移给后代、REFUNDED 联已变成退款+手续费，均不计入存量，避免重复计算。

## 物化、重放与共享 SQLite

- 物化状态是服务的事实来源；`journal` 为仅追加请求日志，`replay_journal` 按序重放即可重建
  逐表一致的新库（请求号幂等使重复日志天然安全，墙钟时间戳不参与语义比对）。
- `:memory:` 使用共享缓存 URI（`file:...?mode=memory&cache=shared`）加保活连接，
  使每个操作仍可开独立短连接、保持与文件库相同的事务语义；生产形态直接传文件路径即可，
  多个柜台进程可对同一 SQLite 文件工作（`busy_timeout` + IMMEDIATE 串行化）。
