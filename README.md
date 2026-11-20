# 多区室通风产品与示踪气体暴露仓 (VentNet)

本仓库实现一个**多区室通风网络目录**和一个**示踪气体观测仓**，全部数据为合成，
仅用于数据工程与数值方法演示，**不用于健康、安全或建筑合规判断**，也不反演真实
通风量、不拟合真实人员风险。

实现使用 Python 3.11、NumPy、SciPy 与 SQLite。核心思想：每个房间的浓度方程是
**线性、分段常源**的常微分方程组，因此可用**增广矩阵指数**精确推进，并**精确积分**
每个人员访问区间内的浓度-时间积分（而非端点梯形）。

## 安装 / 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # numpy, scipy

.venv/bin/python -m unittest -v tests.test_ventnet   # 33 个单元测试
.venv/bin/python -m ventnet.cli verify               # 端到端 + 解析核对
.venv/bin/python -m ventnet.cli demo                 # 跑全部合成场景
.venv/bin/python -m ventnet.scenarios_out examples   # 生成示例 JSON
```

## 模型

每个房间 `i` 有正体积 `V_i` (m³)，有向房间间流量 `q_ij` (m³/s)，以及与室外的
送风/排风。目录发布时要求**每房间总入流 = 总出流**（精确有理数比较），否则拒绝
发布。室外浓度恒为 0。浓度动力学：

```
V_i dc_i/dt = s_i + Σ_j q_ji c_j − c_i (Σ_j q_ij + q_i,out)
c_i(0) ≥ 0,  s_i ≥ 0
```

## 目录产品（`ventnet.catalog`）

* `Catalog.add_room / add_supply / add_exhaust / add_transfer`，支持 `"1/3"`、
  `"0.5"` 等**精确有理数**（内部用 `fractions.Fraction`）。
* `publish()` 校验平衡并冻结；`publish` 失败抛出 `BalanceError`。
* 身份为内容的 SHA-256（`catalog_identity`），可复算。
* `matrices()` 给出数值矩阵、体积、边、出口边；`to_dict()/from_product()`
  支持与外部**同格式**矩阵产品互操作。
* `revise_flow(...)` / `revise_flows(...)` 生成**新修订**（不修改原对象）。
  单条非对称改流通常会破坏平衡，故提供原子多条修订（例如同时改双向交换）。
* `ventnet.transfer.transfer_response()` 独立提供**传递响应**（`exp(Kt)`、源算子、
  出口算子），不依赖观测仓。

## 观测仓（`ventnet.experiment` / `ventnet.db`）

一个实验包含：每房间初始浓度、源开关时刻与源强、分析窗口、人员 `visit` 记录。

* 源在**半开区间** `[t_k, t_{k+1})` 内恒定；**同秒多个源变更合并为一组新状态**，
  不会因输入顺序产生零长段；同一房间同秒冲突赋强直接拒绝。
* 人员记录以 `visit_id` 给房间与半开停留区间；同一人可多次到访，但**不得同时位于
  两个房间**（重叠或重复即整批 `ConflictError`），端点相接允许。
* 计算完整浓度过程、**每个 visit 的浓度-时间积分**、**每人累计积分**。
* 整个窗口质量核对：`末存量 − 初存量 = 源输入 − 室外排出`；内部房间流量**互相抵消**，
  不作为损失重复扣除。

### 数值方法

* 状态增广为 `x = [c, M, 1]`（`M` 为各房间累计室外排出质量），段内为仿射自治线性
  系统，`x(t+dt) = exp(B dt) x(t)`。
* 浓度-时间积分用**双增广矩阵** `Q = [[B,0],[I,0]]`，`exp(Q dt)` 的右下块即
  `∫ exp(Bs) ds`，因此积分是解析的，不把端点均值当积分。
* **自适应细分**：比较一步与两步的结果，超过 `atol=1e-7` 则细分（受 `max_depth`
  限制），记录误差估计。
* 不需求逆：**零流量、闭合连通分量、奇异 `K`** 都能处理（如两室仅互通的闭合网络）。

### 修订与持久化

* 修订一条风量并保持平衡后，新实验的邻室曲线与暴露积分会改变；**旧实验保留旧依据**
  （结果记录 `basis_catalog_identity`，并指向其原始目录修订）。
* 源事件与人员访问各自独立存表（`source_events`、`visits`），结果不是唯一数据副本。
* 目录按身份去重存储；`verify_catalog` 重算身份确保内容未被篡改。

## 解释

每个 visit 的解释记录**使用了哪些时段与哪些房间状态**：
`segments_used` 给出覆盖该区间的各段起止、时长与当时的源强；
`room_states_at_segment_start` 给出各段起点的各房间浓度；
并注明积分方法为增广矩阵指数的解析积分。

## CLI

```bash
# 校验 / 发布目录产品
python -m ventnet.cli catalog-validate examples/two_room_exchange.catalog.json
python -m ventnet.cli catalog-publish  examples/two_room_exchange.catalog.json --out out.json

# 导入实验（只持久化输入）、运行、查看解释
python -m ventnet.cli import  --db run.db --catalog C.json --experiment E.json
python -m ventnet.cli run     --db run.db --experiment-identity <sha256:...>
python -m ventnet.cli explain --db run.db --experiment-identity <sha256:...>

# 验证与演示
python -m ventnet.cli verify
python -m ventnet.cli demo
```

## 示例场景（`ventnet.examples`）

| 场景 | 覆盖点 |
|------|--------|
| `two_room_exchange` | 两室交换、邻室暴露 |
| `closed_zero_exhaust` | 闭合零出口、奇异系统、总量守恒 |
| `piecewise_source` | 分段源（开/关/开），半开区间 |
| `boundary_flow` | 送风/排风边界进出 |
| `legit_flow_correction` | 合法平衡风量更正，改变邻室曲线/积分，保留旧依据 |

## 独立核对

`ventnet.verify` 对每个场景检查质量守恒与身份复算，并做**独立解析核对**：

* 单室：闭式指数解（浓度与积分）；
* 两室对称：特征分解闭式解（浓度与积分）；
* 闭合两室：总示踪质量守恒；
* 边界流两室：独立 RK4 步进解核对末态。

## 限制

房间 ≤ 8、源变更时刻 ≤ 50、人员访问 ≤ 100、总时长 ≤ 10000 秒；超限抛 `LimitError`。
