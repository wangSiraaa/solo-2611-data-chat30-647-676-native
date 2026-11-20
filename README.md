# 多相机标定图产品与三维轨迹事实仓

本地、纯合成、可复跑的坐标标定图服务与独立三维轨迹仓。使用 Python 3.11、NumPy、SciPy、SQLite，不接摄像头，不做测量认证，也不做图像目标检测或跨相机身份识别。

```
trajcalib/
  geometry.py      刚体变换代数与统一容差校验
  graph.py         标定图服务（增删替换边、一致性检查、发布产品、SQLite 版本快照）
  observations.py  观测 JSONL 契约、针孔射线/投影
  solver.py        加权无限直线三角化 + 退化/后方判定
  warehouse.py     独立轨迹仓（run 版本、点、残差、断段、追溯）
  scene.py         合成场景 + 各边界 fixture
  pipeline.py      generate → graph → publish → ingest → run → 更正 → 新 run
  verify.py        独立验证套件（不信任求解器内部）
  server.py        本地 HTTP API
  cli.py           命令行
tests/test_core.py pytest 用例
```

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install numpy scipy pytest

# 全流程：生成场景、建图、发布 v1、入仓、更正、发布 v2、重跑、跑 fixture
.venv/bin/python -m trajcalib build --workspace .

# 独立验证（72 项断言，全部通过时退出码 0）
.venv/bin/python -m trajcalib verify --workspace . | tee out/verification.json

# 单元测试
.venv/bin/python -m pytest tests/ -q
```

## 坐标与变换约定

- 图节点为唯一 `frame_id` 的坐标系；边 `src -> dst` 保存 `x_dst = R @ x_src + t`。
- 统一容差 `tol=1e-9`：`R` 必须有限、`|RᵀR−I|∞ ≤ tol`、`|det R − 1| ≤ tol`；`t` 必须有限。
- 每个连通分量内任意路径必须给出一致的变换。所有变更（单条或 `apply_batch`）先把候选图整体校验：矩阵无效、`NaN/Inf`、非法环 → **整次拒绝**，版本号与图内容不变。
- `transform(src, dst)` 对任意同分量可达坐标直接给变换，无需观测数据。
- 发布指定世界根，输出所有可达相机的 `cam_to_world`；不可达相机进入 `unreachable_cameras`（附原因），绝不臆造单位矩阵。产品 JSON 带内容哈希（`integrity`），加载时校验。

## 观测与三角化

观测 JSONL 字段：`obs_id, track_id, t(整数), camera_id, u, v, weight>0`。

契约（违反则整批拒绝）：
- `(track_id, t, camera_id)` 重复、`obs_id` 重复、`t` 非整数、`u/v/weight` 非有限、`weight ≤ 0`。

视线：相机坐标 `normalize(((u-cx)/fx, (v-cy)/fy, 1))`，用**本次实际标定产品**变换到世界射线。

对每个 `(track_id, t)`，最小化 `Σ w_i·dist(X, line_i)²`。令 `P_i = I − d_i d_iᵀ`：
- 正规矩阵 `A = Σ w_i P_i`，右端 `b = Σ w_i P_i o_i`，解一次 3×3 线性方程。
- 仅当 **≥2 相机** 且 **λmin/λmax ≥ 配置门槛**（默认 `1e-3`）且秩为 3 才发布点；否则记为 `degenerate`，保存特征值、秩、理由，**不伪造深度**。
- 解在任一参与相机 `z ≤ 0` → 标记 `inconsistent`，保留全部观测与点，不自动剔除。
- 投影核对用 `z>0` 针孔公式独立重算像素残差。

## 轨迹仓

- 只消费**外部产品 JSON**，从不读取图的边（验证项 `warehouse.works_without_graph_db` 会隐藏图数据库后重跑）。
- run 以 `(dataset, product, min_eigen_ratio, max_gap)` 唯一；同一观测在用更正后的产品重跑时生成**新 run**，旧 run 的产品引用、点、残差、断段全部保留。
- 相邻点时间间隔 `> max_gap` 或任一端点不可用时，该段 `interpolate_allowed=0` 并给出原因；`interp` 明确拒绝。
- `trace` 从点追到每个相机的观测、实际产品变换、内参、线距离、相机 z、投影与像素残差。

## CLI

```bash
# 生成所有合成场景到 out/
python -m trajcalib generate --out out

