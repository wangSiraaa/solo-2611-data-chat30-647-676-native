"""三维向量工具与有限合成历元传播模型。

模型（题面给定，不套真实天体物理修正）：
    p(t) = r + (t - t0) * v
    若 ||p(t)|| <= ZERO_NORM_EPS 则该历元不可用；否则方向为 normalize(p(t))。
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

# 合成模型下“向量长度为零”的判定阈值（仅用于吸收浮点残差，真实零向量精确为 0）。
ZERO_NORM_EPS = 1e-15


def as_vec3(value: Sequence[float], name: str = "vector") -> np.ndarray:
    """转成 float64 三维向量，并做有限性校验。"""
    arr = np.asarray(value, dtype=np.float64)
    if arr.shape != (3,):
        raise ValueError(f"{name} 必须是长度为 3 的序列，实际形状 {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} 含非有限值（NaN/Inf）: {value!r}")
    return arr


def norm(v: np.ndarray) -> float:
    return float(np.linalg.norm(v))


def is_zero_vector(v: np.ndarray, eps: float = ZERO_NORM_EPS) -> bool:
    return norm(v) <= eps


def normalize(v: np.ndarray) -> Optional[np.ndarray]:
    """单位化；零向量返回 None（表示该历元不可用），不抛异常、不造假方向。"""
    n = norm(v)
    if n <= ZERO_NORM_EPS:
        return None
    return v / n


def unit_length_error(u: np.ndarray) -> float:
    """单位长度误差 | ||u|| - 1 |。"""
    return abs(norm(u) - 1.0)


def validate_unit_direction(u: Sequence[float], tol: float, name: str = "direction") -> np.ndarray:
    """亮点/快照方向校验：有限且单位长度误差不超过声明容差 tol。"""
    arr = as_vec3(u, name)
    err = unit_length_error(arr)
    if err > tol:
        raise ValueError(f"{name} 单位长度误差 {err:.3e} 超过声明容差 {tol:.3e}")
    return arr


def propagate(r: np.ndarray, v: np.ndarray, t: int, t0: int) -> Optional[np.ndarray]:
    """历元 t 的方向；零向量历元返回 None。"""
    p = r + float(t - t0) * v
    return normalize(p)


def angle_between(u: np.ndarray, w: np.ndarray) -> float:
    """两单位方向夹角（弧度，[0, pi]），裁剪浮点越界。"""
    cos_a = float(np.dot(u, w))
    if cos_a > 1.0:
        cos_a = 1.0
    elif cos_a < -1.0:
        cos_a = -1.0
    return math.acos(cos_a)


def angle_matrix(star_dirs: np.ndarray, det_dirs: np.ndarray) -> np.ndarray:
    """向量化夹角矩阵 rows=星 cols=亮点；输入均为单位方向行向量。"""
    dots = star_dirs @ det_dirs.T
    np.clip(dots, -1.0, 1.0, out=dots)
    return np.arccos(dots)
