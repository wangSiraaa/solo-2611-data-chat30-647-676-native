# 降雨响应核与河网汇流事实仓

精确有理数离散模型（Python 3.11）。维护有向无环河网响应目录与离散汇流
事实仓，**不连接任何水文服务、不作安全决策、不做 DEM/非线性蓄洪/真实
降雨预报**。全部体积、面积、核系数、比例使用 `fractions.Fraction`，
SQLite 中以规范有理文本 `p/q` 冻结，全链路无浮点近似（显式拒绝 float）。

## 模型语义（与题面逐条对应）

1. **节点**是本地汇水区：正有理面积 `A`、有限非负响应核 `k[0..m]`，
   核之和严格等于 1（核长 ≤ 20）。
2. **有向边**给非负整数延迟 `d` 与流出比例 `p`；同节点出边比例之和
   `≤ 1`，差额即「离开本模型」。出边 ≤ 3 条，节点 ≤ 30。
3. **本地水体积** `V = A * max(r-l, 0)`；有效量 `max(r-l, 0)`。
   本地产流 = 有效量序列与本节点核的卷积。
4. **节点总出流 = 本地产流 + 所有父节点出流按边延迟、比例传递之和。**
   上游传来的体积**不再乘接收节点面积或响应核**——边只做移位与比例。
5. **零延迟边**在同一时间步内按 DAG 拓扑序即时求值（父先于子）。
6. **未收到数据 ≠ 零**：请求指定完整节点与 `[L,R)`，闭包（请求节点+
   全部祖先）缺任一必需格即返回 `INCOMPLETE`，不补零、不产出曲线。
7. **尾部不截断**：输入覆盖 `[L,R)`，输出延伸到
   `T = R-1 + (max核长-1) + 全网最长路径延迟`，核尾与延迟尾全部落账，
   不能在 R 处截断并宣称水量丢失。
8. **守恒**：逐节点 `本地体积+上游来水 = 离开+出边流量`；全局
   `输入体积 = 全网离开体积`，残差必须精确为 0。
9. **多路径不去重**：分流后重汇时，同一降雨格经不同真实路径的份额
   分别列示并相加，不按降雨 id 去重。
10. **冻结身份**：目录与降雨批次内容寻址（SHA-256），各自冻结；
    核/延迟/降雨修订产生新版本与新曲线，旧分析永远绑定旧产品哈希。
11. **目录独立**：单位脉冲查询只依赖冻结目录，不经过降雨仓库；
    可接收外部同格式 `hydro-catalog/1` 产品并复验身份。

## 模块

| 模块 | 职责 |
|---|---|
| `hydro/rational.py` | 有理数转换与规范文本，拒绝浮点 |
| `hydro/graph.py` | 拓扑排序、全节点对最长延迟路径（NumPy，带纯 Python 回退） |
| `hydro/catalog.py` | `Node/Edge/Catalog/CatalogDraft`，发布前全量校验、单位脉冲 |
| `hydro/storage.py` | SQLite 事实仓：目录版本、降雨版本、格点、分析档案 |
| `hydro/rainfall.py` | 降雨/损失批次导入、格点修订、完整矩形缺格校验 |
| `hydro/engine.py` | 卷积、零延迟拓扑汇流、尾部时域、峰值、精确守恒对账 |
| `hydro/explain.py` | 来源分解：路径级与 `(起源节点,输入步)` 聚合，逐格对账 |
| `hydro/cli.py` | 命令行入口 |
| `hydro/validate.py` | 10 个自建案例（逐水团手算、零延迟、分流重汇、核尾、损失、修订、INCOMPLETE、发布失败、外部产品） |
| `tests/` | 48 个 unittest 用例 + 随机暴力交叉验证 |

## 运行

```bash
# 内置验证（10 个案例，含独立逐水团手算核对）
python -m hydro.validate

# 单元测试（标准库 unittest）
python -m unittest discover -s tests -v

# 随机压力测试：引擎 vs 独立路径枚举暴力实现，逐格精确比对
PYTHONPATH=. python tests/stress_crosscheck.py
```

## CLI 入口（交付的五类入口）

```bash
# 1. 目录编辑/发布（发布失败不产生新版本，旧产品不变）
python -m hydro.cli catalog-node --db h.db --node a --area 2 --kernel 1 --publish
python -m hydro.cli catalog-edge --db h.db --catalog <hash> --from a --to b \
    --delay 1 --proportion 3/4
python -m hydro.cli catalog-import  --db h.db --file external.json --expect-hash <sha256>
python -m hydro.cli catalog-export  --db h.db --catalog <hash>
python -m hydro.cli pulse           --db h.db --catalog <hash> --node a   # 不依赖仓库

# 2. 降雨导入/修订（缺格拒绝；修订产生显式新版本，父版本保留）
python -m hydro.cli rain-import  --db h.db --catalog <hash> --lo 0 --hi 2 \
    --nodes a,b --cell a:0:2:0 --cell a:1:0:0 --cell b:0:0:0 --cell b:1:0:0
python -m hydro.cli rain-revise --db h.db --catalog <hash> --rain <rh> \
    --node a --time 0 --rain-value 0

# 3. 分析（含 INCOMPLETE、尾部延伸、逐节点报告、全局守恒对账）
python -m hydro.cli analyze --db h.db --catalog <hash> --rain <rh> \
    --nodes b --lo 0 --hi 2

# 4. 来源解释（某时刻出流来自哪些节点/输入步/真实路径）
python -m hydro.cli explain --db h.db --catalog <hash> --rain <rh> \
    --node b --time 2

# 5. 验证入口
python -m hydro.cli validate
```

有理量一律传整数或 `p/q`（如 `1/2`、`3/4`），CLI 不接受小数。

## 程序化用法

```python
from fractions import Fraction as Q
from hydro import CatalogDraft, Store, RainRepo, Engine, AnalysisRequest, SourceExplainer

d = CatalogDraft()
d.add_node("a", 2, [1])
d.add_node("b", 3, [Q(1, 2), Q(1, 2)])
d.add_edge("a", "b", 1, Q(3, 4))          # b 另 1/4 离开模型
cat = d.publish()                          # 发布前校验：无环/核和=1/比例<=1/限额

store = Store("h.db")
rain = RainRepo(store, cat).import_batch(
    0, 2, ["a", "b"],
    {("a", 0): (1, 0), ("a", 1): (0, 0),
     ("b", 0): (0, 0), ("b", 1): (0, 0)},
)
report = Engine(cat).analyze(AnalysisRequest.make(("b",), 0, 2), rain, store)
assert report.conserved and report.residual == 0

expl = SourceExplainer(cat, rain).explain("b", 1)   # 路径级来源
print(expl.by_source)
```

## 设计要点

- **稀疏传播**：只对非零有效量建水团，核系数为 0 的项跳过；序列用
  定长列表承载（规模 ≤ 30 节点 × 200 步），路径枚举带记忆与上限保护。
- **选择性重算**：分析身份由 `(目录哈希, 降雨哈希, 请求)` 决定，
  任一修订都得到新 `analysis_id`；旧报告以规范 JSON 整体归档，可重放。
- **NumPy 可选**：最长延迟路径矩阵用 NumPy Floyd 松弛加速；环境无
  NumPy 时自动回退等价纯 Python 实现，结果逐格一致（有测试覆盖）。
- **发布事务语义**：草稿期允许任意编辑，所有语义校验集中在 `publish()`；
  失败抛错且不写入任何新版本，已冻结产品永不被覆盖。
