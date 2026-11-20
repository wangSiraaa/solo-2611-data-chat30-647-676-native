# 多历元星表服务与独立曝光匹配仓

本地合成数据实现，不连接任何天文数据库。两个服务各自独立、各自有 SQLite 持久化，
可随时关闭重开：

* **星表服务（catalog）**：条目的新增 / 替换 / 撤回、事件审计、固定版本发布、
  指定历元方向查询与方向快照导出（只导出方向与来源版本，不导出速度）。
* **匹配仓（warehouse）**：消费历元方向快照（本星表导出或外部同格式 JSON），
  对曝光亮点做带三级目标的二分图匹配，每次匹配生成不可变 run，并提供落选解释。

运行环境：Python 3.11、NumPy、SciPy（仅用 `scipy.optimize.linear_sum_assignment`）、
标准库 SQLite。无其他依赖。

## 1. 安装与复跑

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 一键自检：30 个 ≤5星×5点 随机小例独立枚举核对 + 手算速度传播 + 场景回归 + 规模冒烟
.venv/bin/python -m astromatch.cli verify

# 端到端演示：200星/20曝光规模集 + 全部小场景，结尾删除原始输入后再复读
.venv/bin/python -m astromatch.demo --data-dir data

# 单元/集成测试（标准库 unittest，无需 pytest）
.venv/bin/python -m tests.test_astromatch
```

参考实测：全量 200 星 × 20 曝光 × 每曝光 100 亮点端到端约 1.2 秒（普通笔记本）。

## 2. 合成模型

严格采用题面给定的有限模型，不套真实天体物理修正：

```
p(t) = r + (t - t0) * v
若 ||p(t)|| <= 1e-15  → 该历元不可用（不进入快照，不参与匹配）
否则方向 dir(t) = normalize(p(t))
```

`r/v` 为 float64 三维向量，所有写入先做有限性（无 NaN/Inf）校验。

## 3. 匹配优化目标与算法

候选边：仅当星方向与亮点方向夹角 `<= threshold` 时建边；向量化全量角度矩阵，
**不做逐点最近邻、不使用亮度**（亮度只做非负整数校验并持久化，不进入任何规则）。

三级字典序目标（严格按题面）：

1. **最大化匹配数 K**（Kuhn 增广路求基数）；
2. 在所有 K 条边的匹配中**最小化夹角平方和 C**；
3. 在匹配数仍为 K 且总代价 `<= C + eps` 的方案里，取排序后
   `(star_id, detection_id)` 列表**字典序最小**者。

实现要点：

* 第 2、3 级用带离配惩罚的线性指派（匈牙利法，LSA）作可行性预言机。
  指派矩阵中真实边代价为夹角平方（`<= pi^2 < 10`），离配代价 `P_UNMATCH=100`，
  禁边 `BIG=1e100`；因 `P_UNMATCH > min(n,m)*pi^2`，基数区间互不重叠，
  LSA 先最大化基数，同基数下再最小化平方和。
* 第 3 级是逐条边的字典序贪心：候选边按 `(star_id,detection_id)` 排序，
  对每条边调用一次 LSA 预言机，判定“包含该边后是否仍存在 K 条边且总代价
  不超过 C+eps 的可行方案”，可行则固定、否则排除。每次裁决（含预算、最优补全代价、
  落选原因）都写入 run 的轨迹，供 `wh-explain` 复读。
* **eps 是统一的总代价容差**：先求最小代价 C，再只在 `[C, C+eps]` 带内做字典序裁决；
  不把近似相等关系串联成等价类（没有传递闭包、没有并查集）。
* `matcher.brute_force_optimal` 独立枚举全部顶点不相交边集（仅用于 ≤5×5 小例），
  与主求解器分开实现，`verify` 逐例核对两者的基数、代价与字典序结果完全一致。

## 4. 数据布局

```
astromatch/
  vectors.py    向量校验、零向量判定、历元传播、夹角矩阵
  snapshot.py   快照数据类与 JSON 格式（astromatch.snapshot/v1），sha256 派生 snapshot_id
  catalog.py    星表服务（SQLite：stars / star_events / releases / release_entries）
  matcher.py    候选构建、基数、LSA 预言机、三级求解、裁决轨迹、暴力枚举
  warehouse.py  匹配仓（SQLite：snapshots / exposures / detections / runs /
                run_matches / run_candidates，快照与轨迹全部库内封存）
  synthetic.py  200星/20曝光规模集与全部小场景（固定随机种子，可复跑）
  cli.py        全部交付命令 + verify
  demo.py       端到端演示
