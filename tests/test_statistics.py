"""统计模块测试：缺失/零区分、分母规则、身份校验、产品绑定与联动。"""

from __future__ import annotations

import os

import pytest

from surveycal import calibration as C, ingest, statistics
from surveycal.errors import IdentityError, InputError
from surveycal.models import ProductKind, ResponseBatch

from .conftest import load_case


@pytest.fixture()
def published(data_dir):
    sample, units, mem = load_case(data_dir["joint_marginal"])
    r = C.calibrate(sample, units, mem, cross_check=False)
    product = C.build_product(sample, units, mem, r, created_from={"case": "joint"})
    return sample, product


def _batch(indicator, values, missing, sample):
    return ResponseBatch(f"b_{indicator}", indicator, dict(values), tuple(missing), "")


def test_missing_excluded_from_numerator_and_denominator(published):
    sample, product = published
    ids = product.sample_ids
    # 第一个人缺失，第二个人真实零值
    vals = {ids[1]: 0.0, ids[2]: 10.0}
    missing = (ids[0],)
    batches = {"Y": _batch("Y", vals, missing, sample)}
    rep = statistics.publish_report(product, batches, "region")
    for cell in rep.cells:
        # 恒等式：总体权重 = 有效权重 + 缺失权重
        assert cell.total_weight == pytest.approx(
            cell.valid_weight + cell.missing_weight
        )
    # 含零值者的组：均值可为 0，但加权总量必须计入该零值
    zero_cell = next(
        c for c in rep.cells
        if product.person_design[1]["region"] == c.group_value
    )
    assert zero_cell.valid_n >= 1
    assert zero_cell.mean is not None


def test_zero_is_valid_not_missing(published):
    sample, product = published
    ids = product.sample_ids
    vals = {pid: 0.0 for pid in ids}  # 全部真实零值，无缺失
    batches = {"Z": _batch("Z", vals, (), sample)}
    rep = statistics.publish_report(product, batches, "region")
    for cell in rep.cells:
        if cell.total_weight > 0:
            assert cell.missing_n == 0
            assert cell.valid_weight == pytest.approx(cell.total_weight)
            assert cell.mean == 0.0
            assert cell.weighted_total == 0.0


def test_zero_denominator_gives_no_result(published):
    sample, product = published
    ids = product.sample_ids
    # 所有人在该指标缺失
    batches = {"B": _batch("B", {}, ids, sample)}
    rep = statistics.publish_report(product, batches, "region")
    for cell in rep.cells:
        if cell.total_weight > 0:
            assert cell.valid_weight == 0.0
            assert cell.mean is None and cell.weighted_total is None
            assert "无结果" in cell.note


def test_different_indicators_have_different_denominators(published):
    sample, product = published
    ids = product.sample_ids
    b1 = _batch("Y1", {ids[0]: 1.0, ids[1]: 2.0}, (ids[2], ids[3]), sample)
    b2 = _batch("Y2", {ids[0]: 1.0}, tuple(ids[1:]), sample)
    rep = statistics.publish_report(product, {"Y1": b1, "Y2": b2}, "region")
    v1 = sum(c.valid_weight for c in rep.cells if c.indicator == "Y1")
    v2 = sum(c.valid_weight for c in rep.cells if c.indicator == "Y2")
    assert v1 > v2  # 同一批人、不同指标，有效分母不同


def test_unknown_and_duplicate_identity_rejected(published, tmp_path):
    sample, product = published
    good = product.sample_ids[0]
    p = tmp_path / "r.csv"
    p.write_text(f"person_id,indicator,value\n{good},Y,1\nGHOST,Y,2\n", encoding="utf-8")
    with pytest.raises(IdentityError):
        ingest.load_responses_csv(str(p), sample)
    p2 = tmp_path / "r2.csv"
    p2.write_text(f"person_id,indicator,value\n{good},Y,1\n{good},Y,2\n", encoding="utf-8")
    with pytest.raises(IdentityError):
        ingest.load_responses_csv(str(p2), sample)


def test_missing_person_rows_treated_as_missing(published):
    """某人对某指标没有任何回答行 => 合法，按缺失处理，不报错不补零。"""
    sample, product = published
    ids = product.sample_ids
    vals = {ids[0]: 5.0}  # 其余人没有行
    batches = {"Y": _batch("Y", vals, (), sample)}
    statistics.check_identity(product, batches)  # 不抛异常
    rep = statistics.publish_report(product, batches, "region")
    total_missing = sum(c.missing_weight for c in rep.cells)
    assert total_missing > 0


def test_report_bound_to_product_and_revision_drives_change(data_dir):
    s1, u1, m1 = load_case(data_dir["joint_marginal"])
    s2, u2, m2 = load_case(data_dir["control_revision"])
    r1 = C.calibrate(s1, u1, m1, cross_check=False)
    r2 = C.calibrate(s2, u2, m2, cross_check=False)
    p1 = C.build_product(s1, u1, m1, r1, created_from={"rev": 1})
    p2 = C.build_product(s2, u2, m2, r2, created_from={"rev": 2})
    assert p1.product_id != p2.product_id

    ids = p1.sample_ids
    vals = {pid: float(i + 1) for i, pid in enumerate(ids)}
    batches = {"Y": _batch("Y", vals, (), s1)}
    rep1 = statistics.publish_report(p1, batches, "region")
    rep2 = statistics.publish_report(p2, batches, "region")
    assert rep1.product_id == p1.product_id and rep2.product_id == p2.product_id
    change = statistics.assert_product_drives_change(rep1, rep2)
    assert change["n_changed"] >= 1


def test_relabel_only_without_change_is_rejected(published):
    sample, product = published
    ids = product.sample_ids
    vals = {pid: float(i + 1) for i, pid in enumerate(ids)}
    batches = {"Y": _batch("Y", vals, (), sample)}
    rep1 = statistics.publish_report(product, batches, "region")
    rep2 = statistics.publish_report(
        product, batches, "region", report_id="rpt_fake_new_label"
    )
    # 同一产品、同一数据：没有任何下游变化 => 拒绝“新产品”叙事
    with pytest.raises(InputError):
        statistics.assert_product_drives_change(rep1, rep2)


def test_diagnostic_product_rejected_for_official_report(published):
    sample, product = published
    from surveycal.models import WeightProduct

    diag = WeightProduct(
        product_id="wp_diag",
        kind=ProductKind.DIAGNOSTIC,
        sample_ids=product.sample_ids,
        design_summary=product.design_summary,
        controls=product.controls,
        global_bounds=product.global_bounds,
        person_bounds_ratio=product.person_bounds_ratio,
        person_design=product.person_design,
        base_weights=product.base_weights,
        weights=tuple(w * 1.01 for w in product.weights),
        residuals=product.residuals,
        objective=None,
        tol=product.tol,
        solver="diagnostic",
        representation=product.representation,
        created_from={"mode": "diagnostic"},
    )
    batches = {"Y": _batch("Y", {pid: 1.0 for pid in product.sample_ids}, (), sample)}
    with pytest.raises(Exception):
        statistics.publish_report(diag, batches, "region")


def test_cross_group_uses_declared_categories(published):
    sample, product = published
    ids = product.sample_ids
    batches = {"Y": _batch("Y", {pid: 1.0 for pid in ids}, (), sample)}
    rep = statistics.publish_report(product, batches, "region||age_group")
    # 笛卡尔积类别数（含样本未出现的组合）
    fields = product.design_summary["fields"]
    expected_n = len(fields["region"]) * len(fields["age_group"])
    assert len({c.group_value for c in rep.cells}) == expected_n
