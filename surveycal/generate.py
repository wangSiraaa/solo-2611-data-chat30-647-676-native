"""合成数据生成器：自行生成样本 CSV、控制数 JSON 与回答 CSV，不采集真实个人数据。

生成的案例（对应交付要求）：

==========================  =====================================================
场景                         用途
==========================  =====================================================
base_satisfied              基础权重已满足全部控制（校准应原样返回）
joint_marginal              联合边际调整（地区、年龄、交叉组同时约束）
bounds_active               边界生效（最优解被权重比区间夹住）
empty_unit                  必要单元无样本 => 不可发布
contradiction               控制矛盾 => 不可发布
missing_responses           回答含缺失与零值，验证分母规则
rearranged                  同一数据的行重排版本（统计必须不变）
control_revision            控制更正后的新版本（产生新产品且下游联动）
large_5000                  5000 人 / 100 控制单元 / 20 指标（稀疏路径）
==========================  =====================================================

另有 ``handcalc`` 目录：2 至 4 人的严格凸小问题，附分数形式的手算最优权重。
"""

from __future__ import annotations

import csv
import json
import os
from fractions import Fraction

import numpy as np

REGIONS = ("R1", "R2", "R3", "R4", "R5")
AGES = ("A1", "A2", "A3", "A4", "A5", "A6")


def _write_sample(path: str, rows: list[dict]) -> None:
    cols = ["person_id", "base_weight", "region", "age_group", "lo", "hi"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in cols})


def _write_controls(path: str, controls: list[dict], lo: float, hi: float) -> None:
    doc = {
        "schema_version": 1,
        "design": {"region": list(REGIONS), "age_group": list(AGES)},
        "bounds": {"lo": lo, "hi": hi},
        "controls": controls,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)


