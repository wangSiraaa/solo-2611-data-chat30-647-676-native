# 调查样本的有界权重校准与分组统计发布

从空项目实现的、可重开的调查数据加工工具。全部输入数据均为**合成虚构数据**，
不采集任何真实个人数据。工具包含两个相互独立、各自带实质逻辑的源码模块：

| 模块 | 职责 | 是否需要指标回答 |
| --- | --- | --- |
| `surveycal/calibration.py` | 登记设计字段/基础权重/控制数/边界，生产并独立复核校准权重 | 否 |
| `surveycal/statistics.py` | 只消费固定的权重产品，发布带依据的分组统计 | 是，但绝不重新校准 |

## 1. 数学问题

设 n 名样本，基础权重 d_j > 0，校准权重 w_j；m 个**允许互相重叠**的控制单元，
0/1 成员矩阵 A∈{0,1}^{m×n}，控制目标 b_i ≥ 0，每人权重比闭区间 [lo_j, hi_j]：

```
minimize  F(w) = Σ_j (w_j - d_j)^2 / d_j                 （严格凸，解唯一）
subject to A w = b,
           d_j·lo_j ≤ w_j ≤ d_j·hi_j,   lo_j, hi_j > 0, lo_j ≤ hi_j
```

### 求解方案（算法可替换，发布只认真复核）

1. **结构化预检**（无需求解器）：必要单元无样本、逐单元边界容量不足、
   同成员集合目标冲突，直接给出带相关约束的不可行判定。
2. **主求解器：原始主动集 KKT 法**。自由变量上解闭式等式约束 QP

   ```
   w_F = d_F + D_F A_F' (A_F D_F A_F')⁺ (rhs - A_F d_F)
   ```

   违反盒约束的变量钉到边界、符号条件错误的活动变量被释放，严格凸下有限步收敛。
   重叠/冗余控制使 KKT 矩阵奇异时用最小二乘求伪逆——w 仍唯一，仅乘子不唯一。
3. **交叉印证：拉格朗日对偶 L-BFGS-B**。独立第二条算法路径，结果仅作旁证，
   其返回码从不作为发布依据。
4. **独立复核**（`verify_weights` / `verify_product`）：重新计算每个控制单元残差、
   每条权重边界、卡方目标值，并用一个**带符号约束的线性可行性规划（HiGHS LP）**
   独立检验 KKT 平稳性（正确处理活动边界点与秩亏损约束，不依赖求解器乘子）。

### 三态结论（不可行 ≠ 未决）

- `PUBLISHABLE`：独立复核全部通过才发布；
- `INFEASIBLE`：有证据地判定不可行（预检或最小残差诊断），附原因、相关约束与依据，
  绝不静默丢约束、补造样本人或放宽边界；
- `UNDECIDED`：数值预算内未能判定，单独报告，**不**当作数学不可行；
- `DIAGNOSTIC`：`--diagnostic` 附带的最小残差放宽结果，产品显式打标，禁止冒充正式权重。

### 数值精度与容差（随产品发布，违反即不发布）

| 量 | 容差 |
| --- | --- |
| 控制残差 | 绝对 1e-6，或相对目标 1e-8 |
| 权重边界 | 绝对 1e-9，或相对边界 1e-9 |
| KKT 平稳性 | 绝对 1e-7，或相对梯度尺度 1e-8 |
| 相对对偶间隙 | 1e-10 |
| 结构化预检 | 1e-12 相对尺度 |

## 2. 稀疏/稠密与性能取舍

- 成员矩阵按 `n·m` 阈值（25 万元素）自动在稠密 ndarray 与 CSR 稀疏矩阵间切换；
  稀疏路径只在 m×m 小矩阵上做线性求解，成员矩阵乘法保持稀疏。
- 5000 人 × 100 控制单元实测稀疏路径校准约 0.8 秒（见 `large_5000` 案例）。
- 统计端为按需聚合（按组遍历一次权重），不预建立方体；产品 JSON/SQLite 快照自包含，
  外部消费方无需原始样本文件。产品带 `schema_version`，字段演进不破坏旧产品可读性。

## 3. 统计规则

- 某指标**缺失**的人不进入该指标均值的分子与分母，但其权重单独报告：
  `总体权重 = 有效回答权重 + 缺失权重`；
- **零是真实回答**，正常进入分子分母，与缺失严格区分（CSV 中空白=缺失，`0`=零）；
- 有效回答权重（分母）为零 => 均值与加权总量显式 `无结果`，绝不把缺失填零；
- 同一人的不同指标可有不同有效分母；缺少某人回答按缺失处理；
- 回答身份校验：未知 ID、重复 `(person_id, indicator)` 整批拒绝；
- 报告绑定 `product_id`；指标更换复用同一权重产品；控制更正确保产生**真实改变
  下游统计**的新产品（`assert_product_drives_change` 拒绝只换标签的重发布）。

## 4. 持久化与产品不可变

SQLite 单文件（`repository.py`），产品表只插入不更新：权重产品固定后不随源更正改变，
控制更正产生新版本（revision 自增）与新产品行。可从产品/报告汇总经外键追溯到
**每个参与样本**的基础权重、校准权重、权重比与逐单元残差。

## 5. 命令清单

```bash
export PYTHONPATH=$(pwd)
python -m surveycal generate      --out data/                 # 生成全部合成案例
python -m surveycal import        --db w.db --project p --sample s.csv --controls c.json
python -m surveycal import-resp   --db w.db --project p --responses r.csv
python -m surveycal calibrate     --db w.db --project p [--diagnostic] [--maxiter N]
python -m surveycal publish       --db w.db --project p [--export wp.json]
python -m surveycal import-product --db w2.db --project ext --product wp.json
python -m surveycal report        --db w.db --project p --product-id ID --group region||age_group
python -m surveycal query         --db w.db --project p products|reports|report ID|trace ID|cells ID
python -m surveycal verify        --db w.db --project p --product-id ID
```

## 6. 交付的案例（`generate` 产出）

| 案例 | 验证点 |
| --- | --- |
| `base_satisfied` | 基础权重已满足控制，校准原样返回，目标≈0 |
| `joint_marginal` | 地区/年龄/交叉组三层边际联合调整 |
| `bounds_active` | 两人顶到权重比上界，KKT 活动集生效 |
| `empty_unit` | 必要单元无样本 => 不可发布 |
| `contradiction` | 同成员异目标 => 控制矛盾，不可发布 |
| `missing_responses` | 缺失与零区分、分母为零无结果 |
| `rearranged` | 行/控制顺序重排，权重与统计不变 |
| `control_revision` | 控制更正 => 新产品且下游联动 |
| `large_5000` | 5000 人/100 单元/20 指标，稀疏路径 |
| `handcalc` | 2–4 人 Fraction 手算最优权重、目标与残差 |

## 7. 测试

```bash
pip install numpy scipy pytest
python -m pytest tests/ -q     # 38 个测试：手算核、三态、边界、缺失、重排、持久化、CLI
```

## 8. 明确不做

不做置信区间、因果推断；不采集真实个人数据；诊断性放宽结果不充当正式权重；
数值未决不冒充数学不可行。
