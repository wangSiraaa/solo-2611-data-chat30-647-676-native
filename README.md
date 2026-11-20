# 钻孔层位对照与可比较地层数据仓（stratcorr）

两个**相互独立**的服务，全部使用 Python 3.11 标准库（SQLite + 精确动态规划，
无第三方依赖）。所有数据均为合成数据，软件只做组合簿记，**不作任何工程勘察判断**。

* **层位目录服务**（`stratcorr.catalog_service`）：只维护有限层位的偏序关系
  `shallower_than` 与各层位可接收的岩性代码集合，**不接收任何钻孔数据**。
* **钻孔分析仓**（`stratcorr.repo_db`）：消费目录发布的不可变产品（也接受
  外部产品 JSON），导入钻孔/样品，做精确对照求解并固化方案、统计与样品归属。

## 为什么需要它

相邻钻孔都写“砂层”，岩性代码相同并不代表属于同一层位；若跳过缺失层后按行号
对齐，会把下部样品错误接到别的地层。本系统：

1. 用偏序（而不是行号、也不是任意拓扑序）约束层位的深浅先后；
2. 允许层位之间**不可比**（顺序未定是一等公民，会给出证据）；
3. 用精确优化把每个已取芯段分配到“可接收其岩性”的层位或 `UNKNOWN`；
4. 样品按半开区间 `[start, end)` 归属，边界、缺口、UNKNOWN 都有确定规则。

## 核心规则

### 偏序与目录

* 边 `a -> b` 表示 `a` 比 `b` 浅。**禁止自环、禁止有向环**（建边即时拦截，
  发布时再次独立校验，有环绝不部分发布）。
* 不要求所有层位可比较。`evidence` 返回两个方向的路径；双向都无路径即不可比，
  并给出文字证据。拓扑排序仅作计算便利，**从不**被当作必然地层顺序。
* 每个层位有稳定 ID 与可接收岩性集合；每条边、每条岩性接收关系都带 `source`
  来源，发布产品完整保留来源。
* 更正一条边或岩性集合 = 发布**新版本不可变产品**；旧产品永不被改写。

### 钻孔与对照

* 输入为从浅到深、非重叠、正厚度、非负有理端点（`"num/den"` 规范字符串）的区间，
  可含明确未取芯缺口。
* 每个已取芯段分配到一个可接收其岩性的层位或 `UNKNOWN`；忽略 UNKNOWN 后：
  * 相邻**不同**层位之间必须存在由浅到深的 `shallower_than` 路径；
  * 同层位允许多个连续段，但一旦离开**不能再次返回**；
  * 人工固定层位是硬约束，**不能**被改成 UNKNOWN。
* 目标按严格字典序依次优化（先满足前者再比后者）：
  1. 最小化 UNKNOWN 权重总和；
  2. 再最小化每孔层位切换次数（求和）；
  3. 最后使按 `(孔ID, 段起深度)` 排列的层位 ID 向量字典序最小，
     `UNKNOWN` 排在所有真实 ID 之后。
* 不会因两层岩性相同就合并；不会把偏序的任意拓扑顺序当作必然顺序。
* 无满足人工固定的方案时返回**矛盾**，指出相关固定与关系证据并落库，
  绝不静默清除约束。

### 统计、缺口与样品

* 仓库按所选方案产出每层位每孔的顶/底深度、实际取芯厚度、缺口厚度与样品归属。
* **缺口只有当其上下紧邻的已取芯段属于同一已知层位时才归入该层位**；
  否则记为 `UNDETERMINED`，绝不跨层平均分配。
* 样品有稳定 ID 与单个深度，采用层段 `[start, end)` 归属：
  * 最深末端深度恰好等于 `end` 仍算**未命中（MISS）**；
  * 落在 UNKNOWN 段的样品记 `UNKNOWN_HIT`，**不计入**任何已知层位统计；
  * 落在缺口记 `GAP`；未指定孔且深度命中多个孔记 `AMBIGUOUS`。

### 不可变性与重新对照

* 关系更正后重新对照会真实产生**新方案与新样品归属**；旧方案永久固定、
  仍可查询并可独立复验。

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `rational.py` | 非负有理数的规范表示与精确运算（避免浮点漂移） |
| `models.py` | 层位/边/产品、钻孔/缺口/样品的冻结模型与 JSON 序列化 |
| `poset.py` | 偏序：环检测、位掩码传递闭包、路径重构、不可比证据、序列校验 |
| `catalog_service.py` | 目录草稿 CRUD、发布不可变产品、关系/证据/序列/来源查询（SQLite） |
| `solver.py` | 精确动态规划求解、矛盾检测，以及独立暴力枚举器（交叉核对） |
| `geometry.py` | 几何校验、逐层位统计、缺口归属、样品 `[start,end)` 命中 |
| `repo_db.py` | 分析仓：产品消费、导入、求解落库、解释、来源、全量复验 |
| `synthetic.py` | 确定性合成数据（缺层/不可比/重复岩性/边界样品/矛盾固定） |
| `cli.py` | 命令行入口 |

