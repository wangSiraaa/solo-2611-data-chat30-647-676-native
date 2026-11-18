"""独立权重校准模块。

职责边界
--------
本模块**只**登记样本设计字段、基础权重、控制数与边界，不需要任何指标回答即可
计算并导出权重产品。统计发布在 :mod:`surveycal.statistics`，二者不互相调用优化。

数学问题
--------
设 n 名样本，基础权重 d_j，校准权重 w_j，m 个（允许重叠的）控制单元，
成员矩阵 A∈{0,1}^{m×n}，控制目标 b：

    minimize  F(w) = Σ_j (w_j - d_j)^2 / d_j          （严格凸）
    subject to A w = b,   l_j ≤ w_j ≤ u_j,
    其中 l_j = d_j·lo_j, u_j = d_j·hi_j，lo/hi 为正的权重比闭区间。

求解方案（不排斥其它算法，这里给出可替换的一种）
------------------------------------------------
#. 结构化预检：空单元、逐单元边界容量、同成员异目标等可直接判定的不可行；
#. 对偶光滑凸优化：拉格朗日对偶目标凹且梯度易得，用 L-BFGS-B 最大化，
   盒约束在对偶层以精确投影处理，天然支持重叠约束；
#. 独立复核：重新计算每个控制残差、每条边界与目标值，并做 KKT 平稳性检验，
   **不**以求解器返回成功作为发布依据；
#. 复核存疑时用有界最小二乘（``lsq_linear``）与无界最小二乘（``lsqr``）
   区分“边界导致不可行”与“控制矛盾”，并把未能判定单列为 UNDECIDED。

稀疏/稠密取舍
-------------
n·m 较小时用稠密 ndarray（常数小、最快）；超过阈值自动切换 CSR 稀疏矩阵
（成员矩阵每行仅含单元内的 1，天然稀疏）。两种表示的数学结果一致，
产品中记录实际表示，保证产品兼容性与可复核性。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import sparse
from scipy.optimize import lsq_linear, minimize

from .errors import InfeasibleError, InputError, UndecidedError
from .models import (
    CalibStatus,
    CalibrationResult,
    ControlUnit,
    ProductKind,
    Sample,
    WeightProduct,
    content_id,
)

SPARSE_THRESHOLD = 250_000  # n*m 超过该值使用 CSR 稀疏表示


# ---------------------------------------------------------------------------
# 容差：全部显式记录并随产品发布，目标/边界违反容差时不发布
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Tolerance:
    # 说明：绝对容差覆盖小目标/零目标单元，相对容差覆盖大目标；
    # 目标（控制总量）与边界任一项违反对应容差，产品一律不发布。
    # 1e-6 绝对残差是人口校准常用的发布容差；演示中的真实矛盾残差
    # （≥1e-5 或同成员目标差 4.5）远在容差之外，不会被误判为可行。
    residual_abs: float = 1e-6
    residual_rel: float = 1e-8
    bound_abs: float = 1e-9
    bound_rel: float = 1e-9
    stationarity_abs: float = 1e-7
    stationarity_rel: float = 1e-8
    gap_rel: float = 1e-10
    precheck_eps: float = 1e-12

    def as_dict(self) -> dict:
        return {
            "residual_abs": self.residual_abs,
            "residual_rel": self.residual_rel,
            "bound_abs": self.bound_abs,
            "bound_rel": self.bound_rel,
            "stationarity_abs": self.stationarity_abs,
            "stationarity_rel": self.stationarity_rel,
            "gap_rel": self.gap_rel,
            "precheck_eps": self.precheck_eps,
        }

    def residual_tol(self, target: float) -> float:
        return self.residual_abs + self.residual_rel * max(1.0, abs(float(target)))

    def bound_tol(self, w_bound: float) -> float:
        return self.bound_abs + self.bound_rel * max(1.0, abs(float(w_bound)))


# ---------------------------------------------------------------------------
# 约束矩阵与结构化预检
# ---------------------------------------------------------------------------
def build_constraint_matrix(sample: Sample, membership: dict[str, frozenset[int]]):
    """按控制单元登记顺序构造 0/1 成员矩阵 A 与目标向量 b。"""
    n = len(sample.persons)
    codes = list(membership.keys())
    m = len(codes)
    dense = n * m < SPARSE_THRESHOLD
    if dense:
        A = np.zeros((m, n), dtype=float)
        for i, code in enumerate(codes):
            for j in membership[code]:
                A[i, j] = 1.0
        representation = "dense"
    else:
        rows, cols = [], []
        for i, code in enumerate(codes):
            rows.extend([i] * len(membership[code]))
            cols.extend(membership[code])
        A = sparse.csr_matrix(
            (np.ones(len(rows), dtype=float), (rows, cols)), shape=(m, n)
        )
        representation = "sparse"
    return A, representation, codes


def structural_prechecks(
    sample: Sample,
    units: tuple[ControlUnit, ...],
    membership: dict[str, frozenset[int]],
    tol: Tolerance,
) -> None:
    """不需要求解器即可判定的不可行情形。判定失败即抛 :class:`InfeasibleError`。"""
    d = sample.base_weights
    lo_w, hi_w = sample.person_bounds()

    # 1) 必要单元无样本：目标为正却没有任何成员
    for u in units:
        idx = membership[u.code]
        if not idx:
            if u.target > tol.residual_abs:
                raise InfeasibleError(
                    "empty_required_unit",
                    f"控制 {u.code} 目标 {u.target} 为正，但成员集合为空（必要单元无样本）",
                    {"control": u.code, "target": u.target, "member_count": 0},
                )
            # 目标为 0 的空单元本身可行（残差天然为 0），保留约束不丢弃

    # 2) 逐单元边界容量：成员权重上下界之和必须覆盖目标
    for u in units:
        idx = np.array(sorted(membership[u.code]), dtype=int)
        if idx.size == 0:
            continue
        cap_lo = float(lo_w[idx].sum())
        cap_hi = float(hi_w[idx].sum())
        scale = max(1.0, cap_lo, cap_hi, abs(u.target))
        eps = tol.precheck_eps * scale
        if cap_lo > u.target + eps:
            raise InfeasibleError(
                "bound_infeasible",
                f"控制 {u.code} 成员下界权重之和 {cap_lo} 已超过目标 {u.target}，边界使其不可行",
                {
                    "control": u.code,
                    "target": u.target,
                    "sum_lower_weights": cap_lo,
                    "sum_upper_weights": cap_hi,
                    "members": [sample.persons[j].person_id for j in idx],
                },
            )
        if cap_hi < u.target - eps:
            raise InfeasibleError(
                "bound_infeasible",
                f"控制 {u.code} 成员上界权重之和 {cap_hi} 仍低于目标 {u.target}，边界使其不可行",
                {
                    "control": u.code,
                    "target": u.target,
                    "sum_lower_weights": cap_lo,
                    "sum_upper_weights": cap_hi,
                    "members": [sample.persons[j].person_id for j in idx],
                },
            )

    # 3) 同成员集合、不同目标 => 控制矛盾（重叠单元的显式冲突）
    seen: dict[frozenset[int], ControlUnit] = {}
    for u in units:
        key = membership[u.code]
        if key in seen:
            prev = seen[key]
            scale = max(1.0, abs(u.target), abs(prev.target))
            if abs(u.target - prev.target) > tol.precheck_eps * scale:
                raise InfeasibleError(
                    "contradiction",
                    f"控制 {prev.code} 与 {u.code} 成员完全相同但目标不同："
                    f"{prev.target} != {u.target}",
                    {
                        "controls": [prev.code, u.code],
                        "targets": [prev.target, u.target],
                        "shared_members": [sample.persons[j].person_id for j in sorted(key)],
                    },
                )
        else:
            seen[key] = u


# ---------------------------------------------------------------------------
# 求解器一：原始主动集 KKT 法（正式权重的主求解路径）
# ---------------------------------------------------------------------------
def _eqp_free_solution(A, b, d, free, fixed_w, lo_w, hi_w):
    """在自由变量集合 ``free`` 上解等式约束严格凸 QP：

        min ½ Σ_{j∈F} (w_j-d_j)²/d_j   s.t. A_F w_F = b - A_P w_P

    一阶条件 (w_F-d_F)/D_F + A_F'λ = 0 与 A_F w_F = rhs，消元得闭式解：

        w_F = d_F + D_F A_F' (A_F D_F A_F')⁺ (rhs - A_F d_F)

    重叠/冗余控制会使 KKT 矩阵奇异，用最小二乘求伪逆解；
    原始问题严格凸，``w`` 仍然唯一，只是拉格朗日乘子不唯一。
    返回 (全量 w, 乘子最小二乘相容残差, 乘子)。
    """
    w = fixed_w.copy()
    f_idx = np.array(sorted(free), dtype=int)
    rhs = b - A @ fixed_w
    if f_idx.size == 0:
        return w, float(np.max(np.abs(rhs))), None
    Df = d[f_idx]
    Af = A[:, f_idx]
    g = rhs - (Af @ Df)  # = rhs - A_F d_F
    if sparse.issparse(Af):
        # 稀疏成员矩阵：稀疏乘法后只在 m×m 小矩阵上求解
        H = (Af.multiply(Df)) @ Af.T
        H = H.toarray() if sparse.issparse(H) else H
        y, *_ = np.linalg.lstsq(H, g, rcond=None)
        w[f_idx] = Df + Df * (Af.T @ y)
    else:
        H = (Af * Df) @ Af.T
        y, *_ = np.linalg.lstsq(H, g, rcond=None)
        w[f_idx] = Df + Df * (Af.T @ y)
    # 乘子方程残差：衡量冗余约束下 KKT 系统的相容程度（仅诊断用）
    mult_resid = float(np.max(np.abs(H @ y - g))) if H.size else 0.0
    return w, mult_resid, y


def active_set_solve(A, b, d, lo_w, hi_w, *, max_sweeps=200):
    """盒约束 + 线性等式的原始主动集求解。

    流程：全部变量自由 → 解等式 QP → 违反 [l,u] 的变量钉到对应边界 →
    重解；自由解全部入界后，再按 KKT 乘子符号检查是否有变量应被释放，
    符号错误则释放继续。严格凸 QP 下自由集单调调整，有限步终止。
    """
    n = d.shape[0]
    free = set(range(n))
    fixed_w = np.zeros(n)        # 固定位写边界值；自由位每轮由 EQP 覆盖
    pinned: dict[int, int] = {}  # 行下标 -> -1 下界 / +1 上界
    history = []
    btol = 1e-11 * np.maximum(1.0, np.maximum(lo_w, hi_w))
    w = d.copy()  # 预算为 0（一次迭代都不做）时的安全回退值

    for sweep in range(max_sweeps):
        w, mult_resid, y = _eqp_free_solution(A, b, d, free, fixed_w, lo_w, hi_w)
        if not free:
            # 全部钉死：直接以等式残差判定，交给上层诊断分支
            return w, {"sweeps": sweep, "reason": "all_pinned", "mult_resid": mult_resid}

        low_viol = [j for j in free if w[j] < lo_w[j] - btol[j]]
        up_viol = [j for j in free if w[j] > hi_w[j] + btol[j]]
        if low_viol or up_viol:
            # 一次钉住全部违规变量（可能过度固定，由后面的释放检查纠正）
            for j in low_viol:
                free.discard(j)
                pinned[j] = -1
                fixed_w[j] = lo_w[j]
            for j in up_viol:
                free.discard(j)
                pinned[j] = +1
                fixed_w[j] = hi_w[j]
            history.append({"sweep": sweep, "pinned_lower": len(low_viol), "pinned_upper": len(up_viol)})
            continue

        # 自由变量全部入界：检查固定变量的 KKT 符号条件
        g_all = (w - d) / d
        f_idx = np.array(sorted(free), dtype=int)
        Af = A[:, f_idx]
        Af_t = Af.T.toarray() if sparse.issparse(Af) else Af.T
        lam, *_ = np.linalg.lstsq(Af_t, -g_all[f_idx], rcond=None)
        stat = g_all + (A.T @ lam)
        to_free = []
        for j, side in pinned.items():
            if side == -1 and stat[j] < -Tolerance().stationarity_abs:
                to_free.append(j)      # 下界处却有下降方向 => 应释放
            elif side == +1 and stat[j] > Tolerance().stationarity_abs:
                to_free.append(j)      # 上界处却有上升方向 => 应释放
        if to_free:
            for j in to_free:
                free.add(j)
                pinned.pop(j)
            history.append({"sweep": sweep, "freed": len(to_free)})
            continue

        # KKT 全部满足
        info = {
            "sweeps": sweep,
            "n_pinned_lower": sum(1 for s in pinned.values() if s == -1),
            "n_pinned_upper": sum(1 for s in pinned.values() if s == +1),
            "mult_resid": mult_resid,
            "history": history,
        }
        return w, info

    # 主动集调整未在预算内稳定 => 交上层按未决处理
    return w, {"sweeps": max_sweeps, "reason": "active_set_did_not_settle", "history": history}


def _dual_solve(A, b, d, lo_w, hi_w, *, ftol=1e-12, gtol=1e-11, maxiter=2000):
    """对偶 L-BFGS-B 交叉校验求解器（不作为主发布路径，用于两算法互相印证）。

        r_j = w_j/d_j ∈ [a_j, c_j]，令 q_j = r_j - 1，
        q(λ) = clip(-A'λ, a-1, c-1)，w(λ) = d·(1+q)，
        φ(λ) = -min_w L，∇φ(λ) = b - A w(λ)。
    """
    a = lo_w / d
    c = hi_w / d
    At = A.T
    s = max(1.0, float(np.max(np.abs(b))))

    def phi_and_grad(z):
        lam = z / s
        Atlam = At @ lam
        q = np.clip(-Atlam, a - 1.0, c - 1.0)
        w = d * (1.0 + q)
        phi = -(0.5 * float(np.dot(d, q * q)) + float(np.dot(Atlam, w)) - float(np.dot(lam, b)))
        grad = (b - (A @ w)) / s
        return phi, grad

    res = minimize(
        phi_and_grad,
        np.zeros_like(b),
        jac=True,
        method="L-BFGS-B",
        options={"ftol": ftol, "gtol": gtol, "maxiter": maxiter, "maxfun": maxiter * 20},
    )
    lam = res.x / s
    Atlam = At @ lam
    q = np.clip(-Atlam, a - 1.0, c - 1.0)
    w = d * (1.0 + q)
    return w, lam, res


# ---------------------------------------------------------------------------
# 求解器二/三：有界与无界最小二乘，仅用于可行性诊断
# ---------------------------------------------------------------------------
def _bounded_min_residual(A, b, lo_w, hi_w, *, max_iter=2000):
    """min ½‖Aw-b‖² s.t. l≤w≤u。结果只作诊断，不冒充正式权重。"""
    res = lsq_linear(A, b, bounds=(lo_w, hi_w), max_iter=max_iter, tol=1e-12)
    return res.x, res


def _unbounded_min_residual(A, b, *, iter_lim=2000):
    """去掉盒约束后的最小残差解：残差仍非零 => 控制本身矛盾。"""
    if sparse.issparse(A):
        out = sparse.linalg.lsqr(A, b, atol=1e-12, btol=1e-12, iter_lim=iter_lim)
        w, istop = out[0], out[1]
        converged = istop in (1, 2)
    else:
        w, *_ = np.linalg.lstsq(A, b, rcond=None)
        converged = True
    return w, converged


# ---------------------------------------------------------------------------
# 独立复核：残差、边界、目标值、KKT 平稳性全部重算
# ---------------------------------------------------------------------------
def _primal_objective(w, d) -> float:
    return float(np.sum((w - d) ** 2 / d))


def _kkt_stationarity(w, d, lo_w, hi_w, A, tol: Tolerance):
    """KKT 平稳性独立检验（线性可行性规划，不依赖求解器返回的乘子）。

    梯度 g_j=(w_j-d_j)/d_j。存在可行乘子 λ 使：
      内部点：A'λ = -g；下界点：A'λ ≥ -g；上界点：A'λ ≤ -g。
    用带松弛变量的 LP 最小化内部等式违反量；松弛为零（容差内）即存在
    合法乘子，无论约束矩阵是否秩亏损（重叠/冗余单元）。
    """
    from scipy.optimize import linprog

    m = A.shape[0]
    g = (w - d) / d
    btol = tol.bound_abs + tol.bound_rel * np.maximum(1.0, np.maximum(lo_w, hi_w))
    interior = (w > lo_w + btol) & (w < hi_w - btol)
    at_lower = (~interior) & (w <= lo_w + btol)
    at_upper = (~interior) & (~at_lower)

    At = A.T.toarray() if sparse.issparse(A) else A.T
    # 变量 z=(λ[m], t+[nI], t-[nI])；先全部按不等式建模，内部点再加反向不等式
    n_int = int(interior.sum())
    n_var = m + 2 * n_int
    c_obj = np.zeros(n_var)
    c_obj[m:] = 1.0
    A_ub_rows, b_ub = [], []

    def add_ineq(row_coef, rhs):
        A_ub_rows.append(row_coef)
        b_ub.append(rhs)

    int_positions = np.where(interior)[0]
    int_slot = {j: k for k, j in enumerate(int_positions)}

    for j in range(len(w)):
        row_ub = np.zeros(n_var)
        row_ub[:m] = At[j]
        if interior[j]:
            k = int_slot[j]
            # A'λ - t+ + t- <= -g  与  -A'λ - t+ + t- <= g
            row_ub[m + k] = -1.0
            row_ub[m + n_int + k] = 1.0
            add_ineq(row_ub.copy(), -g[j])
            row2 = -row_ub
            row2[m + k] = -1.0
            row2[m + n_int + k] = 1.0
            add_ineq(row2, g[j])
        elif at_lower[j]:
            add_ineq(row_ub, -g[j])          # A'λ >= -g
        else:  # at_upper
            add_ineq(-row_ub, g[j])          # A'λ <= -g

    lp = linprog(
        c_obj,
        A_ub=np.array(A_ub_rows),
        b_ub=np.array(b_ub),
        # 乘子 λ 自由（m 个），松弛变量 t± 必须非负（2*n_int 个）
        bounds=[(None, None)] * m + [(0.0, None)] * (2 * n_int),
        method="highs",
    )
    if not lp.success:
        # LP 本身没解出来属于数值未决，不能据此宣布最优性失败
        return {
            "passed": False,
            "worst_stationarity": float("inf"),
            "stationarity_tol": tol.stationarity_abs,
            "n_interior": n_int,
            "n_at_lower": int(at_lower.sum()),
            "n_at_upper": int(at_upper.sum()),
            "lp_status": lp.status,
            "lp_message": lp.message,
        }
    slack_max = float(np.max(lp.x[m:])) if n_int else 0.0
    scale = max(1.0, float(np.max(np.abs(g))))
    stat_tol = tol.stationarity_abs + tol.stationarity_rel * scale
    return {
        "passed": slack_max <= stat_tol,
        "worst_stationarity": slack_max,
        "stationarity_tol": stat_tol,
        "n_interior": n_int,
        "n_at_lower": int(at_lower.sum()),
        "n_at_upper": int(at_upper.sum()),
        "lp_status": lp.status,
    }


def verify_weights(w, d, lo_w, hi_w, A, b, codes, tol: Tolerance, *, lam=None):
    """独立复核一份权重向量，返回结构化证据。任何一项不过即不得发布。"""
    w = np.asarray(w, dtype=float)
    Aw = A @ w
    residuals = Aw - b
    per_unit_tol = np.array([tol.residual_tol(t) for t in b])
    res_abs = np.abs(residuals)
    residual_ok = bool(np.all(res_abs <= per_unit_tol))

    btol = tol.bound_abs + tol.bound_rel * np.maximum(1.0, np.maximum(lo_w, hi_w))
    lower_violation = np.maximum(0.0, lo_w - w)
    upper_violation = np.maximum(0.0, w - hi_w)
    bound_ok = bool(np.all(lower_violation <= btol) and np.all(upper_violation <= btol))

    objective = _primal_objective(w, d)
    kkt = _kkt_stationarity(w, d, lo_w, hi_w, A, tol)

    gap = None
    gap_ok = True
    if lam is not None:
        # 弱对偶复核：重新独立计算对偶目标，检查相对对偶间隙
        Atlam = A.T @ lam
        a, c = lo_w / d, hi_w / d
        q = np.clip(-Atlam, a - 1.0, c - 1.0)
        dual_obj = 0.5 * float(np.dot(d, q * q)) + float(np.dot(Atlam, d * (1.0 + q))) - float(
            np.dot(lam, b)
        )
        gap = abs(objective + dual_obj) / max(1.0, abs(objective))
        gap_ok = gap <= tol.gap_rel

    per_unit = [
        {
            "control": codes[i],
            "target": float(b[i]),
            "achieved": float(Aw[i]),
            "residual": float(residuals[i]),
            "tol": float(per_unit_tol[i]),
            "passed": bool(res_abs[i] <= per_unit_tol[i]),
        }
        for i in range(len(codes))
    ]
    worst_res = float(np.max(res_abs))
    return {
        "passed": bool(residual_ok and bound_ok and kkt["passed"] and gap_ok),
        "residual_ok": residual_ok,
        "bound_ok": bound_ok,
        "stationarity_ok": kkt["passed"],
        "gap_ok": gap_ok,
        "worst_residual_abs": worst_res,
        "worst_bound_violation": float(
            max(np.max(lower_violation), np.max(upper_violation))
        ),
        "relative_dual_gap": gap,
        "objective_recomputed": objective,
        "per_unit": per_unit,
        "kkt": {k: v for k, v in kkt.items() if k != "lambda_estimate"},
    }


def _residual_report(w, A, b, codes, tol: Tolerance):
    Aw = A @ w
    r = Aw - b
    per_unit_tol = np.array([tol.residual_tol(t) for t in b])
    ok = bool(np.all(np.abs(r) <= per_unit_tol))
    return ok, float(np.max(np.abs(r))), [
        {
            "control": codes[i],
            "target": float(b[i]),
            "achieved": float(Aw[i]),
            "residual": float(r[i]),
            "tol": float(per_unit_tol[i]),
        }
        for i in range(len(codes))
    ]


# ---------------------------------------------------------------------------
# 主编排
# ---------------------------------------------------------------------------
def calibrate(
    sample: Sample,
    units: tuple[ControlUnit, ...],
    membership: dict[str, frozenset[int]],
    *,
    tol: Tolerance | None = None,
    maxiter: int = 1000,
    emit_diagnostic: bool = False,
    cross_check: bool = True,
):
    """执行一次完整校准，返回 :class:`CalibrationResult`。

    状态语义：

    * ``PUBLISHABLE``：求解且独立复核（残差/边界/KKT 平稳性）全部通过；
    * ``INFEASIBLE``：结构化预检或最小残差诊断给出不可行证据（含原因与相关约束）；
    * ``UNDECIDED``：数值预算内未能判定，**不**当作数学不可行；
    * ``DIAGNOSTIC``：仅当 ``emit_diagnostic=True`` 时附带的放宽结果，显式标记。

    主求解器是原始主动集 KKT 法；可选的对偶 L-BFGS-B 仅作交叉印证，
    发布与否只取决于独立复核，不取决于任一求解器的返回码。
    """
    tol = tol or Tolerance()
    try:
        structural_prechecks(sample, units, membership, tol)
    except InfeasibleError as exc:
        # 结构化预检本身就是有依据的不可行判定，转为统一三态结果
        diag = {
            "reason": exc.reason,
            "evidence": str(exc),
            "related_constraint": exc.diagnostics,
        }
        result = CalibrationResult(
            status=CalibStatus.INFEASIBLE,
            weights=None,
            residuals=None,
            objective=None,
            diagnostics=diag,
            tol=tol.as_dict(),
            iterations=0,
            solver="structural_precheck",
            representation="n/a",
        )
        return result

    d = sample.base_weights
    lo_w, hi_w = sample.person_bounds()
    A, representation, codes = build_constraint_matrix(sample, membership)
    b = np.array([next(u.target for u in units if u.code == c) for c in codes], dtype=float)

    # ---- 主路径：原始主动集 KKT 法 ---------------------------------------
    w, as_info = active_set_solve(A, b, d, lo_w, hi_w, max_sweeps=maxiter)
    verify = verify_weights(w, d, lo_w, hi_w, A, b, codes, tol)

    # 交叉印证：独立的对偶 L-BFGS-B 路径，结果只作旁证
    cross = None
    if cross_check:
        try:
            w2, lam2, opt2 = _dual_solve(A, b, d, lo_w, hi_w, maxiter=maxiter)
            cross = {
                "dual_status": opt2.message,
                "dual_worst_residual": float(np.max(np.abs(A @ w2 - b))),
                "max_weight_diff_vs_dual": float(np.max(np.abs(w2 - w))),
                "objective_dual": _primal_objective(w2, d),
            }
        except Exception as exc:  # 交叉路径失败不影响主路径判定，仅记录
            cross = {"dual_error": repr(exc)}

    if verify["passed"]:
        diag = {"active_set": as_info, "verify": verify}
        if cross is not None:
            diag["cross_check"] = cross
        return CalibrationResult(
            status=CalibStatus.PUBLISHABLE,
            weights=tuple(float(x) for x in w),
            residuals=tuple(float(x) for x in (A @ w - b)),
            objective=verify["objective_recomputed"],
            diagnostics=diag,
            tol=tol.as_dict(),
            iterations=int(as_info.get("sweeps", 0)),
            solver="primal_active_set_KKT"
            + ("+L-BFGS-B(dual,crosscheck)" if cross_check else ""),
            representation=representation,
        )

    if as_info.get("reason") == "active_set_did_not_settle":
        # 主动集未在预算内稳定：不宣布可行也不宣布不可行 => 未决
        diag = {
            "reason": "active_set_budget_exceeded",
            "active_set_info": as_info,
            "verify_attempt": verify,
        }
        result = CalibrationResult(
            status=CalibStatus.UNDECIDED,
            weights=None,
            residuals=None,
            objective=None,
            diagnostics=diag,
            tol=tol.as_dict(),
            iterations=maxiter,
            solver="primal_active_set_KKT",
            representation=representation,
        )
        if emit_diagnostic:
            w_b, _ = _bounded_min_residual(A, b, lo_w, hi_w)
            object.__setattr__(
                result,
                "diagnostics",
                {**diag, "diagnostic_weights": tuple(float(x) for x in w_b)},
            )
        return result

    # ---- 复核未通过：用有界/无界最小残差区分不可行类型 --------------------
    w_b, b_res = _bounded_min_residual(A, b, lo_w, hi_w)
    bounded_ok, bounded_worst, bounded_detail = _residual_report(w_b, A, b, codes, tol)

    if bounded_ok:
        # 有界可行点存在，但它不满足卡方最优性的 KKT 证据 => 未决而非不可行
        diag = {
            "reason": "feasible_point_but_optimality_unconfirmed",
            "active_set_verify": verify,
            "bounded_min_residual": bounded_detail,
        }
        result = CalibrationResult(
            status=CalibStatus.UNDECIDED,
            weights=None,
            residuals=None,
            objective=None,
            diagnostics=diag,
            tol=tol.as_dict(),
            iterations=int(as_info.get("sweeps", 0)),
            solver="primal_active_set_KKT+lsq_linear",
            representation=representation,
        )
        if emit_diagnostic:
            object.__setattr__(
                result,
                "diagnostics",
                {**diag, "diagnostic_weights": tuple(float(x) for x in w_b)},
            )
        return result

    w_u, ub_converged = _unbounded_min_residual(A, b)
    unbounded_ok, unbounded_worst, unbounded_detail = _residual_report(
        w_u, A, b, codes, tol
    )
    if not ub_converged:
        return CalibrationResult(
            status=CalibStatus.UNDECIDED,
            weights=None,
            residuals=None,
            objective=None,
            diagnostics={
                "reason": "unbounded_feasibility_solve_did_not_converge",
                "active_set_verify": verify,
                "bounded_min_residual": bounded_detail,
            },
            tol=tol.as_dict(),
            iterations=int(as_info.get("sweeps", 0)),
            solver="primal_active_set_KKT+lsqr",
            representation=representation,
        )

    reason = "bound_infeasible" if unbounded_ok else "contradiction"
    diag = {
        "reason": reason,
        "active_set_verify": verify,
        "bounded_min_residual": bounded_detail,
        "unbounded_min_residual": unbounded_detail,
        "evidence": (
            "去掉全部权重边界后最小残差在容差内 => 边界使其不可行"
            if unbounded_ok
            else "即使去掉权重边界，控制方程仍无精确解 => 控制目标互相矛盾"
        ),
    }
    result = CalibrationResult(
        status=CalibStatus.INFEASIBLE,
        weights=None,
        residuals=None,
        objective=None,
        diagnostics=diag,
        tol=tol.as_dict(),
        iterations=int(as_info.get("sweeps", 0)),
        solver="primal_active_set_KKT+lsq_linear+lsqr",
        representation=representation,
    )
    if emit_diagnostic:
        object.__setattr__(
            result,
            "diagnostics",
            {**diag, "diagnostic_weights": tuple(float(x) for x in w_b)},
        )
    return result


# ---------------------------------------------------------------------------
# 产品构造与独立验证（含外部产品）
# ---------------------------------------------------------------------------
def build_product(
    sample: Sample,
    units: tuple[ControlUnit, ...],
    membership: dict[str, frozenset[int]],
    result,
    *,
    created_from: dict,
    note: str = "",
):
    """把可发布结果固化为不可变 :class:`WeightProduct`。"""
    if result.status not in (CalibStatus.PUBLISHABLE, CalibStatus.DIAGNOSTIC):
        raise InfeasibleError(
            "not_publishable",
            f"校准状态为 {result.status.value}，不得构造权重产品",
            result.diagnostics,
        )
    kind = (
        ProductKind.DIAGNOSTIC
        if result.status == CalibStatus.DIAGNOSTIC
        else ProductKind.OFFICIAL
    )
    controls_snapshot = [
        {
            "code": u.code,
            "target": u.target,
            "selector": u.selector,
            "members": list(u.members),
            "label": u.label,
            "resolved_member_ids": [sample.persons[j].person_id for j in sorted(membership[u.code])],
        }
        for u in units
    ]
    person_ratio = [
        {
            "person_id": p.person_id,
            "lo": p.lo if p.lo is not None else sample.design.global_lo,
            "hi": p.hi if p.hi is not None else sample.design.global_hi,
        }
        for p in sample.persons
    ]
    person_design = [dict(p.design) for p in sample.persons]
    payload = {
        "sample_ids": list(sample.ids),
        "design_summary": {
            "fields": {k: list(v) for k, v in sample.design.fields.items()},
            "n_persons": len(sample.persons),
        },
        "controls": controls_snapshot,
        "global_bounds": [sample.design.global_lo, sample.design.global_hi],
        "person_bounds_ratio": person_ratio,
        "person_design": person_design,
        "base_weights": list(sample.base_weights),
        "weights": list(result.weights),
        "residuals": list(result.residuals),
        "objective": result.objective,
        "tol": result.tol,
        "solver": result.solver,
        "representation": result.representation,
        "created_from": created_from,
        "note": note,
        "kind": kind.value,
    }
    return WeightProduct(
        product_id=content_id("wp", payload),
        kind=kind,
        sample_ids=tuple(payload["sample_ids"]),
        design_summary=payload["design_summary"],
        controls=tuple(controls_snapshot),
        global_bounds=(sample.design.global_lo, sample.design.global_hi),
        person_bounds_ratio=tuple(person_ratio),
        person_design=tuple(person_design),
        base_weights=tuple(sample.base_weights),
        weights=result.weights,
        residuals=result.residuals,
        objective=result.objective,
        tol=result.tol,
        solver=result.solver,
        representation=result.representation,
        created_from=created_from,
        note=note,
    )


def verify_product(product: WeightProduct, *, tol: Tolerance | None = None) -> dict:
    """对（可能来自外部的）权重产品做完全独立的复核。

    依据产品自带的成员快照重建成员矩阵，重算残差、边界、目标与 KKT，
    并核对产品中登记的残差与重算值是否一致（防篡改/防损坏）。
    """
    tol = tol or Tolerance(**{k: v for k, v in product.tol.items() if k in Tolerance().as_dict()})
    ids = list(product.sample_ids)
    n = len(ids)
    idx_of = {pid: i for i, pid in enumerate(ids)}
    d = np.array(product.base_weights, dtype=float)
    w = np.array(product.weights, dtype=float)

    ratio = product.person_bounds_ratio
    lo_w = np.array([r["lo"] for r in ratio], dtype=float) * d
    hi_w = np.array([r["hi"] for r in ratio], dtype=float) * d

    codes = [c["code"] for c in product.controls]
    b = np.array([c["target"] for c in product.controls], dtype=float)
    m = len(codes)
    dense = n * m < SPARSE_THRESHOLD
    if dense:
        A = np.zeros((m, n), dtype=float)
        for i, c in enumerate(product.controls):
            for pid in c["resolved_member_ids"]:
                if pid not in idx_of:
                    raise InputError(f"控制 {c['code']} 快照含未知样本 ID: {pid}")
                A[i, idx_of[pid]] = 1.0
    else:
        rows, cols = [], []
        for i, c in enumerate(product.controls):
            for pid in c["resolved_member_ids"]:
                if pid not in idx_of:
                    raise InputError(f"控制 {c['code']} 快照含未知样本 ID: {pid}")
                rows.append(i)
                cols.append(idx_of[pid])
        A = sparse.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(m, n))

    verify = verify_weights(w, d, lo_w, hi_w, A, b, codes, tol)

    # 登记残差与重算残差必须一致，否则产品已被改动
    stored = np.array(product.residuals, dtype=float)
    recomputed = A @ w - b
    tamper = float(np.max(np.abs(stored - recomputed)))
    verify["stored_residual_match"] = bool(tamper <= tol.residual_abs)
    verify["stored_residual_max_diff"] = tamper
    # 登记目标值同样独立重算
    verify["objective_matches_stored"] = bool(
        product.objective is None
        or abs(verify["objective_recomputed"] - product.objective)
        <= tol.gap_rel * max(1.0, abs(verify["objective_recomputed"]))
    )
    verify["passed"] = bool(
        verify["passed"] and verify["stored_residual_match"] and verify["objective_matches_stored"]
    )
    verify["product_id"] = product.product_id
    verify["kind"] = product.kind.value
    return verify


def require_publishable(result) -> None:
    """发布前守卫：不可行/未决/诊断结果一律不得作为正式权重发布。"""
    if result.status == CalibStatus.INFEASIBLE:
        raise InfeasibleError(
            result.diagnostics.get("reason", "infeasible"),
            "校准不可行，拒绝发布；诊断依据见 diagnostics",
            result.diagnostics,
        )
    if result.status == CalibStatus.UNDECIDED:
        raise UndecidedError("数值求解未决，拒绝发布；这不等于数学不可行", result.diagnostics)
    if result.status == CalibStatus.DIAGNOSTIC:
        raise InfeasibleError(
            "diagnostic_not_official",
            "诊断性放宽结果不得冒充正式权重发布",
            result.diagnostics,
        )
