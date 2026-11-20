# 缠绕相位约束目录与位移范围事实仓

全离散合成模型（不处理真实雷达图像）。给定固定正整数模数 **M**（一整周），
有向观测边 `(u,v)` 携带余数 `r ∈ [0,M)` 与闭整数界 `[lo,hi]`，语义为

```
x_v - x_u ∈ { r + M·k | lo ≤ r + M·k ≤ hi }      （全部为整数）
```

本项目用 **SQLite + 自包含精确约束求解器** 实现两个职责独立、互不耦合的部件：

| 部件 | 模块 | 职责 | 是否管理锚点/尺度 |
| --- | --- | --- | --- |
| 独立相位约束目录（生产侧） | `phaseware/catalog.py` | 边的新增/撤回/更正、候选与路径查询、发布不可变产品 | 否 |
| 位移分析仓（消费侧） | `phaseware/repository.py` | 消费公开产品，精确可行性、节点极值、位移有理区间、反证 | 是 |

全部数值使用 Python `int` 与 `fractions.Fraction`，**不使用浮点余数**。

## 核心设计原则（对应题面要求）

1. **多来源边同时成立、互不覆盖**：同一有向节点对可挂多条边；目录提供同向候选
   **交集**，交集为空即来源冲突，照实报告，绝不自动删除或让后者覆盖前者。
2. **冲突不阻断发布**：回路残差不为零、同向交集为空的观测仍随产品发布，由分析仓
   判定 `INCONSISTENT` 并给出去重后的最小反证核与可重放搜索记录。
3. **极值来自同一个全图可行域**：每个节点的最小/最大值都是在“满足全部边 + 全局界
   + 锚点”的完整赋值上独立优化得到，并附完整见证赋值；**绝不把各边沿路径独立相加
   当作全局范围**。不同节点的极值不保证同时达到，报告显式给出全最小/全最大向量的
   联合可行性标记。
4. **无锚分量不钉零**：每个连通分量可以没有锚点；无锚分量的整体平移歧义如实报告，
   其有限极值仅由全局界 `[-B,B]` 截断。
5. **产品不可变、分析可冻结复跑**：目录每次发布生成内容寻址的 `product_id`；分析仓
   只导入产品 JSON、不读目录编辑表。撤回/更正产生**新产品**，旧分析报告永久固定在旧
   产品与旧锚点上，`request_id` 不可覆盖。
6. **未知节点 / 空候选 / 空交集 / 空路径明确拒绝**，不静默吞掉观测。

## 求解方法

`phaseware/exact.py` 是自包含的有限域 CSP 求解器（题面允许穷举/传播，环境无外部
求解器库）：

- **AC-3 弧一致性**（含一元界/锚点约束、自环、平行边、多重图）；
- **MRV 回溯**，按节点 ID 与值升序枚举，天然得到字典序最小示例解；
- 单节点极值用“目标节点优先、按升/降序取值”的定向搜索，返回完整见证；
- `brute_force_enumerate` 是与 AC-3 **完全独立的全枚举代码路径**，在小图上交叉核对
  可行性、解数、唯一性与逐节点极值；组合空间过大时明确跳过而不做假核对。

## 规模上限

节点 ≤ 12，活跃边 ≤ 30，M ≤ 16，B ≤ 32，每边候选差值 ≤ 5，分析请求 ≤ 10。
超限在导入/发布/分析时直接拒绝。

## 命令一览

```bash
# 目录（生产侧）
python -m phaseware.cli catalog init      --db cat.db --m 8
python -m phaseware.cli catalog add-node  --db cat.db --node a
python -m phaseware.cli catalog add-edge  --db cat.db --source s1 --u a --v b --r 0 --lo 0 --hi 2
python -m phaseware.cli catalog candidates--db cat.db --edge-id 1
python -m phaseware.cli catalog intersect --db cat.db --u a --v b
python -m phaseware.cli catalog path      --db cat.db --nodes a b c          # 节点序列
python -m phaseware.cli catalog path      --db cat.db --edges 1 2            # edge_id，可反向经过
python -m phaseware.cli catalog lint      --db cat.db                        # 来源冲突/回路残差
python -m phaseware.cli catalog retract   --db cat.db --edge-id 3 --reason "错误回路边"
python -m phaseware.cli catalog correct   --db cat.db --edge-id 3 --r 0 --lo 0 --hi 0
python -m phaseware.cli catalog publish   --db cat.db --note v1 --export product.json
python -m phaseware.cli catalog products  --db cat.db

# 分析仓（消费侧；scale 为正有理数，anchor 为整数）
python -m phaseware.cli repo import   --db repo.db --file product.json --origin external-file
python -m phaseware.cli repo analyze  --db repo.db --request-id r1 --product-id <PID> \
       --bound 4 --scale a=3/2 --scale b=1/4 --anchor a=0
python -m phaseware.cli repo report   --db repo.db --request-id r1
python -m phaseware.cli repo explain  --db repo.db --request-id r1
python -m phaseware.cli repo requests --db repo.db

# 验证
python -m phaseware.cli validate            # 12 个内置场景，含独立全枚举核对
python -m phaseware.random_check            # 200 个随机小 CSP 与全枚举逐项对齐
```

任意查询命令前加全局 `--json` 可输出机器可读 JSON。退出码：`0` 成功/一致，
`2` 分析结果为 `INCONSISTENT`（合法结果，非用法错误），`1` 用法或数据错误。

## 报告字段要点

- `status`：`CONSISTENT` / `INCONSISTENT`；
- `node_results[n].phase_min/max` 与 `min/max_witness`：极值及来自全图可行域的见证；
- `node_results[n].displacement_min/max`：`Fraction(相位)·scale` 的精确有理区间
  （`{num, den, text}`）；
- `unique_solution_overall`、每节点 `unique_phase`；
- `lexicographically_smallest_solution`：仅为字典序最小**示例**，多解时不冒充唯一；
- `joint_extrema`：全最小/全最大向量及其联合可行性（不同节点极值不保证同时达到）；
- `components[].translation_freedom`：无锚分量的平移歧义证据；
- `unsat_core`：删除意义下最小的不相容约束子集（边/锚点，含最小性复核）；
- `replay`：初始域、约束清单、AC-3 每次修订删除的值、空域 wipeout，可完整重放反证；
- `brute_force_cross_check`：独立全枚举的解数与逐项核对结果。

## 内置验证场景（validate）

树形多解、无锚平移歧义不钉零、单个回路矛盾、正反向边各自解释、同向来源冲突不覆盖、
撤回与更正后解恢复（旧分析冻结）、外部产品导入与篡改拒绝、有理 scale 位移区间、
节点极值同源与联合可达声明、不相容锚点反证核、未知节点/空候选显式拒绝、请求上限与
不可变性。所有可行场景均附带独立全枚举核对。

## 目录结构

```
phaseware/
  exact.py        # AC-3 + MRV 回溯精确求解器、连通分量、回路基、独立全枚举
  catalog.py      # SQLite 编辑表、候选/交集/路径查询、不可变产品发布与校验
  repository.py   # 产品导入、锚点分析、极值/位移、最小反证核、报告冻结
  validation.py   # 12 个内置场景
  random_check.py # 随机化性质测试（与全枚举对齐）
  cli.py          # 命令行入口
```