def _write_responses(path: str, rows: list[tuple[str, str, str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["person_id", "indicator", "value"])
        writer.writerows(rows)


# ---------------------------------------------------------------------------
def gen_base_satisfied(out: str) -> None:
    """基础权重天然满足控制：所有目标按基础权重之和构造。"""
    rng = np.random.default_rng(101)
    rows, by_region, by_age, by_cell = [], {}, {}, {}
    total = 0.0
    for i in range(120):
        region = REGIONS[i % len(REGIONS)]
        age = AGES[(i // len(REGIONS)) % len(AGES)]
        bw = round(float(rng.uniform(2, 8)), 4)
        rows.append({"person_id": f"P{i:04d}", "base_weight": bw, "region": region, "age_group": age})
        by_region[region] = by_region.get(region, 0.0) + bw
        by_age[age] = by_age.get(age, 0.0) + bw
        by_cell[f"{region}|{age}"] = by_cell.get(f"{region}|{age}", 0.0) + bw
        total += bw
    controls = [{"code": "TOTAL", "target": round(total, 6), "selector": {}}]
    controls += [
        {"code": f"REG_{r}", "target": round(by_region[r], 6), "selector": {"region": r}}
        for r in REGIONS
    ]
    controls += [
        {"code": f"AGE_{a}", "target": round(by_age[a], 6), "selector": {"age_group": a}}
        for a in AGES
    ]
    controls += [
        {"code": f"CELL_{r}_{a}", "target": round(by_cell[k], 6),
         "selector": {"region": r, "age_group": a}}
        for (r, a), k in ((tuple(k.split("|")), k) for k in sorted(by_cell))
    ]
    _write_sample(os.path.join(out, "sample.csv"), rows)
    _write_controls(os.path.join(out, "controls.json"), controls, 0.5, 2.0)


def gen_joint_marginal(out: str) -> None:
    """目标相对基础权重有偏移，需联合调整同时满足三层边际。

    可行性构造：区域与年龄边际都以*同一总体缩放系数* k 等比缩放，
    交叉单元目标直接取基础单元总量（恒可行），保证控制方程相容；
    边界 [0.5, 1.8] 留有充足调整空间，调整后权重与基础权重不同。
    """
    rng = np.random.default_rng(202)
    rows = []
    for i in range(150):
        rows.append(
            {
                "person_id": f"P{i:04d}",
                "base_weight": round(float(rng.uniform(1, 5)), 4),
                "region": REGIONS[rng.integers(0, len(REGIONS))],
                "age_group": AGES[rng.integers(0, len(AGES))],
            }
        )
    k = 1.08  # 总体扩张 8%，区域/年龄边际同比例 => 边际方程天然相容
    total_bw = sum(r["base_weight"] for r in rows)
    controls = [{"code": "TOTAL", "target": round(total_bw * k, 6), "selector": {}}]
    for r in REGIONS:
        base = sum(x["base_weight"] for x in rows if x["region"] == r)
        controls.append(
            {"code": f"REG_{r}", "target": round(base * k, 6), "selector": {"region": r}}
        )
    for a in AGES:
        base = sum(x["base_weight"] for x in rows if x["age_group"] == a)
        controls.append(
            {"code": f"AGE_{a}", "target": round(base * k, 6), "selector": {"age_group": a}}
        )
    # 显式交叉单元（部分组合，模拟重叠且不完整的交叉控制），目标取基础单元总量
    for r in ("R1", "R3", "R5"):
        for a in ("A1", "A3", "A6"):
            base = sum(
                x["base_weight"] for x in rows if x["region"] == r and x["age_group"] == a
            )
            controls.append(
                {"code": f"CELL_{r}_{a}", "target": round(base, 6),
                 "selector": {"region": r, "age_group": a}}
            )
    _write_sample(os.path.join(out, "sample.csv"), rows)
    _write_controls(os.path.join(out, "controls.json"), controls, 0.5, 1.8)


def gen_bounds_active(out: str) -> None:
    """目标迫使两人同时顶到上界，验证边界生效与 KKT 活动集。"""
    rows = [
        {"person_id": "P1", "base_weight": 1.0, "region": "R1", "age_group": "A1"},
        {"person_id": "P2", "base_weight": 2.0, "region": "R1", "age_group": "A1"},
        {"person_id": "P3", "base_weight": 3.0, "region": "R2", "age_group": "A2"},
    ]
    controls = [
        {"code": "TOTAL", "target": 9.0, "selector": {}},
        {"code": "REG_R1", "target": 6.0, "selector": {"region": "R1"}},  # 两人上界和=6
    ]
    _write_sample(os.path.join(out, "sample.csv"), rows)
    _write_controls(os.path.join(out, "controls.json"), controls, 0.8, 2.0)


def gen_empty_unit(out: str) -> None:
    """登记一个样本中不存在的类别单元，目标为正 => 必要单元无样本。"""
    rows = [
        {"person_id": "P1", "base_weight": 1.0, "region": "R1", "age_group": "A1"},
        {"person_id": "P2", "base_weight": 2.0, "region": "R2", "age_group": "A2"},
    ]
    controls = [
        {"code": "TOTAL", "target": 3.0, "selector": {}},
        # R5 在登记类别中合法，但样本里没有人落在 R5
        {"code": "REG_R5_EMPTY", "target": 10.0, "selector": {"region": "R5"}},
    ]
    _write_sample(os.path.join(out, "sample.csv"), rows)
    _write_controls(os.path.join(out, "controls.json"), controls, 0.5, 2.0)


def gen_contradiction(out: str) -> None:
    """同批人被两个控制单元要求不同总量 => 控制矛盾（边界放宽也无解）。"""
    rows = [
        {"person_id": "P1", "base_weight": 1.0, "region": "R1", "age_group": "A1"},
        {"person_id": "P2", "base_weight": 1.5, "region": "R1", "age_group": "A2"},
        {"person_id": "P3", "base_weight": 2.0, "region": "R2", "age_group": "A1"},
    ]
    controls = [
        {"code": "TOTAL", "target": 4.5, "selector": {}},
        # 与 TOTAL 完全同成员、不同目标；即使无边界也不可能同时成立
        {"code": "TOTAL_CONFLICT", "target": 9.0, "selector": {}, "label": "矛盾副本"},
        {"code": "REG_R1", "target": 2.5, "selector": {"region": "R1"}},
    ]
    _write_sample(os.path.join(out, "sample.csv"), rows)
    _write_controls(os.path.join(out, "controls.json"), controls, 0.1, 10.0)


def gen_missing_responses(out: str) -> None:
    """小样本 + 含缺失/零值的回答，专门验证统计端分母规则。"""
    rows = [
        {"person_id": "P1", "base_weight": 1.0, "region": "R1", "age_group": "A1"},
        {"person_id": "P2", "base_weight": 2.0, "region": "R1", "age_group": "A2"},
        {"person_id": "P3", "base_weight": 3.0, "region": "R2", "age_group": "A1"},
        {"person_id": "P4", "base_weight": 4.0, "region": "R2", "age_group": "A2"},
    ]
    controls = [
        {"code": "TOTAL", "target": 10.0, "selector": {}},
        {"code": "REG_R1", "target": 3.0, "selector": {"region": "R1"}},
        {"code": "REG_R2", "target": 7.0, "selector": {"region": "R2"}},
    ]
    _write_sample(os.path.join(out, "sample.csv"), rows)
    _write_controls(os.path.join(out, "controls.json"), controls, 0.5, 2.0)
    # income: P2 缺失（空白），P3 为真实零值；score: P4 缺失，P1 零值
    resp = [
        ("P1", "income", "100"),
        ("P2", "income", ""),
        ("P3", "income", "0"),
        ("P4", "income", "40"),
        ("P1", "score", "0"),
        ("P2", "score", "8"),
        ("P3", "score", "5"),
        ("P4", "score", ""),
    ]
    _write_responses(os.path.join(out, "responses.csv"), resp)


def gen_rearranged(src_out: str, out: str) -> None:
    """把 joint_marginal 的样本行逆序、控制单元顺序打乱，统计结果必须不变。"""
    with open(os.path.join(src_out, "sample.csv"), encoding="utf-8") as fh:
        reader = list(csv.DictReader(fh))
    os.makedirs(out, exist_ok=True)
    reversed_rows = list(reversed(reader))
    _write_sample(os.path.join(out, "sample.csv"), reversed_rows)
    with open(os.path.join(src_out, "controls.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    controls = doc["controls"]
    doc["controls"] = controls[1::2] + controls[0::2]  # 打乱控制顺序
    with open(os.path.join(out, "controls.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)


def gen_control_revision(src_out: str, out: str) -> None:
    """控制更正：样本不变，仅修改两个目标并新增一个交叉单元 => 新产品。"""
    with open(os.path.join(src_out, "sample.csv"), encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    os.makedirs(out, exist_ok=True)
    _write_sample(os.path.join(out, "sample.csv"), rows)
    with open(os.path.join(src_out, "controls.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    # 更正：所有现有目标统一乘 0.95（保持边际相容），并新增一个交叉控制单元
    for c in doc["controls"]:
        c["target"] = round(float(c["target"]) * 0.95, 6)
    doc["controls"].append(
        {
            "code": "CELL_R2_A4_NEW",
            "target": round(
                sum(
                    float(r["base_weight"])
                    for r in rows
                    if r["region"] == "R2" and r["age_group"] == "A4"
                ),
                4,
            ),
            "selector": {"region": "R2", "age_group": "A4"},
        }
    )
    with open(os.path.join(out, "controls.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
def gen_handcalc(out: str) -> dict:
    """2-4 人严格凸小问题，附分数手算最优权重与残差（测试据此精确断言）。"""
    os.makedirs(out, exist_ok=True)

    # H1：2 人单约束，内部解，d=(1,2)，b=4.5，边界 [0.5,2] 不活动
    h1_rows = [
        {"person_id": "H1_P1", "base_weight": 1.0, "region": "R1", "age_group": "A1"},
        {"person_id": "H1_P2", "base_weight": 2.0, "region": "R1", "age_group": "A2"},
    ]
    h1_controls = [{"code": "TOTAL", "target": 4.5, "selector": {}}]
    _write_sample(os.path.join(out, "h1_sample.csv"), h1_rows)
    _write_controls(os.path.join(out, "h1_controls.json"), h1_controls, 0.5, 2.0)

    # H2：3 人重叠约束，TOTAL=6 且 REG_R1=4（d=(1,2,3)，边界 [0.5,2]）
    h2_rows = [
        {"person_id": "H2_P1", "base_weight": 1.0, "region": "R1", "age_group": "A1"},
        {"person_id": "H2_P2", "base_weight": 2.0, "region": "R1", "age_group": "A2"},
        {"person_id": "H2_P3", "base_weight": 3.0, "region": "R2", "age_group": "A1"},
    ]
    h2_controls = [
        {"code": "TOTAL", "target": 6.0, "selector": {}},
        {"code": "REG_R1", "target": 4.0, "selector": {"region": "R1"}},
    ]
    _write_sample(os.path.join(out, "h2_sample.csv"), h2_rows)
    _write_controls(os.path.join(out, "h2_controls.json"), h2_controls, 0.5, 2.0)

    # 手算分数结果（Fraction 精确推导，测试转 float 后按 1e-10 断言）
    # H1: 单约束 w1+w2=4.5 => 3w1-4.5=0 => w=(3/2, 3)，F=1/4+1/2=3/4
    # H2: 约束给出 w3=2、w1+w2=4；对 w1 求导 3w1-4=0 =>
    #     w1=4/3, w2=8/3, w3=2，F=1/9+2/9+1/3=2/3（均在 [0.5,2] 边界内）
    expected = {
        "h1": {
            "weights": [float(Fraction(3, 2)), float(Fraction(3, 1))],
            "objective": float(Fraction(3, 4)),
            "residuals": [0.0],
        },
        "h2": {
            "weights": [float(Fraction(4, 3)), float(Fraction(8, 3)), float(Fraction(2, 1))],
            "objective": float(Fraction(2, 3)),
            "residuals": [0.0, 0.0],
        },
    }
    with open(os.path.join(out, "handcalc_expected.json"), "w", encoding="utf-8") as fh:
        json.dump(expected, fh, ensure_ascii=False, indent=2)
    return expected


# ---------------------------------------------------------------------------
def gen_large(out: str, n: int = 5000, n_controls: int = 100, n_indicators: int = 20) -> None:
    """规模案例：5000 人、100 控制单元、20 指标，触发稀疏约束表示。

    可行性构造（与缩放舍入无关的强保证）：先生成一个已知在边界内、
    非均匀的参考权重向量 ``w*``，再让*每个*控制单元的目标都等于
    ``w*`` 在该单元成员上的精确分组和。无论单元如何重叠，``w*`` 本身
    就是可行解；卡方目标严格凸，求解器应收敛到同一唯一解。
    """
    rng = np.random.default_rng(777)
    rows = []
    for i in range(n):
        rows.append(
            {
                "person_id": f"S{i:05d}",
                "base_weight": round(float(rng.uniform(0.5, 5.0)), 4),
                "region": REGIONS[int(rng.integers(0, len(REGIONS)))],
                "age_group": AGES[int(rng.integers(0, len(AGES)))],
            }
        )
    _write_sample(os.path.join(out, "sample.csv"), rows)

    # 参考可行权重：每人权重比在 [0.72, 1.48]，严格落在全局边界 [0.6, 1.7] 内，
    # 且非均匀 => 最优解不是简单等比缩放，目标值为正、解不平凡。
    ratio_star = rng.uniform(0.72, 1.48, size=n)
    w_star = [r["base_weight"] * float(ratio_star[i]) for i, r in enumerate(rows)]

    def target_over(pred_or_ids) -> float:
        if callable(pred_or_ids):
            return sum(w_star[i] for i, r in enumerate(rows) if pred_or_ids(r))
        idset = set(pred_or_ids)
        return sum(w_star[i] for i, r in enumerate(rows) if r["person_id"] in idset)

    controls = [{"code": "TOTAL", "target": round(target_over(lambda r: True), 6), "selector": {}}]
    for r in REGIONS:
        controls.append(
            {"code": f"REG_{r}", "target": round(target_over(lambda x, r=r: x["region"] == r), 6),
             "selector": {"region": r}}
        )
    for a in AGES:
        controls.append(
            {"code": f"AGE_{a}", "target": round(target_over(lambda x, a=a: x["age_group"] == a), 6),
             "selector": {"age_group": a}}
        )
    # 20 个交叉单元（不铺满全网格，避免把所有人钉死在单元目标上）
    cells = [(r, a) for r in REGIONS for a in AGES][:20]
    for r, a in cells:
        controls.append(
            {
                "code": f"CELL_{r}_{a}",
                "target": round(
                    target_over(lambda x, r=r, a=a: x["region"] == r and x["age_group"] == a), 6
                ),
                "selector": {"region": r, "age_group": a},
            }
        )
    # 补足到 n_controls：显式 members 的重叠单元（随机 20-80 人），目标同样取自 w*
    kk = 0
    while len(controls) < n_controls:
        size = int(rng.integers(20, 80))
        idxs = sorted(rng.choice(n, size=size, replace=False))
        members = [rows[i]["person_id"] for i in idxs]
        controls.append(
            {
                "code": f"OVL_{kk:03d}",
                "target": round(target_over(members), 6),
                "selector": None,
                "members": members,
            }
        )
        kk += 1
    _write_controls(os.path.join(out, "controls.json"), controls, 0.6, 1.7)

    # 20 个指标，约 5% 缺失（空白），并刻意保留若干真实零值
    resp_rows: list[tuple[str, str, str]] = []
    for ind_k in range(n_indicators):
        ind = f"Y{ind_k:02d}"
        for i, r in enumerate(rows):
            if rng.random() < 0.05:
                resp_rows.append((r["person_id"], ind, ""))  # 缺失
            else:
                val = round(float(rng.normal(50 + ind_k, 10)), 6)
                if i % 211 == ind_k:
                    val = 0.0  # 真实零值
                resp_rows.append((r["person_id"], ind, str(val)))
    _write_responses(os.path.join(out, "responses.csv"), resp_rows)


def generate_all(base_dir: str) -> dict:
    """生成全部演示/测试数据，返回场景 -> 目录的映射。"""
    paths = {}
    demos = {
        "base_satisfied": gen_base_satisfied,
        "joint_marginal": gen_joint_marginal,
        "bounds_active": gen_bounds_active,
        "empty_unit": gen_empty_unit,
        "contradiction": gen_contradiction,
        "missing_responses": gen_missing_responses,
    }
    for name, fn in demos.items():
        d = os.path.join(base_dir, name)
        os.makedirs(d, exist_ok=True)
        fn(d)
        paths[name] = d
    gen_rearranged(paths["joint_marginal"], os.path.join(base_dir, "rearranged"))
    paths["rearranged"] = os.path.join(base_dir, "rearranged")
    gen_control_revision(paths["joint_marginal"], os.path.join(base_dir, "control_revision"))
    paths["control_revision"] = os.path.join(base_dir, "control_revision")
    large_dir = os.path.join(base_dir, "large_5000")
    os.makedirs(large_dir, exist_ok=True)
    gen_large(large_dir)
    paths["large_5000"] = large_dir
    paths["handcalc"] = os.path.join(base_dir, "handcalc")
    gen_handcalc(paths["handcalc"])
    return paths