tests/          unittest 测试套件
data/           运行时生成的 SQLite 与输入 JSON（演示结尾会删除 JSON 再复读）
```

### 星表服务表

* `stars`：当前工作集（star_id 主键，active/withdrawn）；
* `star_events`：add/replace/withdraw 全量审计；
* `releases` + `release_entries`：固定版本发布即冻结当时全部 active 条目。
  撤回不影响任何已发布版本；替换保持 star_id。
* 每次写入一个事务。批量导入在最外层开单个事务，任一坏行整批回滚，
  不会留下“半条速度”（嵌套方法通过 `_commit=False` 复用外层事务，避免提前提交）。

### 匹配仓表与不可变 run

* 快照以规范化 JSON 的 sha256 前 16 位为 `snapshot_id` 去重封存；
  外部快照 `source=external`、`catalog_release=NULL`，匹配仓不读取速度表。
* 同一曝光每次匹配生成 `R-<exposure_id>-<seq:03d>`，seq 单调递增；
  run 绑定当时的 snapshot_id、阈值、eps、候选边（含 selected 标记）、匹配与完整轨迹。
* 星表更正后重跑产生**新 run 绑定新快照**；旧 run 的分配与快照永不改变，
  不会只更新显示坐标而保留旧分配。原始输入 JSON 删除后，所有结果仍可从库内复读。

## 5. 命令清单

### 星表服务

```bash
# 初始化 / 批量导入（支持 add|replace|withdraw 操作行，整批一个事务）
python -m astromatch.cli catalog-init --db data/catalog.sqlite
python -m astromatch.cli catalog-import --db data/catalog.sqlite --file stars.json
python -m astromatch.cli catalog-replace  --db data/catalog.sqlite --file fix.json
python -m astromatch.cli catalog-withdraw --db data/catalog.sqlite --star-id S2
python -m astromatch.cli catalog-show --db data/catalog.sqlite [--star-id S1]

# 固定版本发布与查询
python -m astromatch.cli catalog-release  --db data/catalog.sqlite --release-id R1 --note "初始版本"
python -m astromatch.cli catalog-releases --db data/catalog.sqlite
# 普通用户：不导入曝光也能取某历元完整方向表与来源版本（零向量列在 unavailable）
python -m astromatch.cli catalog-query --db data/catalog.sqlite --epoch 30 --release R1 --out q.json
# 导出只含方向的快照（供匹配仓或外部系统消费）
python -m astromatch.cli catalog-snapshot --db data/catalog.sqlite --epoch 30 --release R1 --out snap.json
```

星表导入 JSON 形式：

```json
{"stars": [
  {"star_id": "S1", "t0": 0, "r": [1.0, 0.0, 0.0], "v": [0.0, 0.0, 0.0]},
  {"op": "replace", "star_id": "S2", "t0": 0, "r": [1.0, 0.03, 0.0], "v": [0.0, -0.06, 0.0]},
  {"op": "withdraw", "star_id": "S3"}
]}
```

### 匹配仓

```bash
python -m astromatch.cli wh-init --db data/warehouse.sqlite
# 导入曝光（单条或数组）；方向须有限且单位长度误差 <= unit_tol，亮度为非负整数
python -m astromatch.cli wh-import-exposure --db data/warehouse.sqlite --file exposure.json
# 也可直接消费外部同格式快照（不读星表库）
python -m astromatch.cli wh-load-snapshot --db data/warehouse.sqlite --file external_snapshot.json

# 三种快照来源任选其一；threshold 为夹角阈值（弧度），eps 为总代价容差
python -m astromatch.cli wh-match --db data/warehouse.sqlite --exposure-id E1 \
    --threshold 0.02 --eps 1e-12 --snapshot-file snap.json
#   或 --snapshot-id <id>
#   或 --catalog-db data/catalog.sqlite --release R1   （按曝光历元现场导出）

python -m astromatch.cli wh-runs     --db data/warehouse.sqlite [--exposure-id E1]
python -m astromatch.cli wh-show-run --db data/warehouse.sqlite --run-id R-E1-001 [--full]
# 解释：某条匹配为何成立 / 某候选因竞争落选（含预算、最优补全代价、竞争边）
python -m astromatch.cli wh-explain  --db data/warehouse.sqlite --run-id R-E1-001 \
    --star-id S2 --detection-id D1
python -m astromatch.cli wh-explain  --db data/warehouse.sqlite --run-id R-E1-001   # 整条 run 叙述
```

曝光 JSON 形式：

```json
{"exposure_id": "E1", "epoch": 30, "unit_tol": 1e-9,
 "detections": [
   {"detection_id": "D1", "direction": [0.9998, 0.0199, 0.0], "brightness": 512}
 ]}
```

## 6. 内置场景（`astromatch/synthetic.py`，demo 与 verify 全覆盖）

| 场景 | 验证点 |
| --- | --- |
| `adjacent_competition` | 两个近邻亮点逐点取最近星会重复判给同一颗星（被禁止）；全局最优为 2 条 |
| `one_star_many_detections` | 单星多亮点只配一个，另一个进入未匹配亮点 |
| `velocity_flip` | 手算速度传播：t=0 最近 S1、t=1 翻转到 S2；星表更正后新 run 改变、旧 run 不变 |
| `zero_vector` | `r+(t-t0)v=0` 的历元不可用，星从快照剔除 |
| `no_candidate` | 全部夹角超阈值，K=0，星与亮点均未匹配 |
| `withdrawal` | 撤回星不进新发布、旧发布与旧 run 仍可查 |
| `brightness_invariance` | 同一几何换亮度分布，分配完全不变 |
| 外部快照 | `source=external` 的同格式快照可独立消费，匹配仓不接触速度表 |

## 7. 边界与校验行为

* 方向非有限、单位长度误差超 `unit_tol`、detection_id 重复、亮度非非负整数
  → 拒绝且不入库；曝光历元与快照历元不一致 → 拒绝匹配。
* 重复 add、替换不存在/已撤回条目、重复撤回 → 明确报错，事务回滚。
* 零向量历元不造假方向：从可用方向表与快照中剔除，并在查询结果的
  `unavailable_star_ids` 中显式列出。
* 不做图像星点提取、不做观测设备控制。
