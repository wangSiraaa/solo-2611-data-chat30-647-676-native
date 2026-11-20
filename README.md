# polcal — 独立通道标定服务 + 观测事实仓（有限合成模型）

本仓库只实现题定**有限合成模型**，不连接任何仪器，不承诺真实物理测量精度，
所有数字都不构成医学、天文或真实仪器测量结论。

    s = (I + Q·cos(2θ) + U·sin(2θ)) / 2        # 理想信号
    y = g·s + b,   g > 0                        # 通道读数

- θ 为通道固定角度（度），按 180° 周期等价（`canonical_theta` 归一到 `[0,180)`）。
- 标定服务与观测仓为两个独立 SQLite 存储，观测仓**只从产品**取参数，不读标定参考。

## 目录

```
polcal/
  model.py         数值核心：拟合、秩判定、加权反演、约束检验、偏振报告、产品校验
  calibration.py   标定服务：参考/测量、不可变产品发布、外部产品导入、追溯
  warehouse.py     观测仓：曝光/读数、产品固定、反演 run、重开/重算、追溯
  demo.py          端到端演示（手算 I/Q/U + 独立回代 + 全部要求案例 + 真实撤回）
  tests/test_polcal.py
artifacts/         demo 生成的 summary.json / products.json / runs.json
```

## 安装 / 运行

```bash
python3 -m venv .venv
.venv/bin/pip install numpy scipy pytest
.venv/bin/python -m pytest polcal/tests -q      # 36 tests
.venv/bin/python -m polcal.demo                 # 生成 artifacts/
```

## 关键设计

### 1. 标定服务（`CalibrationStore`）

- **参考身份**是 `reference_id`。同一参考可多次独立测量：每次生成新的
  `measurement_id`，不是重送。`record_measurement(..., correct=True)` 会先对参考
  做版本自增（参考更正），再记录本次测量。
- **每通道一元最小二乘**：`y = g·s + b`，`s` 由该通道 θ 与参考的已知 I/Q/U 算出。
  - 要求 ≥2 个**不同** `s`，且拟合增益 `g>0`；否则该通道 `uncalibratable`，并保留
    `reason`（`insufficient_measurements` / `insufficient_distinct_s` /
    `design_rank_deficient_at_configured_tolerance` / `gain_nonpositive`）。
  - 保留 `residual_ss`、`r_squared`、拟合参数、以及**每个参考点的 s/y/残差**
    （`product_fit_points` 表，供 `trace_product` 审计）。增益非正时保留
    `attempted_gain/attempted_offset`。
- **产品不可变**：`publish()` 对当前活跃测量做快照拟合，写入新产品（版本号自增）。
  SQLite 触发器禁止对 `products` / `calibration_designs` / `product_fit_points` 的
  UPDATE/DELETE。增删/更正参考、撤回测量都会让下一次发布产生**新版本**，旧产品不变。
- **产品是自洽参数包**：仅含每通道 θ、状态、g、b、点数、残差、`reason` 等，
  **不含任何参考读数**。普通客户可用 `apply_channel_correction(payload, ch, y)`
  只凭产品完成 `s=(y-b)/g` 线性校正，无需观测仓。
- **外部产品**：`import_product(payload)` 校验同格式（`polcal.product.v1`，
  `model_version` 必须一致，校准通道 g>0，不可校准通道须有 reason）后以独立
  `kind='external'` 版本序列持久化；观测仓可直接消费其 payload。

### 2. 观测事实仓（`ObservationWarehouse`）

- 曝光 `exposure_id` 唯一；`(exposure_id, channel)` 主键保证**每通道最多一条**读数；
  `weight>0` 由 CHECK 与校验双重保证。缺测通道就是不存在的行。
- **反演真读一份完整产品**，以 `product_id / version / sha256` 固定到 run。
  资格判定：
  - 读数通道不在产品 → `qc.not_in_product`
  - 产品中该通道 `uncalibratable` → `qc.uncalibratable`（附 reason）
  - 产品中已校准但曝光无读数 → `qc.missing`
  - **绝不补零**：候选仅限“已校准且在曝光中存在”的通道。
- **秩判定**：设计矩阵 `A=[1, cos2θ, sin2θ]`，对 `x=2s` 做加权最小二乘
  （`sqrt(w)` 加权）。秩 = `count(sv > rank_tol·sv[0])`，`rank_tol` 在配置里且
  每个 run 都记录。要求 `rank==3` 且候选 ≥ `min_channels_for_inversion`（默认 3）；
  否则 `rank_deficient` / `insufficient_channels`，不当作确定解（近共线角度不通过）。
- **约束检验**：`I ≥ 0` 且 `Q²+U² ≤ I²`，容差为 `constraint_atol + constraint_rtol·scale`
  并在解中记录。超容差 → `status=model_inconsistent`，**保留无约束最小二乘解**，
  不做裁剪，`DOLP/角度` 标为无定义。绝不把裁剪到可行区的解称为原始观测一致。
- **报告**：合法解给出 `DOLP=sqrt(Q²+U²)/I`（`I≈0` 时无定义），
  `angle=atan2(U,Q)/2` 规范到 `[0,180)`（`Q=U=0` 时无定义）。每通道保留
  `corrected_s / predicted_s / predicted_y_raw / residual / weighted_residual_sq`。
- **run 不可变**（触发器），可 `reopen_run` 校验 `solution_sha256` 与产品 `sha256`；
  `rerun(prior_run_id, new_payload)` 生成新 run 并 `reof_of` 关联，旧 run 仍绑定旧产品。

### 3. 数值容差（`DEFAULT_CONFIG`，产品与 run 都记录）

| 键 | 默认 | 用途 |
|---|---|---|
| `rank_tol` | `1e-10` | 所有矩阵秩判定（相对奇异值阈值） |
| `constraint_atol/rtol` | `1e-9/1e-9` | 模型一致性 `I≥0, Q²+U²≤I²` |
| `zero_tol` | `1e-12` | `I≈0`（DOLP 无定义）/`Q=U=0`（角度无定义） |
| `distinct_atol/rtol` | `1e-12` | 拟合判“不同 s”的诊断 |
| `min_channels_for_inversion` | `3` | 反演最少候选通道 |

## 演示输出要点（`artifacts/summary.json`）

- 16 通道 / 100 参考：恢复增益最大误差 ~1.6e-15，残差 ~1e-28。
- 手算 I=1,Q=0.3,U=-0.4：θ=0/45/90/135 的 s=0.65/0.30/0.35/0.70，回代误差 ~1e-16。
- 1000 曝光反演：I/Q/U 最大恢复误差 ~3e-6（读数含 1e-6 量级独立扰动）。
- 场景：正交可解、全角度重复秩亏、增益错误（残差暴露）、增益拟合非正（拒收）、
  零强度（DOLP/角度无定义）、合法边界（DOLP=1）、模型不一致（保留无约束解）、
  缺测/不可校准通道单独去向、近共线秩亏。
- **真实撤回**：向 R042 注入不一致参考后发布 v6（增益被偏置），撤回 R042 后发布 v7；
  同一曝光 rerun 的 Q 变化，旧 run 仍绑定 v6 且解不变。

所有输出均明确只表示题定模型，不作医学或天文判断。
