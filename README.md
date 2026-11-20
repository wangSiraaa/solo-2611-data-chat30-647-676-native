# 载波连续弧产品与双站差分位移仓

独立的连续弧服务 (arc service) 与双站差分位移仓 (differential warehouse)。
全部物理量用 `fractions.Fraction` 精确计算，二进制浮点从不参与相位、波长、
位移或搜索边界。持久化使用 SQLite。不实现真实 GNSS 协议、不联网、不用外部星历。

```
gnss_arc/
  rational.py     # 精确有理数：Q() 把 float 经 repr 转成分数，安全取整/上下取整
  storage.py      # SQLite：观测、周跳事件、不可变弧产品快照、任务与结果
  arc.py          # 连续弧服务：版本化公开产品、切断原因、来源事件、区间查询
  differential.py # 双站差分仓：精确整数歧义求解（lattice + 闭界）
  synthetic.py    # 有限合成场景（地面真值），供 CLI/测试使用
  cli.py          # demo / verify / solve 命令
tests/test_arc_diff.py
```

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install pytest

# 1) 单元/集成测试（19 项，覆盖全部要求场景）
.venv/bin/python -m pytest tests/ -q

# 2) 独立暴力核对：小例直接枚举整数对，与精确求解器对比
.venv/bin/python -m gnss_arc.cli verify      # -> verified 126 cases, 0 failures

# 3) 内置合成场景演示（同波长多解 / 双波长唯一 / 锁失 / 超 1000 解）
.venv/bin/python -m gnss_arc.cli demo

# 4) 对已填充的 SQLite 库求解一条任务，输出精确 JSON
.venv/bin/python -m gnss_arc.cli solve --db demo.db \
  --rover ROV --base BASE --lambda1 1/5 --lambda2 1/7 \
  --win-lo 1 --win-hi 4 --dlo 0 --dhi 1/2
```

## 模型（题目给定，原样实现）

每个历元 `t`、每个通道 `j∈{1,2}`：

```
z_j(t) = corrected_phase_rover_j(t) - corrected_phase_base_j(t)
lambda_j * (z_j(t) - n_j) = d(t)          # n1, n2 全窗口固定整数，d(t) 有理
Dlo <= d(t) <= Dhi                         # 闭区间，逐历元成立
```

不同历元的 `d(t)` 可以各不相同；不强加平滑或均值约束。

## 连续弧服务 (`arc.py`)

把「观测 + 显式周跳事件」变成**版本化的公开产品**。每个历元公开：

* 校正相位 `raw_phase - 累计jump`（精确有理数）
* 是否可用（`lock` 为真且观测存在）
* 连续弧 ID
* 新弧开始的原因（`FEED_START` / `NO_LOCK` / `JUMP` / `GAP` / `END_OF_DATA`）及来源事件 id

**切弧规则**（`lock=false` 的历元本身不可用，且切断弧）：

1. `lock == false` → `NO_LOCK`
2. 相邻可用历元间存在生效周跳事件 → `JUMP`（记录触发的 `source_event`）
3. 相邻可用历元间隔 `> G` → `GAP`（`<= G` 的间隔不切弧，只是节点变稀）
4. 数据起点 → `FEED_START`，数据终点 → `END_OF_DATA`

**周跳更正**：新事件或 `supersede_jump` 后重建 → 产生**新的 product id**；
旧产品是不可变快照（`arc_epochs` 表逐历元落盘），历史任务用旧 id 可复现旧答案。
`ArcService.query(station, channel, lo, hi)` 独立支持任意站的弧区间查询。

## 差分仓 (`differential.py`)

**只消费公开产品的校正相位**，从不读原始观测、从不改写相位；可接受持久化产品
（按 id 固定快照）或外部传入的 `ArcProduct`。

### 完整性

窗口内四路（rover/base × 两通道）每一历元都必须可用，且每路弧 ID 在窗口内不变，
否则 `INCOMPLETE`——**绝不删除坏历元来缩短窗口**。

### 精确求解（关键几何）

单通道只看得到 `n_j` 的模 `lambda_j` 的身份，无法单独定值。但两通道共享同一
`d(t)`，逐历元相消得到：

```
A = lambda1, B = lambda2
A*z1(t) - B*z2(t) = A*n1 - B*n2 =: K      # 对所有 t 必须是同一个有理数
```

因此：

1. **K 恒定检验**：若 `A*z1(t)-B*z2(t)` 随 `t` 变化 → 无解 `INCONSISTENT`。
2. 清分母后变成一条线性丢番图方程 `a*n1 - b*n2 = m`。整数解构成一条等差数列
   `(n1, n2) = (n1_0 + b'·s, n2_0 + a'·s)`，`s∈Z`。
3. 闭位移界把 `s` 限制到一个整数区间：
   `s ∈ [ceil(max_t (base_t - Dhi)/step), floor(min_t (base_t - Dlo)/step)]`，
   其中 `base_t = A*(z1(t) - n1_0)`，`step = A*b'`。
