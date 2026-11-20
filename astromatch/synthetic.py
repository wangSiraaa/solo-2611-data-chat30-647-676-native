"""本地合成数据（不连接任何天文数据库）。

产出：
  * 200 颗星的星表种子（star_id/t0/r/v）与 20 张曝光（各 100 个亮点）；
  * 一组手工构造的小场景，覆盖：相邻两星竞争（贪心最近邻反例）、单星多亮点、
    速度使最近关系翻转、零向量历元不可用、无候选、撤回、亮度无关性、外部快照。
全部用固定种子的 numpy 生成器，结果可复跑。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

import numpy as np

from .vectors import normalize

SCALE_STARS = 200
SCALE_EXPOSURES = 20
DETECTIONS_PER_EXPOSURE = 100


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def random_unit(rng: np.random.Generator) -> np.ndarray:
    v = rng.normal(size=3)
    return v / np.linalg.norm(v)


# ------------------------------------------------------------- scale dataset
def build_scale_catalog(seed: int = 20261118) -> List[Dict[str, Any]]:
    """200 颗星：t0=0，单位方向 r，小速度 v（各方向随机、量级有差异）。"""
    rng = _rng(seed)
    rows: List[Dict[str, Any]] = []
    for i in range(SCALE_STARS):
        r = random_unit(rng)
        speed = float(rng.uniform(1e-4, 2e-3))      # 每天方向漂移的合成速度
        v = random_unit(rng) * speed
        rows.append({
            "star_id": f"S{i:04d}", "t0": 0,
            "r": [float(x) for x in r], "v": [float(x) for x in v],
        })
    return rows


def build_scale_exposures(
    catalog_rows: Sequence[Dict[str, Any]], seed: int = 4242,
) -> List[Dict[str, Any]]:
    """20 张曝光、每张 100 亮点：取随机 100 星在曝光历元的方向加小抖动，
    混入 5% 杂散亮点（不对应任何星），亮度为非负整数但匹配规则不使用它。"""
    rng = _rng(seed)
    rng_jit = _rng(seed + 1)
    exposures: List[Dict[str, Any]] = []
    n_pick = DETECTIONS_PER_EXPOSURE - 5
    for k in range(SCALE_EXPOSURES):
        epoch = 30 * (k + 1)                        # 整数日：30,60,...,600
        chosen = rng.choice(len(catalog_rows), size=n_pick, replace=False)
        detections: List[Dict[str, Any]] = []
        for j, idx in enumerate(chosen):
            row = catalog_rows[int(idx)]
            r = np.array(row["r"], dtype=np.float64)
            v = np.array(row["v"], dtype=np.float64)
            u = normalize(r + float(epoch - row["t0"]) * v)
            assert u is not None
            jitter = random_unit(rng_jit) * float(rng_jit.uniform(1e-5, 8e-4))
            d = normalize(u + jitter)
            detections.append({
                "detection_id": f"D{k:02d}-{j:03d}",
                "direction": [float(x) for x in d],
                "brightness": int(rng_jit.integers(0, 10_000)),
            })
        # 杂散亮点：纯随机方向
        for j in range(5):
            d = random_unit(rng_jit)
            detections.append({
                "detection_id": f"D{k:02d}-S{j:02d}",
                "direction": [float(x) for x in d],
                "brightness": int(rng_jit.integers(0, 10_000)),
            })
        exposures.append({
            "exposure_id": f"E{k:02d}", "epoch": epoch,
            "unit_tol": 1e-8, "detections": detections,
        })
    return exposures


# ------------------------------------------------------------- small scenarios
def scenario_adjacent_competition() -> Dict[str, Any]:
    """相邻两星竞争 + 最近邻贪心反例（题面：两个近邻亮点不能都贪心判给同一颗星）。

    几何（y 小角近似夹角，阈值 0.030）：
        S1=(1,0)       S2≈(1,0.020)
        D1≈(1,0.004)   D2≈(1,0.008)
    实测角度：
        (S1,D1)=0.0040  (S1,D2)=0.0080
        (S2,D1)=0.0160  (S2,D2)=0.0120
    逐点“每个亮点找最近星”：D1、D2 的最近星都是 S1
        => 不删除时两个亮点重复匹配到 S1（题面明确禁止的“同一颗星被选两次”）；
       配后删除：D1->S1 后 S1 被占，D2 改配 S2 => 2 条（顺序敏感、仍属被禁启发式）。
    全局求解器给出唯一合法最优 2 条：(S1,D1)+(S2,D2)，每颗星/亮点最多一次。
    """
    s1 = np.array([1.0, 0.0, 0.0])
    s2 = normalize(np.array([1.0, 0.020, 0.0]))
    d1 = normalize(np.array([1.0, 0.004, 0.0]))     # 最近星 S1
    d2 = normalize(np.array([1.0, 0.008, 0.0]))     # 最近星也是 S1，S2 仍在阈值内
    stars = [
        {"star_id": "S1", "t0": 0, "r": [1.0, 0.0, 0.0], "v": [0.0, 0.0, 0.0]},
        {"star_id": "S2", "t0": 0, "r": [float(x) for x in s2], "v": [0.0, 0.0, 0.0]},
    ]
    detections = [
        {"detection_id": "D1", "direction": [float(x) for x in d1], "brightness": 100},
        {"detection_id": "D2", "direction": [float(x) for x in d2], "brightness": 999},
    ]
    return {
        "name": "adjacent_competition",
        "epoch": 0, "threshold": 0.030, "eps": 1e-12,
        "stars": stars,
        "exposure": {
            "exposure_id": "E-ADJ", "epoch": 0, "unit_tol": 1e-9,
            "detections": detections,
        },
        "expect_greedy_nearest_fails": True,
    }


def scenario_one_star_many_detections() -> Dict[str, Any]:
    """单星面对两个亮点：只能选一个，另一个未匹配；选夹角更小者。"""
    d1 = normalize(np.array([1.0, 0.010, 0.0]))
    d2 = normalize(np.array([1.0, 0.020, 0.0]))
    return {
        "name": "one_star_many_detections",
        "epoch": 0, "threshold": 0.05, "eps": 1e-12,
        "stars": [{"star_id": "S1", "t0": 0, "r": [1.0, 0.0, 0.0], "v": [0.0, 0.0, 0.0]}],
        "exposure": {
            "exposure_id": "E-OSMD", "epoch": 0, "unit_tol": 1e-9,
            "detections": [
                {"detection_id": "D1", "direction": [float(x) for x in d1], "brightness": 1},
                {"detection_id": "D2", "direction": [float(x) for x in d2], "brightness": 1},
            ],
        },
    }


def scenario_velocity_flip() -> Dict[str, Any]:
    """手算速度传播：S2 在 y 方向运动，t=1 时越过 S1，最近关系翻转。

    S1 静止于 (1,0,0)；S2: r=(1,0.030,0), v=(0,-0.060,0), t0=0。
      t=0: S2 方向 ~ (1,0.030,0)，亮点 D=(1,0.005,0) 离 S1 更近；
      t=1: p(S2)=(1,-0.030,0)，方向 ~ (1,-0.030,0)，离 D 更远……
    为得到“翻转”，D 选在 t=1 的 S2 一侧：D_t1=(1,-0.020,0)。
      t=0 时 D_t1 离 S2(+0.030) 比离 S1 更远；t=1 时 S2=-0.030 反而更近 => 翻转。
    """
    s2_r = np.array([1.0, 0.030, 0.0])
    s2_v = np.array([0.0, -0.060, 0.0])
    d_t1 = normalize(np.array([1.0, -0.020, 0.0]))
    stars = [
        {"star_id": "S1", "t0": 0, "r": [1.0, 0.0, 0.0], "v": [0.0, 0.0, 0.0]},
        {"star_id": "S2", "t0": 0, "r": [float(x) for x in s2_r],
         "v": [float(x) for x in s2_v]},
    ]
    return {
        "name": "velocity_flip",
        "threshold": 0.05, "eps": 1e-12,
        "stars": stars,
        "epoch_before": 0,
        "epoch_after": 1,
        "exposure_before": {
            "exposure_id": "E-FLIP", "epoch": 0, "unit_tol": 1e-9,
            "detections": [
                {"detection_id": "D1", "direction": [float(x) for x in d_t1],
                 "brightness": 500}],
        },
        "exposure_after": {
            "exposure_id": "E-FLIP", "epoch": 1, "unit_tol": 1e-9,
            "detections": [
                {"detection_id": "D1", "direction": [float(x) for x in d_t1],
                 "brightness": 500}],
        },
    }


def scenario_zero_vector() -> Dict[str, Any]:
    """零向量历元不可用：S2 在 t=1 时 r+(t-t0)v 恰好为零，快照中被剔除。"""
    return {
        "name": "zero_vector",
        "epoch": 1, "threshold": 0.05, "eps": 1e-12,
        "stars": [
            {"star_id": "S1", "t0": 0, "r": [1.0, 0.0, 0.0], "v": [0.0, 0.0, 0.0]},
            {"star_id": "S2", "t0": 0,
             "r": [0.0, 0.030, 0.0], "v": [0.0, -0.030, 0.0]},
        ],
        "exposure": {
            "exposure_id": "E-ZERO", "epoch": 1, "unit_tol": 1e-9,
            "detections": [
                {"detection_id": "D1",
                 "direction": [float(x) for x in normalize(np.array([1.0, 0.0, 0.0]))],
                 "brightness": 10}],
        },
        "zero_star": "S2",
    }


def scenario_no_candidate() -> Dict[str, Any]:
    """无候选：亮点方向与所有星夹角超阈值，K=0。"""
    return {
        "name": "no_candidate",
        "epoch": 0, "threshold": 0.01, "eps": 1e-12,
        "stars": [
            {"star_id": "S1", "t0": 0, "r": [1.0, 0.0, 0.0], "v": [0.0, 0.0, 0.0]},
        ],
        "exposure": {
            "exposure_id": "E-NONE", "epoch": 0, "unit_tol": 1e-9,
            "detections": [
                {"detection_id": "D1",
                 "direction": [0.0, 1.0, 0.0], "brightness": 1}],
        },
    }


def scenario_withdrawal() -> Dict[str, Any]:
    """撤回：S2 参与 release R1；撤回后发布 R2 不再含 S2，R1 仍可查。"""
    return {
        "name": "withdrawal",
        "epochs": [0, 1], "threshold": 0.05, "eps": 1e-12,
        "stars": [
            {"star_id": "S1", "t0": 0, "r": [1.0, 0.0, 0.0], "v": [0.0, 0.0, 0.0]},
            {"star_id": "S2", "t0": 0,
             "r": [float(x) for x in normalize(np.array([1.0, 0.02, 0.0]))],
             "v": [0.0, 0.0, 0.0]},
        ],
        "withdraw": "S2",
        "exposure": {
            "exposure_id": "E-WD", "epoch": 0, "unit_tol": 1e-9,
            "detections": [
                {"detection_id": "D1",
                 "direction": [float(x) for x in normalize(np.array([1.0, 0.018, 0.0]))],
                 "brightness": 7}],
        },
    }


def scenario_brightness_invariance() -> Dict[str, Any]:
    """亮度无关性：同一几何、不同亮度序列必须得到完全相同的分配。"""
    base = scenario_adjacent_competition()
    exp2 = {
        "exposure_id": "E-ADJ2", "epoch": 0, "unit_tol": 1e-9,
        "detections": [
            {"detection_id": "D1",
             "direction": base["exposure"]["detections"][0]["direction"],
             "brightness": 999_999},
            {"detection_id": "D2",
             "direction": base["exposure"]["detections"][1]["direction"],
             "brightness": 0},
        ],
    }
    return {"name": "brightness_invariance", "base": base, "exposure_alt": exp2}


def external_snapshot_payload() -> Dict[str, Any]:
    """外部同格式快照（来源标记 external，无 catalog_release）。"""
    return {
        "schema": "astromatch.snapshot/v1",
        "epoch": 0,
        "source": "external",
        "catalog_release": None,
        "unit_tol": 1e-9,
        "entries": [
            {"star_id": "X1", "available": True,
             "direction": [float(x) for x in normalize(np.array([1.0, 0.0, 0.0]))]},
            {"star_id": "X2", "available": True,
             "direction": [float(x) for x in normalize(np.array([1.0, 0.01, 0.0]))]},
        ],
    }


ALL_SCENARIOS = [
    scenario_adjacent_competition,
    scenario_one_star_many_detections,
    scenario_velocity_flip,
    scenario_zero_vector,
    scenario_no_candidate,
    scenario_withdrawal,
    scenario_brightness_invariance,
]
