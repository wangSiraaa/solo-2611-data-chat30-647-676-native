# 能谱刻度服务与衰变校正计数仓（Python 3.11）

纯本地合成实现，不连接任何设备，也不含辐射安全或医学解释。几何量
（通道、能量、覆盖长度、效率、时长）全部用 `fractions.Fraction` 精确表示；
指数积分与强度反解为超越运算，使用 `float` 并按 1e-8 绝对/相对容差核对。

## 目录结构

```
spectrum_svc/
  rationals.py    有理数解析与校验（精确十进制/分数，拒绝 1e3 类输入）
  geometry.py     半开区间与整通道箱 [j, j+1) 的精确覆盖几何
  calibration.py  分段线性刻度产品（不可变、带内容哈希、可移植 JSON）
  catalog.py      刻度目录 SQLite：私有校准点、版本、产品发布（事务原子）
  decay.py        稳定衰变积分、活度反解、参考时刻换算（lambda=0 极限）
  repository.py   计数仓 SQLite：公开产品、谱、逐箱结果、净值与结果缓存
  cli.py          交付命令
tests/test_core.py   13 项测试，可独立运行（无需 pytest）
examples/run_examples.py  题目要求的全部情形与手算核对
```

两个 SQLite 文件物理隔离：目录库保存**私有**校准点表；仓库库只保存
**已发布产品**，从不读取私有点表。仓库可通过 `product-import` 接受外部
同格式产品，导入时重新校验单调性、分段与产品 ID。

## 关键语义

- **分段线性、点外不可用**：通道与能量均严格递增；任何超出端点的投影
  直接报错，绝不外推。
- **按覆盖长度分箱，不按箱中心整箱进出**：能窗先反投影为通道上的
  半开区间 `[c_lo, c_hi)`，再对每个整箱求交；计数
  `N = Σ_j 计数[j] × 覆盖长度_j`，全程精确分数。能量均匀不代表通道均匀，
  跨折点窄能窗会在折点两侧按不同斜率切分（见示例 1）。
- **INCOMPLETE 不当零**：能窗越出刻度能量域，或反投影落到谱未测量的
  通道箱时，整窗标记 INCOMPLETE，不产出活度；背景不完整则净值不完整。
- **衰变模型**（有效采集在实测时段均匀）：
  `N = eff·(L/T)·A·I(T,λ)`，`I = ∫₀ᵀ exp(-λt)dt`；
  小 `λT` 走 `expm1` 防相消，`λ=0` 取极限 `I=T`，不除零。
- **参考时刻**：`A_ref = A·exp(-λ(t_ref-start))`；背景用自己的日期与
  L/T 先换算到同一 `t_ref` 再相减，负净值保留不截零。
- **可回查**：每条结果保存逐箱覆盖、原始计数、L/T 时长修正、积分 I、
  起止活度与参考时刻；`verify` 从原始输入完整重算并逐字段比对。
- **重刻度**：新产品有新内容哈希；新谱引用新产品后能窗分数与净强度
  真实改变，旧结果按原产品与原身份保留且仍可核对。

限额：每谱 ≤4096 通道、≤50 谱、≤8 校准点、每次 ≤10 能窗。

## 命令一览

有理数直接写 `1/3`、`2.5`、`7`；计数支持逗号串或 `@文件`（.npy/.txt）。

```bash
# 刻度编辑与发布（失败整体不变）
python3 -m spectrum_svc calib-put det 0,10 10,20 20,30
python3 -m spectrum_svc calib-list
python3 -m spectrum_svc calib-get det
python3 -m spectrum_svc calib-publish det
python3 -m spectrum_svc product-list
python3 -m spectrum_svc product-export <product_id> > product.json
python3 -m spectrum_svc project <product_id> --energy 31/2 69/4   # 反投影
python3 -m spectrum_svc project <product_id> --channel 5 7        # 正投影

# 仓库：外部产品、谱导入、定量、核对
python3 -m spectrum_svc product-import product.json
python3 -m spectrum_svc spectrum-import s1 det <pid> 1000 21 21 @counts.txt
python3 -m spectrum_svc spectrum-list
python3 -m spectrum_svc quantify s1 31/2,69/4 --eff 1 --lambda 0 --t-ref 1000
python3 -m spectrum_svc quantify-net s1 bg 15,16 --eff 1/10 --lambda 1e-4 --t-ref 0
python3 -m spectrum_svc result-list
python3 -m spectrum_svc verify <result_id>
```

数据库路径可用全局 `--catalog` / `--repo` 覆盖，默认 `data/` 下。

## 手算核对（常数刻度）

刻度 `E = C + 10`（点 (0,10),(10,20),(20,30)），21 个箱每箱计数 100，
`λ=0、L=T=21、eff=1`。能窗 `E∈[15.5,17.25)` 反投影为通道
`[5.5,7.25)`，覆盖箱 5/6/7 的长度为 `1/2 + 1 + 1/4 = 7/4`，故
`N = 100×7/4 = 175`，`A = N/T = 175/21 ≈ 8.3333`。反向用 `A=100` 经
前向模型生成的总计数恰为 `100×21 = 2100`，与原始谱合计一致
（`examples/run_examples.py` 场景 1b 自动核对）。

## 运行

```bash
python3 tests/test_core.py            # 13/13 独立测试
python3 examples/run_examples.py      # 跨折点窄窗、异日背景、λ=0、
                                      # 零计数、刻度缺口、重刻度与缓存核对
```

测试覆盖：跨刻度折点窄能窗、不同测量日期背景相减、`lambda=0` 极限、
零计数、能量域与测量域两类刻度缺口、重刻度改变分数且旧结果身份不变、
篡改外部产品被拒、全部限额、能窗重叠校验与结果篡改重算。
