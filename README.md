# tidewin — 独立合成潮位目录与船舶通行窗口仓

纯 Python 3.11 本地实现，数据全部本地合成，**不可用于真实航行**。唯一运行时依赖为
`mpmath`（高精度求根）；`numpy/scipy` 不需要（题目允许但不适用，本实现也未引入）。

---

## 1. 题目要素如何落地

| 题面要求 | 本实现 |
|---|---|
| 每港有理常数 `b` + 有限项 `a_k cos(kwt)+c_k sin(kwt)`，`k≤4`，`w=π/12` | `tidewin/model.py`：`TideModel`，系数用 `Fraction` 精确存储 |
| 目录**独立**提供指定时刻水位、区间极值、局部项贡献 | `catalog.py` 的 `level_at / extrema / local_terms` |
| 模型**更正原子**形成新产品，旧分析不变 | 修订（revision）发布：`TideCatalog.publish`，旧修订永久可查、结果按修订钉住 |
| 航道有序段，`depth≥0`，`0<duration≤12`，各有港口模型 | `vault.py` 的 `Channel / Segment` |
| 船 `draft>0`，`margin≥0` | `Vessel` |
| 段 i 占用 `[s+offset_i, s+offset_i+duration_i]` | `Channel.offsets()`，`solve_request` |
| **整段**满足 `depth+tide ≥ draft+margin`，等号可过 | 安全集 `SAFE_i={u: level≥req}`，段窗口须整体落入 |
| 不能只查入/出段、不能忽略航行偏移 | 见 §3 算法；`verify.py` 用不同港偏移反例核对 |
| 输出**所有最大可行闭区间**与**孤立可行时刻**，按时间排序 | `solve_request` → `windows`（含 `isolated`） |
| 每个边界给约束段、对应潮位时刻、残差 | `BoundaryInfo.to_dict()`：`segment_index / port / revision / tide_time / residual / all_constraints` |
| 全空给 `INFEASIBLE` | `status` 字段 |
| 边界绝对误差 `1e-7` h | 高精度求根 + 相位展开，远超要求 |
| 相切零宽安全点必须保留 | 平方自由化保留重根（`gaussian.square_free_part`） |
| 仅在一点接触的两个可行区间应合并 | `merge_intervals`（容差 `1e-9`，远小于 `1e-7`） |
| 不得用容差抹平真实不安全正宽间隙 | 合并容差仅 `1e-9`；`verify` 专门核对 `1e-3` 间隙保留 |
| 常量模型、恒定恰达阈值、重根均合法 | 显式处理 `is_constant` 与重根；`verify` §2/§5 |
| 限 5 港、每模型 4 项、8 段、30 请求 | `data/catalog.json`（5 港 + 1 修订）、`data/scenarios.json`（≤8 段、30 请求） |

---

## 2. 关键数学：把"临界时刻"化为单位圆上的多项式

设 `θ = w t`，`z = e^{iθ}`。有
`cos(kθ)=(z^k+z^{-k})/2`，`sin(kθ)=(z^k−z^{-k})/(2i)`。
乘以 `z^4`（最高谐波 `k=4`）后，方程

```
b + Σ_{k=1..4} a_k cos(kθ) + c_k sin(kθ) = 0
```

等价于一个 **高斯有理系数多项式 `P(z)` 在单位圆上的根**（次数 ≤ 8）。

实现要点（`gaussian.py`）：

- 系数的加减乘除、多项式除法、GCD 全在 **Q(i)** 上精确进行（`Fraction`）。
- 求根前先做**精确平方自由化**：`G=gcd(P,P')`，`S=P/G`。于是任何**重根/相切根**
  （如 `1−cos` 在 `θ=0` 的 2 重根、`(1−cos)^2` 的 4 重根）在 `S` 中都是**单根**，
  再由 `mpmath.polyroots` 求出，**不会因重根合并/根平方而丢失**。
- 只保留 `|z|≈1` 的根，展开相位 `θ∈[0,2π)`，换算 `t=12θ/π`。

这套机制同时用于：

1. **极值/临界点**：`g'(t)` 也是有限三角多项式，其根即全部局部极值候选时刻。
2. **阈值穿越**：安全边界恰为 `g(t)=req` 的根。

因此"区间内最低值"只需比较**端点 + 全部临界点**，无需任何固定采样网格，
即题目要求的"完整有限范围求出相关临界时刻"。

---

## 3. 通行窗口算法（占整段语义，不漏根/相切点）

对段 `i`，令 `req_i = draft + margin − depth_i`。

1. **安全集** `SAFE_i = {u : level_i(u) ≥ req_i}`。
   在请求跨度上用 `level=req` 的**精确根**划分区间，每段用中点符号分类，
   得到若干**闭区间**；仅在某点相切（`level=req` 而邻域更小）的根保留为**孤立点**。
2. **按整段侵蚀**：长 `duration_i` 的窗口只能落在**同一个**安全区间内，故可开始时刻
   `U_i = {u : [u, u+duration_i] ⊆ SAFE_i}` = 对每个安全区间 `[a,b]` 取 `[a, b−duration_i]`
   （要求 `b−a ≥ duration_i`）。当 `b−a == duration_i` **恰恰好**时退化为**孤立点 `a`**
   ——这正是"相切零宽安全点"。
3. **回退偏移**：`s = u − offset_i`。每段用**自己**港口模型与**自己**偏移，绝不把
   各港原始窗口直接相交。
