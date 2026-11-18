"""独立分组统计模块。

契约
----
* 输入是**已固定**的 :class:`~surveycal.models.WeightProduct` 与数值回答；
  本模块绝不重新执行校准优化，也不修改任何权重；
* 可消费合法外部权重产品（身份对齐并通过
  :func:`surveycal.calibration.verify_product` 后）；
* 按地区、年龄组或登记的交叉维度计算加权总量与均值；
* 某指标缺失的人不进入该指标均值的分子与分母，但其权重单独报告：
  总体权重 = 有效回答权重 + 缺失权重；
* 分母（有效回答权重）为零 => 显式 ``无结果``，绝不把缺失填零；
* 同一人的不同指标可以有不同的有效分母（各指标缺失集合独立）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import IdentityError, InputError
from .models import GroupCell, ProductKind, ResponseBatch, WeightProduct

CROSS_SEP = "||"  # 交叉组取值的规范连接符，如 "R1||A1"


@dataclass(frozen=True)
class Report:
    """一份绑定到具体权重产品的分组统计报告（产品更换必须重发）。"""

    report_id: str
    product_id: str
    group_field: str
    cells: tuple[GroupCell, ...]

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "product_id": self.product_id,
            "group_field": self.group_field,
            "cells": [cell.__dict__.copy() for cell in self.cells],
        }


def _design_field_names(product: WeightProduct) -> list[str]:
    return list(product.design_summary["fields"].keys())


def available_group_fields(product: WeightProduct) -> list[str]:
    """可用于分组的字段：登记的全部设计字段及其交叉组合。"""
    names = _design_field_names(product)
    out = list(names)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            out.append(names[i] + CROSS_SEP + names[j])
    return out


def _resolve_groups(product: WeightProduct, group_field: str):
    """返回 [(组取值, 成员行下标...)]，类别清单全部来自产品快照，不猜样本。"""
    fields = product.design_summary["fields"]
    id_index = {pid: i for i, pid in enumerate(product.sample_ids)}

    if group_field in fields:
        groups = [
            (cat, [i for i, pid in enumerate(product.sample_ids)
                   if _person_design(product, i).get(group_field) == cat])
            for cat in fields[group_field]
        ]
        return groups

    if CROSS_SEP in group_field:
        parts = group_field.split(CROSS_SEP)
        if any(p not in fields for p in parts):
            raise InputError(f"未知分组字段: {group_field}")
        # 交叉类别取登记类别的笛卡尔积，含样本未出现的组合（结果为零权重组）
        combos = [()]
        for p in parts:
            combos = [base + (cat,) for base in combos for cat in fields[p]]
        groups = []
        for combo in combos:
            value = CROSS_SEP.join(combo)
            members = []
            for i in range(len(product.sample_ids)):
                design = _person_design(product, i)
                if all(design.get(parts[k]) == combo[k] for k in range(len(parts))):
                    members.append(i)
            groups.append((value, members))
        return groups

    raise InputError(
        f"未知分组字段 {group_field!r}，可选: {available_group_fields(product)}"
    )


def _person_design(product: WeightProduct, i: int) -> dict[str, str]:
    """直接读取产品快照中每人的设计字段（外部产品同样自包含）。"""
    return dict(product.person_design[i])


def check_identity(product: WeightProduct, batches: dict[str, ResponseBatch]) -> None:
    """回答身份校验：未知或重复 ID 在导入时已拒；此处再核对产品身份集合。

    缺少某人回答 => 合法，按该指标缺失处理（不报错、不补零）。
    """
    product_ids = set(product.sample_ids)
    if len(product_ids) != len(product.sample_ids):
        raise IdentityError("权重产品内部样本 ID 重复，拒绝统计")
    for ind, batch in batches.items():
        unknown = (set(batch.values) | set(batch.missing_ids)) - product_ids
        if unknown:
            raise IdentityError(
                f"指标 {ind} 的回答含权重产品未知 ID: {sorted(unknown)}，拒绝统计"
            )


def _cell_for(
    product: WeightProduct,
    indicator: str,
    batch: ResponseBatch | None,
    group_field: str,
    group_value: str,
    members: list[int],
) -> GroupCell:
    weights = product.weights
    total_w = sum(weights[i] for i in members)
    valid_w = 0.0
    missing_w = 0.0
    weighted_total = 0.0
    valid_n = 0
    missing_n = 0
    values = batch.values if batch is not None else {}
    missing_set = set(batch.missing_ids) if batch is not None else set()

    for i in members:
        pid = product.sample_ids[i]
        if pid in values:
            # 真实零值同样进入分子与分母，与缺失严格区分
            valid_w += weights[i]
            weighted_total += weights[i] * values[pid]
            valid_n += 1
        else:
            # 显式缺失行，或该人对本指标根本没有回答行，均按缺失处理
            missing_w += weights[i]
            missing_n += 1
            _ = pid in missing_set  # 两种缺失来源语义等价，均不填零

    if valid_w == 0.0:
        return GroupCell(
            indicator=indicator,
            group_field=group_field,
            group_value=group_value,
            total_weight=float(total_w),
            valid_weight=0.0,
            missing_weight=float(missing_w),
            weighted_total=None,
            mean=None,
            valid_n=valid_n,
            missing_n=missing_n,
            note="有效回答权重为零，均值与加权总量无结果（缺失未填零）",
        )
    return GroupCell(
        indicator=indicator,
        group_field=group_field,
        group_value=group_value,
        total_weight=float(total_w),
        valid_weight=float(valid_w),
        missing_weight=float(missing_w),
        weighted_total=float(weighted_total),
        mean=float(weighted_total / valid_w),
        valid_n=valid_n,
        missing_n=missing_n,
        note="ok",
    )


def publish_report(
    product: WeightProduct,
    batches: dict[str, ResponseBatch],
    group_field: str,
    indicators: list[str] | None = None,
    *,
    report_id: str | None = None,
) -> Report:
    """对一个分组字段、若干指标发布分组统计。

    权重产品与报告绑定；更换产品时即使指标相同也会得到新 ``report_id``。
    """
    check_identity(product, batches)
    if product.kind != ProductKind.OFFICIAL:
        raise InputError(
            f"产品 {product.product_id} 为 {product.kind.value}，"
            "诊断性放宽结果不得用于正式统计发布"
        )
    inds = indicators or sorted(batches.keys())
    unknown_inds = [x for x in inds if x not in batches]
    if unknown_inds:
        raise InputError(f"指标没有对应回答批次: {unknown_inds}")

    groups = _resolve_groups(product, group_field)
    cells: list[GroupCell] = []
    for ind in inds:
        batch = batches[ind]
        for value, members in groups:
            cells.append(
                _cell_for(product, ind, batch, group_field, value, members)
            )

    import hashlib

    rid = report_id or (
        "rpt_"
        + hashlib.sha256(
            (product.product_id + "|" + group_field + "|" + "|".join(inds)).encode()
        ).hexdigest()[:16]
    )
    return Report(
        report_id=rid,
        product_id=product.product_id,
        group_field=group_field,
        cells=tuple(cells),
    )


def assert_product_drives_change(old: Report, new: Report) -> dict:
    """新产品必须真实改变下游统计，而不是只换标签。

    对比同分组、同指标、同组取值的单元格数值，返回发生变化的单元清单；
    若完全无变化则拒绝（说明所谓“新产品”没有带来任何下游变化）。
    """
    key = lambda c: (c.indicator, c.group_field, c.group_value)
    old_map = {key(c): c for c in old.cells}
    new_map = {key(c): c for c in new.cells}
    if set(old_map) != set(new_map):
        raise InputError("两份报告结构（指标/分组）不一致，无法比较")
    changed = []
    for k in sorted(old_map):
        a, b = old_map[k], new_map[k]
        if (a.mean, b.mean) != (None, None):
            if a.mean is None or b.mean is None or not _close(a.mean, b.mean):
                changed.append(
                    {
                        "cell": k,
                        "old_mean": a.mean,
                        "new_mean": b.mean,
                        "old_valid_weight": a.valid_weight,
                        "new_valid_weight": b.valid_weight,
                    }
                )
    if not changed:
        raise InputError("新产品未引起任何下游统计变化，拒绝仅换标签的重发布")
    return {"changed_cells": changed, "n_changed": len(changed)}


def _close(x: float, y: float, tol: float = 1e-12) -> bool:
    return abs(x - y) <= tol * max(1.0, abs(x), abs(y))