### 求解器与状态压缩（设计取舍）

孔与孔之间无耦合，故逐孔独立精确求解。单孔用动态规划，状态为
`(已用层位掩码, 最后层位)`，掩码用一个整数位表示（≤12 层位时最多 4096 个掩码）。
每层的状态值同时保存最优代价三元组 `(UNKNOWN权重, 切换数, 位置字典序整数)`
与指向**上一层**状态的回退指针；回退指针随状态原子替换，避免污染其他前驱的
回溯链。`UNKNOWN` 用最大数字编号，天然落在字典序最后。

另有一个完全独立的 `brute_force_hole` 枚举器，测试中对大量随机小孔与 DP 逐案
比对，验证两种实现结果一致。

## 容量与性能

限制：12 层位、8 孔、每孔 20 段。满负荷（8×20 段、12 层位、宽分支 DAG）
整批精确求解约十几毫秒。不做三维插值。

## 命令一览

```bash
export PYTHONPATH=/workspace        # 或 pip install -e .
python -m stratcorr.cli --help

# 一键合成端到端演示（含更正、环拒绝、矛盾固化、旧方案保留）
python -m stratcorr.cli demo
```

### 目录服务

```bash
stratcorr catalog init <cat.db>
stratcorr catalog horizon-add <cat.db> H1 顶层 --lith CLAY --source doc-A
stratcorr catalog lith-add|lith-remove <cat.db> <horizon> <code> --source ...
stratcorr catalog edge-add <cat.db> H1 H2 --source log-1      # 自环/环会被拒
stratcorr catalog edge-remove <cat.db> H1 H2
stratcorr catalog edge-list|horizon-list|validate <cat.db>
stratcorr catalog publish <cat.db> --note "..."
stratcorr catalog product-list <cat.db>
stratcorr catalog export <cat.db> P0001 --out product.json
stratcorr catalog relation <cat.db> H1 H6 [--product P0001]   # 传递关系+见证路径
stratcorr catalog evidence <cat.db> H3 H4 [--product P0001]   # 不可比证据
stratcorr catalog sequence <cat.db> H1 H2 H4 [--product ...]  # 序列校验
stratcorr catalog provenance <cat.db> P0001                   # 关系来源
```

### 分析仓

```bash
stratcorr repo init <repo.db>
stratcorr repo import-catalog-product <repo.db> <cat.db> P0001
stratcorr repo import-external <repo.db> product.json          # 接受外部产品
stratcorr repo products|boreholes <repo.db>
stratcorr repo import-borehole <repo.db> boreholes.json
stratcorr repo import-sample  <repo.db> samples.json
stratcorr repo solve <repo.db> P0001                           # 产出不可变方案
stratcorr repo schemes|assignments|stats|gaps|samples <repo.db> <scheme>
stratcorr repo explain   <repo.db> <scheme>   # 原始孔段+层位路径+全局方案
stratcorr repo validate  <repo.db> <scheme>   # 独立重算并逐行比对
stratcorr repo provenance <repo.db> P0001
```

所有命令支持 `--json`（放在子命令前或后均可）；校验类失败与矛盾返回退出码 2。

### 导入 JSON 格式

```json
{
  "borehole_id": "BH1",
  "segments": [
    {"segment_id": "S1", "start": "0/1", "end": "2/1",
     "lithology": "CLAY", "weight": 1, "fixed_horizon": null}
  ],
  "gaps": [{"gap_id": "G1", "start": "4/1", "end": "6/1"}]
}
```

样品：`{"sample_id": "SM1", "depth": "5/1", "borehole_id": "BH1"}`
（`borehole_id` 可省略，省略后跨所有孔检索，多孔命中记为歧义）。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：偏序环/自环/传递闭包/不可比证据/序列；DP 对暴力枚举的随机交叉核对、
三级目标、重入禁止、各类人工固定矛盾；几何与 `[start,end)` 边界样品、
同层/跨层缺口、UNKNOWN 排除；SQLite 端到端、不可变产品、更正后方案与样品
真实变化且旧方案固定、矛盾落库、独立全量复验，以及 12/8/20 容量上限。