4. **交集**：各段集合与 `[S,E]` 用闭区间精确求交，仅接触的区间合并。

完备性论证：
- 安全集边界 = `level=req` 的**全部**根（平方自由，相切不丢），加跨度端点；
- 每段可行开始集 = 安全区间的整段侵蚀（代数恒等式，非采样）；
- 可行出发集 = 各段可行集 ∩ `[S,E]`（闭集有限交，仍为闭集 = 有限闭区间 ∪ 孤立点）。

所以不会漏掉任何区间、相切点或端点；也不依赖任何采样网格。

> 注：`verify.py` 里的高密度网格**只作反例辅助**（dense falsifier 只能"否定"结果，不能"证明"边界），
> 边界由上述精确算法给出。

---

## 4. 验证场景（`tidewin/verify.py`）

1. **单正弦手算**：`level=sin(πt/12)`，手算安全区间与窗口，逐点核对可行/不可行。
2. **相切孤点**：`cos≥0` 安全宽度恰 12h，12h 窗口仅在 `s=18` 成立 → 孤立点保留；
   并做 `±1e-4` 两侧不可行核对。
   另加**离轴真相切** `level=−1/2+2cosθ−cos2θ`（在 `t=4,20` 与阈值 1 双根相切），
   安全集应为**恰两个孤立点**。
3. **内部低谷**：丰富潮位（4 谐波）用较深吃水切出多段窗口；用密集网格核对无泄漏。
4. **不同港偏移**：段 1=P1 正弦、段 2=P3 余弦、偏移 2h。手算正确集与"忽略偏移的原始窗口相交"
   对照，证明偏移必须进入计算；网格核对。
5. **常数模型**：全零谐波常数高于/恰等于阈值（`depth+tide=draft+margin` 等号可过）均整段可行；
   低于阈值则 `INFEASIBLE`。
6. **接触合并**：相接触区间合并；`1e-3` 正宽不安全间隙**不被**抹平；`1e-10`（容差内）视为接触。
7. **修订发布**：发布更正后，钉住旧修订的结果**逐字节不变**，当前修订结果随之改变。
8. **全量 30 请求**：每个边界残差 `|·|≤1e-7`，每个窗口端点/中点可行。
9. （可选 `--falsify`）全量 **480k** 级网格点零泄漏。

运行：

```bash
.venv/bin/python -m tidewin.verify                 # 快速（~秒级）
.venv/bin/python -m tidewin.verify --falsify --falsify-step=0.02   # 含全量网格反例
```

核对手算例（单正弦、相切、低谷、异港偏移、常数）的期望值在 `verify.py` 各 `verify_*` 函数注释中，
可由闭式水位直接手推核对。

---

## 5. 目录与入口

```
tidewin/
  rational.py    有理数工具（Fraction 精确）
  gaussian.py    Q(i) 多项式、精确平方自由化、单位圆多项式构造
  model.py       TideModel：水位、局部项、临界点、阈值根、区间极值/最小残差
  catalog.py     TideCatalog：修订发布（原子）、当前修订、独立查询产品
  vault.py       Channel/Segment/Vessel、安全集、侵蚀、偏移、交集、边界解释
  io.py          JSON ↔ 对象
  gendata.py     合成 5 港模型 + 15 航道 + 30 请求
  run.py         跑全部请求并格式化输出
  verify.py      独立核对套件
  cli.py         统一命令行
data/
  catalog.json   模型修订（P2 含 rev1/rev2 更正产品）
  scenarios.json 航道与 30 请求
```

### 命令行

```bash
# 发布/编辑新产品（原子，旧修订保留）
.venv/bin/python -m tidewin.cli publish P2 --b 3/5 \
      --a "[3/2,-1/2,1/3,1/4]" --c "[0,3/4,-1/5,1/6]" --revision 2 --note "corrected"
.venv/bin/python -m tidewin.cli revisions P2
# 目录独立查询
.venv/bin/python -m tidewin.cli level   P2 6            # 指定时刻水位 + 局部项贡献
.venv/bin/python -m tidewin.cli extrema P2 0 48         # 区间极值（临界点法）
# 通行窗口 + 边界解释
.venv/bin/python -m tidewin.cli windows --request R06
.venv/bin/python -m tidewin.cli explain  R02            # 边界：约束段/潮位时刻/残差
.venv/bin/python -m tidewin.cli run-all
.venv/bin/python -m tidewin.cli verify --falsify
```

---

## 6. 复跑

```bash
make setup       # 建 venv 装依赖
make data        # 生成 data/*.json
make run         # 跑 30 请求
make verify      # 精确核对套件
make verify-full # 追加全量网格反例
```

（本机已用 Python 3.11.2 + mpmath 1.4.1 跑通；核对套件全部通过，
`--falsify` 480k 网格点零泄漏。）

## 7. 说明与边界

- 所有水位为**合成数据**，仅用于算法演示与核对，**禁止用于真实航行**。
- 边界误差要求 `1e-7` h；本实现以 80 位十进制精度求根，典型残差 `~1e-16` 甚至 `~1e-80`，
  合并/侵蚀容差均取 `1e-9`，不会抹平真实正宽不安全间隙。
- 允许外部同格式模型：`TideCatalog.from_dict` 可直接载入同结构 `catalog.json`；
  航道/请求同理经 `io.py` 载入。