# 构建 + 发布 + 入仓 + 更正重建，结果写 out/pipeline_summary.json、out/run_report.json
python -m trajcalib build --workspace .

# 从图发布产品
python -m trajcalib publish --scene main --graph-db db/calibration_graph.sqlite \
    --product-id main-v2 --out out/main/products/calibration_v2.json

# 普通几何客户：任意可达坐标转换，无需观测
python -m trajcalib query --product out/main/products/calibration_v2.json \
    --src cam_00 --dst world --point 0,0,1

# 追溯点 -> 观测 -> 实际变换
python -m trajcalib trace --warehouse db/trajectory_warehouse.sqlite \
    --run-id <RUN_ID> --track-id track_000 --t 0

# 插值（跨断段/退化时明确拒绝）
python -m trajcalib interp --warehouse db/trajectory_warehouse.sqlite \
    --run-id <RUN_ID> --track-id track_gap --t 20

# 用新（更正）产品对同一 dataset 重建新 run
python -m trajcalib rebuild --warehouse db/trajectory_warehouse.sqlite \
    --dataset-id <DATASET_ID> --product out/main/products/calibration_v2.json

# 验证
python -m trajcalib verify --workspace .

# 本地 HTTP 服务
python -m trajcalib serve --host 127.0.0.1 --port 8765
```

HTTP 端点：`GET /health`、`/frames`、`/graph/components`、`/graph/edges`、`/transform?src=&dst=`、`/products`、`/warehouse/runs|tracks|point|trace|trajectory|interp`；`POST /graph/edges`（单条或 `{"ops":[...]}` 原子批次）、`/publish`、`/warehouse/register-product`、`/warehouse/ingest`、`/warehouse/run`。非法矩阵/环返回 HTTP 409，错误详情在 JSON `error`。

## 合成场景

主场景（规模符合要求）：
- 20 坐标系 = `world` + 11 个 `link_*` + 8 个 `cam_*` 相机
- 8 相机，内参 `fx,fy>0`，`cx,cy` 有限，无畸变
- 100 个基础目标 × 100 个整数时刻，另有边界轨迹
- 已知三维点做**无噪声**针孔投影生成观测；`cam_03` 初始外参为陈旧值，更正包修复后产生第二个 run

边界 fixture（各自独立小图与产品）：

| fixture | 现象 | 期望 |
|---|---|---|
| `parallel` | 两相机同姿态、同射线 | 秩 2，全部 `degenerate`，保留证据 |
| `near` | 基线 5e-3、视差近乎消失 | 特征比 < 门槛，`degenerate` |
| `behind` | 目标位于某相机后方 | `inconsistent`，保留观测，标出负深度相机 |
| `unreachable` | 孤立相机岛 | 产品列出 `unreachable_cameras`，无伪单位阵 |
| `loop_conflict` | 冲突环 / 非正交 R / det=−1 / NaN / Inf | 全部整次拒绝，图版本不变 |

## 验证方法（独立于实现）

`verify` 在临时工作区从零重建后执行 72 项断言，关键项：
- 用**原始像素 + 产品位姿**独立重算射线，与 `normalize(((u−cx)/fx,(v−cy)/fy,1))` 逐分量比对（<1e-12）。
- 用 SciPy `lstsq` 堆叠最小二乘独立求点，与仓内解比对（<1e-7）。
- 用独立针孔公式回投，像素残差 <1e-7；产品 v2 与合成真值相机位姿 <1e-9。
- v1 的陈旧相机 `cam_03` 残差 > 1 像素，v2 归零；两 run 解显著不同。
- 验证仓在隐藏图数据库后仍能追溯（证明其不私读图边）。
- 退化/近退化/后方/不可达/环冲突的失败依据均被保留并断言。

产物：`out/verification.json`、`out/pipeline_summary.json`、`out/run_report.json`、`out/main/products/calibration_v{1,2}.json`、各 fixture 目录、`db/*.sqlite`。

## 取舍说明

- 采用加权无限直线（视线）最小二乘，符合题面“到各条无限直线加权平方距离之和最小”；不做重投影非线性优化，避免引入题面未要求的额外步骤。
- 退化门槛用正规矩阵 λmin/λmax 比值，同时要求秩 3，双重保险。
- 图一致性每次变更全图重算（图规模小，20 节点），简单且保证原子性；轨迹仓按 `(track,t)` 分组批量重算，100×100 规模单次数秒。
- 不含镜头畸变、目标检测、跨相机身份识别、测量认证。