4. 区间内每个整数 `s` 对应一个可行 `(n1,n2)` 和一条**唯一确定**的位移序列。

由位移界还能**先独立**推出每个 `n` 的整数范围（`Dlo ≤ lambda_j*(z_j-n_j) ≤ Dhi`
逐历元取交），边界取等保留（闭界）：`n1_range`、`n2_range` 随结果输出。
step 为正的结论来自 `lambda_j > 0`，故排序与范围核定无歧义。

> 注：单波长同波长时 `A*z1 - B*z2` 恒为 0，丢番图退化为 `n1 = n2`，故通常多解；
> 双波长时该约束与闭界常把解集压到唯一。

### 状态

| 状态 | 含义 |
|---|---|
| `INCOMPLETE` | 某历元不可用，或某路弧 ID 在窗口内改变（不缩短窗口） |
| `INCONSISTENT` | 无任何整数对可行（跨通道不一致 / 丢番图无解 / 闭界内 `s` 区间为空 / 独立范围为空） |
| `AMBIGUOUS` | 可行 `(n1,n2)` 多于一个 |
| `UNIQUE` | 恰好一个 |

### 超过 1000 解

候选对可能远多于 1000（例如同波长、宽界）。结果：

* `solution_count`：**精确**可行对数（由 `s` 区间长度直接得出，不枚举全部）
* `truncated`：是否超过输出上限
* `solutions`：按 `(n1,n2)` 升序，最多列出 `OUTPUT_CAP = 1000` 条
* `per_epoch_extremes`：**全部**可行解下每历元位移的 `(min, max)`，由区间端点直接给出，
  即使只列 1000 条也准确

不会「截前 1000 条伪称完整」：超出时明确 `truncated=true`，并给出精确计数与极值。

## 数据规模

题面上限：≤10 站、每站 2 通道、≤200 历元、每 `n` 候选整数 ≤200。
求解规模与候选数无关：`s` 区间长度一步算出计数与极值，仅输出阶段枚举至多 1000 条。
200 历元双波长唯一解 < 0.1s；同波长 201 解 < 0.5s（见 `demo`）。

## 测试覆盖（`tests/test_arc_diff.py`，19 项）

1. 同波长 → 多解（并核对精确解集与真值）
2. 双波长 → 唯一
3. 锁失 → `INCOMPLETE`；弧 ID 在窗口内改变 → `INCOMPLETE`
4. 间隔 `<= G` 不切弧、`> G` 切弧（`GAP`）
5. 闭界端点保留（`d=0` 与 `d=Dhi` 都在解集中）
6. 周跳更正 → 新产品；旧产品保留旧答案；`supersede` 恢复唯一解
7. **暴力枚举交叉核对**：200 组随机小例，精确解集与枚举完全一致
8. 超 1000 解：精确计数 + 每历元极值，仍准确
9. 区间查询与来源事件
10. 逐历元变化位移不被平滑
11. 跨通道不一致 → `INCONSISTENT`；闭界不可行 → `INCONSISTENT`
12. 外部产品消费、结果 provenance 落盘、SQLite 往返

## 设计取舍

* **范围求解**：用丢番图通解参数化 `s`，而非逐对枚举——计数/极值 O(1) 于候选规模。
* **解集压缩**：不压缩；歧义如实报告，列出最多 1000 条并附精确计数与极值。
* **缓存粒度**：弧产品按 `(station, channel)` 版本化快照；差分结果记录所消费的
  product id 与戳（`event_level`、`obs_stamp`、`jump_stamp`），保证可复现。
* 弧服务不知基线配对/位移界；差分仓不越过产品改原始 `phase`。
