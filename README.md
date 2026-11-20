# 可编辑平面三角网服务与守恒场数据仓

纯 Python 3.11 实现，无外部服务依赖（仅标准库 `sqlite3` / `fractions`；测试用
`pytest`）。网格服务与场仓完全解耦：网格服务不需要场值即可导入、细分、查询邻接；
场仓不需要网格编辑入口即可接收任意合法外部网格、做守恒重映射与矩形子区域积分。

所有几何量用精确有理数（`fractions.Fraction`）表示。浮点输入按 `str` 转成精确分数
（`0.1 -> 1/10`），因此“三角形是否正面积”“并集是否恰为矩形”“相交面积是否为零”
都是精确判定，不依赖浮点容差。`GeometryPolicy` 显式声明方向约定、面积/覆盖/守恒容差；
默认面积与覆盖容差为 0，全局守恒相对容差 `1e-12`。

## 布局

```
conservative_remap/
  geometry.py    精确有理多边形裁剪、网格合法性校验、几何指纹
  mesh.py        MeshService：导入/邻接/split-edge 预演与发布/谱系（SQLite）
  field.py       FieldWarehouse：密度/守恒 remap/积分/trace/持久化（SQLite）
  synthetic.py   合成矩形三角网（规则网、扇形网、交错对）
  demo.py        端到端演示
tests/test_remap.py
```

## 运行

```bash
python3 -m pip install --break-system-packages pytest   # 仅测试需要
python3 -m pytest -q          # 23 个测试
python3 -m conservative_remap.demo
```

## 网格服务（`MeshService`）

固定矩形域；每个版本不可变，持久化在 SQLite（`version` / `version_cell` /
`version_vertex` / `operation_log`）。

导入时逐项校验，任一失败即整体拒绝，不留下半张网：

- 每个 cell 恰三个顶点的非退化三角形，**精确面积严格为正**；
- 相邻单元共享边必须两侧完整共享（无悬挂节点）；边被 >2 单元使用或同向使用均拒绝；
- 任意单元对**正面积相交**即拒绝（边/顶点相切允许）；
- 单元并集面积精确等于矩形面积，边界回路恰为矩形四边，边界顶点必须落在矩形边上；
- 所有顶点必须位于域内。

接口：

| 方法 | 说明 |
| --- | --- |
| `import_mesh(mesh=… / vertices=, cells=, domain=, publish=)` | 校验并落库；`publish=False` 为草稿 |
| `publish(vid)` / `discard(vid)` | 发布草稿 / 丢弃未发布草稿 |
| `edge_adjacency(vid)` / `neighbors` / `shared_edges` / `boundary_edges` | 边邻接查询 |
| `preview_split(edge, t=… / point=…)` | 只做校验与构造，**不落库** |
| `publish_split(preview)` / `split_edge(edge, t/point, publish=)` | 原子发布新版本 |
| `cell_provenance(cid)` / `trace_chain(cid)` | 新单元来源旧单元链 |
| `export(vid)` / `report(vid)` / `versions()` / `operation_log()` | 只读访问 |

`split-edge` 在边的内部插入新点，并同时拆分该边两侧的所有相邻三角形（内部边两侧各一
个、边界边一侧一个），绝不只拆一侧留下悬挂节点。边界边可细分，外边界形状不变（新点
仍落在同一条矩形边上）。新顶点/新单元 id 在一次操作内唯一，旧单元被替换，来源通过
`parent_cid` / `parent_version` / `origin_cid` 完整可追溯。

## 场仓（`FieldWarehouse`）

独立接收某已发布源网格上的单元常量密度；也可直接接收任意合法外部网格
（`MeshSnapshot`、dict 或 `MeshService` 版本），不依赖本地 split 历史。

- `register_mesh(mesh)`：重新校验并落库，记录几何指纹；
- `add_density(mesh_id, values, complete=)`：每个 `cell_id` 恰一个有限数值，可正可负；
  捕获未知 cell、重复值、`NaN/Inf`；`complete=True` 时缺值即拒绝；
- `remap(source, density_set, target, publish=True)`：
  对每个目标单元取**所有源单元的交叠面积 × 源密度之和 / 目标面积**，逐条保留非零贡献
  （`contribution` 表），面积为零的边/顶点接触不产生任何贡献；缺源值绝不按 0 处理，
  默认直接拒绝发布；目标必须被源并集完全覆盖；
- `target_densities` / `target_cell` / `contributions` / `trace` / `trace_all`：
  查目标单元密度及其源贡献；关闭重开仍可查；
- `integrate(run_id, rect)`：按目标三角形与查询矩形**实际交集**求积分，返回积分、
  覆盖面积、越界面积；
- `integrate_source(density_set, rect)`：独立直接对源网格积分，用作守恒对照；
- `is_run_stale(run_id, source=, target=)`：比较 run 固定指纹与当前网格指纹。

### Run 的不可变性与失效

一个 run 固定 `source_mesh_id`、`density_set_id`、`target_mesh_id` 及三者的几何指纹，
贡献矩阵写入 `contribution`。真实源网格细化或密度更正后，通过**新 run** 重建贡献关系：
旧 run 的源/目标身份、密度值和面积矩阵保持不变，绝不会在网格更新后沿用旧面积矩阵。
`is_run_stale` 明确报告旧 run 对新网格已失效。

## 测试覆盖

`tests/test_remap.py` 覆盖题面要求：

- **手算贡献矩阵**：矩形切成两三角，源密度 `+3/-2`，目标在对角线中点各拆一半，
  每个目标单元贡献恰为 `1/2`，密度复制，全域积分 `1`。
- **共享边细分**：同时拆两侧，邻接与谱系正确；**边界边细分**不改变外边界。
- **负例**：悬挂节点、正面积重叠、覆盖缺口、退化三角形、域外边界节点、非法 split 参数；
  拒绝后版本、单元、操作日志均不变，重开仍为原网格。
- **跨源边目标单元**：反向对角线目标单元同时含多个源贡献。
- **正负抵消**：混合符号目标密度，加权项相消，全域守恒。
- **边界触碰**：仅共享边/点的单元不产生贡献（不落零面积贡献行）。
- **缺值场**：不完整密度集无法发布；草稿 run 无目标密度、不可积分、不可发布。
- **常量场不变**：重映射后处处等于常量且总量守恒。
- **独立对源积分比对**：300 源 / 300 目标 / 50 个随机查询，全域积分与直接源积分一致，
  子区域积分可加且按实际交集计算。
- **持久化**：关闭重开后网格、密度、run、贡献 trace 全部可查。
- **旧矩阵失效**：真实源细分后旧 run 指纹/矩阵不变且被标记 stale，新 run 指纹与新
  贡献矩阵不同。

## 声明的约定

- 几何有效性：精确有理数，单元逆时针规范化，正面积严格 `> 0`，并集面积精确等于域面积。
- 面积：精确有理数；`intersection_area` 为凸多边形 Sutherland–Hodgman 精确裁剪。
- 守恒：全域 `sum(target_area * target_density) == sum(source_area * source_density)`，
  相对差默认容差 `1e-12`（精确计算下实际差为 0）。
- 子区域积分**不要求**等于直接源积分（目标密度是单元平均值）；要求的性质是
  “按实际交集计算”“覆盖面积正确”“子区域可加”“全域严格守恒”。
