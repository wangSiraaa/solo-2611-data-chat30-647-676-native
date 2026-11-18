"""校准模块测试：手算核、三态分类、边界、重叠约束、重排不变性与规模路径。"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

from surveycal import calibration as C
from surveycal.calibration import Tolerance
from surveycal.errors import InfeasibleError, InputError
from surveycal.models import CalibStatus

from .conftest import load_case


# --------------------------------------------------------------------------
# 2-4 人手算严格凸问题：与 Fraction 精确解逐项核对
# --------------------------------------------------------------------------
def test_handcalc_matches_fraction_optimum(data_dir):
    from surveycal.ingest import load_controls_json, load_sample_csv, resolve_membership

    expected = json.load(open(os.path.join(data_dir["handcalc"], "handcalc_expected.json")))
    for name, sfx in [("h1", "h1"), ("h2", "h2")]:
        sample = load_sample_csv(
            os.path.join(data_dir["handcalc"], f"{sfx}_sample.csv"),
            os.path.join(data_dir["handcalc"], f"{sfx}_controls.json"),
        )
        _, units = load_controls_json(
            os.path.join(data_dir["handcalc"], f"{sfx}_controls.json")
        )
        mem = resolve_membership(sample, units)
        r = C.calibrate(sample, units, mem, cross_check=False)
        assert r.status == CalibStatus.PUBLISHABLE
        np.testing.assert_allclose(r.weights, expected[name]["weights"], atol=1e-10)
        assert abs(r.objective - expected[name]["objective"]) < 1e-10
        np.testing.assert_allclose(r.residuals, expected[name]["residuals"], atol=1e-12)


def test_handcalc_h1_manual_derivation(data_dir):
    """H1 白纸推导：min (w1-1)^2/1+(w2-2)^2/2 s.t. w1+w2=4.5 => (1.5, 3)。"""
    from surveycal.ingest import load_controls_json, load_sample_csv, resolve_membership

    sample = load_sample_csv(
        os.path.join(data_dir["handcalc"], "h1_sample.csv"),
        os.path.join(data_dir["handcalc"], "h1_controls.json"),
    )
    _, units = load_controls_json(os.path.join(data_dir["handcalc"], "h1_controls.json"))
    r = C.calibrate(sample, units, resolve_membership(sample, units), cross_check=False)
    np.testing.assert_allclose(r.weights, [1.5, 3.0], atol=1e-12)
    # 独立重算目标，不使用求解器/产品自带值
    d = sample.base_weights
    manual_obj = float(sum((w - dj) ** 2 / dj for w, dj in zip(r.weights, d)))
    assert abs(manual_obj - 0.75) < 1e-12
    assert abs(r.objective - manual_obj) < 1e-12


# --------------------------------------------------------------------------
# 场景三态
# --------------------------------------------------------------------------
def test_base_weights_already_satisfy_controls(data_dir):
    sample, units, mem = load_case(data_dir["base_satisfied"])
    r = C.calibrate(sample, units, mem)
    assert r.status == CalibStatus.PUBLISHABLE
    np.testing.assert_allclose(r.weights, sample.base_weights, atol=1e-9)
    assert r.objective < 1e-12
    v = r.diagnostics["verify"]
    assert v["residual_ok"] and v["bound_ok"] and v["stationarity_ok"]


def test_joint_marginal_publishable_and_bounded(data_dir):
    sample, units, mem = load_case(data_dir["joint_marginal"])
    r = C.calibrate(sample, units, mem)
    assert r.status == CalibStatus.PUBLISHABLE
    d = sample.base_weights
    ratios = np.array(r.weights) / d
    assert ratios.min() >= 0.5 - 1e-9 and ratios.max() <= 1.8 + 1e-9
    # 控制总量逐项满足
    A, _, codes = C.build_constraint_matrix(sample, mem)
    b = np.array([u.target for u in units])
    np.testing.assert_allclose(A @ np.array(r.weights), b, atol=1e-6)
    # 调整确实发生（不是原样返回）
    assert r.objective > 1e-6


def test_bounds_active_hits_upper_bound(data_dir):
    sample, units, mem = load_case(data_dir["bounds_active"])
    r = C.calibrate(sample, units, mem)
    assert r.status == CalibStatus.PUBLISHABLE
    ratios = np.array(r.weights) / sample.base_weights
    # P1/P2 被 REG_R1=6 顶到上界 2.0；P3 自由
    np.testing.assert_allclose(ratios[:2], [2.0, 2.0], atol=1e-8)
    assert abs(ratios[2] - 1.0) < 1e-8
    v = r.diagnostics["verify"]
    assert v["kkt"]["n_at_upper"] >= 2


def test_empty_required_unit_is_infeasible(data_dir):
    sample, units, mem = load_case(data_dir["empty_unit"])
    r = C.calibrate(sample, units, mem)
    assert r.status == CalibStatus.INFEASIBLE
    assert r.diagnostics["reason"] == "empty_required_unit"
    assert r.diagnostics["related_constraint"]["control"] == "REG_R5_EMPTY"
    assert r.weights is None


def test_contradictory_controls_are_infeasible(data_dir):
    sample, units, mem = load_case(data_dir["contradiction"])
    r = C.calibrate(sample, units, mem)
    assert r.status == CalibStatus.INFEASIBLE
    assert r.diagnostics["reason"] == "contradiction"


def test_bound_capacity_precheck(tmp_path):
    """边界容量不足直接判不可行，并给出相关成员与上下界和。"""
    import csv
    import json as _json

    sdir = tmp_path / "bi"
    sdir.mkdir()
    with open(sdir / "sample.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["person_id", "base_weight", "region", "age_group", "lo", "hi"])
        w.writerow(["P1", 1.0, "R1", "A1", 0.5, 1.5])
    _json.dump(
        {
            "design": {"region": ["R1", "R2"], "age_group": ["A1", "A2"]},
            "bounds": {"lo": 0.5, "hi": 1.5},
            "controls": [{"code": "TOTAL", "target": 2.0, "selector": {}}],
        },
        open(sdir / "controls.json", "w"),
    )
    sample, units, mem = load_case(str(sdir))
    r = C.calibrate(sample, units, mem)
    assert r.status == CalibStatus.INFEASIBLE
    assert r.diagnostics["reason"] == "bound_infeasible"
    assert r.diagnostics["related_constraint"]["sum_upper_weights"] == pytest.approx(1.5)


# --------------------------------------------------------------------------
# 独立性复核：不能只凭求解器成功发布
# --------------------------------------------------------------------------
def test_independent_verify_recomputes_everything(data_dir):
    sample, units, mem = load_case(data_dir["joint_marginal"])
    r = C.calibrate(sample, units, mem)
    product = C.build_product(sample, units, mem, r, created_from={"t": 1})
    verify = C.verify_product(product)
    assert verify["passed"]
    assert verify["stored_residual_match"]
    assert verify["objective_matches_stored"]
    # 每个控制单元都有独立残差证据
    assert len(verify["per_unit"]) == len(units)
    assert all(u["passed"] for u in verify["per_unit"])


def test_tampered_product_fails_verification(data_dir):
    sample, units, mem = load_case(data_dir["joint_marginal"])
    r = C.calibrate(sample, units, mem)
    product = C.build_product(sample, units, mem, r, created_from={"t": 1})
    # 篡改某个人的权重
    object.__setattr__(
        product,
        "weights",
        product.weights[:1] + (product.weights[0] + 0.77,) + product.weights[2:],
    )
    verify = C.verify_product(product)
    assert not verify["passed"]
    assert not verify["residual_ok"]


def test_tolerance_violation_blocks_publish(data_dir):
    sample, units, mem = load_case(data_dir["joint_marginal"])
    r = C.calibrate(sample, units, mem)
    # 收紧到机器精度以下 => 同一解的浮点残差/平稳性无法通过复核
    strict = Tolerance(
        residual_abs=1e-16, residual_rel=1e-16,
        bound_abs=1e-16, bound_rel=1e-16,
        stationarity_abs=1e-16, stationarity_rel=1e-16,
    )
    product = C.build_product(sample, units, mem, r, created_from={"t": 1})
    verify = C.verify_product(product, tol=strict)
    assert not verify["passed"]


# --------------------------------------------------------------------------
# 重排不变性与规模
# --------------------------------------------------------------------------
def test_input_reorder_does_not_change_solution(data_dir):
    s1, u1, m1 = load_case(data_dir["joint_marginal"])
    s2, u2, m2 = load_case(data_dir["rearranged"])
    r1 = C.calibrate(s1, u1, m1, cross_check=False)
    r2 = C.calibrate(s2, u2, m2, cross_check=False)
    w1 = dict(zip(s1.ids, r1.weights))
    w2 = dict(zip(s2.ids, r2.weights))
    assert set(w1) == set(w2)
    for pid in w1:
        assert abs(w1[pid] - w2[pid]) < 1e-10
    assert abs(r1.objective - r2.objective) < 1e-10


def test_large_scale_sparse_path(data_dir):
    sample, units, mem = load_case(data_dir["large_5000"])
    assert len(sample.persons) == 5000
    assert len(units) == 100
    r = C.calibrate(sample, units, mem)
    assert r.representation == "sparse"
    assert r.status == CalibStatus.PUBLISHABLE
    v = r.diagnostics["verify"]
    assert v["residual_ok"] and v["bound_ok"] and v["stationarity_ok"]
    ratios = np.array(r.weights) / sample.base_weights
    assert ratios.min() >= 0.6 - 1e-9 and ratios.max() <= 1.7 + 1e-9


def test_overlapping_controls_allowed(data_dir):
    """大规模案例含显式 members 的重叠单元，求解与复核必须正常。"""
    sample, units, mem = load_case(data_dir["large_5000"])
    ovl = [u for u in units if u.code.startswith("OVL_")]
    assert ovl, "应至少有一个显式 members 的重叠单元"
    # 重叠确实存在：某个样本属于多个单元
    counts = np.zeros(len(sample.persons), dtype=int)
    for u in units:
        for j in mem[u.code]:
            counts[j] += 1
    assert counts.max() > 1


# --------------------------------------------------------------------------
# 输入登记守卫
# --------------------------------------------------------------------------
def test_unknown_category_rejected(tmp_path):
    import csv
    import json as _json

    sdir = tmp_path / "badcat"
    sdir.mkdir()
    with open(sdir / "sample.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["person_id", "base_weight", "region", "age_group", "lo", "hi"])
        w.writerow(["P1", 1.0, "RX", "A1", "", ""])  # RX 未登记
    _json.dump(
        {
            "design": {"region": ["R1"], "age_group": ["A1"]},
            "bounds": {"lo": 0.5, "hi": 2.0},
            "controls": [{"code": "TOTAL", "target": 1.0, "selector": {}}],
        },
        open(sdir / "controls.json", "w"),
    )
    with pytest.raises(InputError):
        load_case(str(sdir))


def test_undecided_is_distinct_from_infeasible(data_dir):
    """数值预算耗尽 => UNDECIDED；同一问题给足预算 => PUBLISHABLE。"""
    sample, units, mem = load_case(data_dir["joint_marginal"])
    starved = C.calibrate(sample, units, mem, maxiter=0, cross_check=False)
    assert starved.status == CalibStatus.UNDECIDED
    assert starved.diagnostics["reason"] == "active_set_budget_exceeded"
    funded = C.calibrate(sample, units, mem, maxiter=200, cross_check=False)
    assert funded.status == CalibStatus.PUBLISHABLE
    # 未决结果不允许发布
    from surveycal.errors import UndecidedError

    with pytest.raises(UndecidedError):
        C.require_publishable(starved)
    # 诊断候选权重可附带，但状态仍是未决，不冒充正式权重
    diag = C.calibrate(sample, units, mem, maxiter=0, emit_diagnostic=True, cross_check=False)
    assert "diagnostic_weights" in diag.diagnostics
    assert diag.status == CalibStatus.UNDECIDED


def test_invalid_bounds_rejected():
    from surveycal.models import DesignSpec

    with pytest.raises(InputError):
        DesignSpec({"region": ("R1",)}, global_lo=2.0, global_hi=1.0)
    with pytest.raises(InputError):
        DesignSpec({"region": ("R1",)}, global_lo=-0.1, global_hi=1.0)
