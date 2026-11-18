"""输入登记：解析样本 CSV、控制数 JSON、回答 CSV 与外部权重产品。

登记原则（对应需求）：

* 类别、控制单元及成员判定全部**显式列出**，不从样本猜测缺失类别；
* 所有输入数值必须有限，控制目标非负，权重比边界为正且下界不大于上界；
* 零与缺失严格区分：回答 CSV 中空白单元格是缺失，``0`` 是真实零值；
* 控制单元可互相重叠（同一人可属于多个单元）。
"""

from __future__ import annotations

import csv
import hashlib
import json
import math

from .errors import IdentityError, InputError
from .models import (
    ControlUnit,
    DesignSpec,
    Person,
    ProductKind,
    ResponseBatch,
    Sample,
    WeightProduct,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _finite_float(raw, what: str) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{what} 必须是数值，收到 {raw!r}") from exc
    if not math.isfinite(v):
        raise InputError(f"{what} 必须是有限数值，收到 {raw!r}")
    return v


def load_sample_csv(path: str, controls_path: str) -> Sample:
    """样本 CSV 与控制数 JSON 联合登记（设计类别清单来自控制数文件）。

    样本 CSV 列：``person_id,base_weight,region,age_group[,lo,hi]``，
    其中 ``lo/hi`` 可选，用于个人专属权重比边界，缺省用全局边界。
    """
    raw = open(path, "rb").read()
    spec, _ = load_controls_json(controls_path)
    seen: set[str] = set()
    persons: list[Person] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        required = {"person_id", "base_weight", *spec.fields.keys()}
        missing_cols = required - set(reader.fieldnames or [])
        if missing_cols:
            raise InputError(f"样本 CSV 缺少列: {sorted(missing_cols)}")
        for line_no, row in enumerate(reader, start=2):
            pid = (row.get("person_id") or "").strip()
            if not pid:
                raise InputError(f"样本 CSV 第 {line_no} 行缺少 person_id")
            if pid in seen:
                raise InputError(f"样本 ID 重复: {pid}（第 {line_no} 行）")
            seen.add(pid)
            bw = _finite_float(row.get("base_weight"), f"{pid}.base_weight")
            if bw <= 0:
                raise InputError(f"{pid}.base_weight 必须为正，收到 {bw}")
            design_values: dict[str, str] = {}
            for fname, cats in spec.fields.items():
                val = (row.get(fname) or "").strip()
                if val not in cats:
                    # 显式类别清单之外的值直接拒绝，绝不临时新增类别
                    raise InputError(
                        f"{pid} 的设计字段 {fname}={val!r} 不在登记类别 {list(cats)} 内"
                    )
                design_values[fname] = val
            lo = hi = None
            if row.get("lo") not in (None, ""):
                lo = _finite_float(row.get("lo"), f"{pid}.lo")
            if row.get("hi") not in (None, ""):
                hi = _finite_float(row.get("hi"), f"{pid}.hi")
            persons.append(Person(pid, bw, design_values, lo, hi))
    if not persons:
        raise InputError("样本 CSV 为空")
    return Sample(tuple(persons), spec, _sha256_bytes(raw))


def load_controls_json(path: str) -> tuple[DesignSpec, tuple[ControlUnit, ...]]:
    """解析控制数 JSON，并解析每个控制单元的显式成员判定。"""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    try:
        fields = {str(k): tuple(map(str, v)) for k, v in doc["design"].items()}
        bounds = doc["bounds"]
        lo = _finite_float(bounds["lo"], "bounds.lo")
        hi = _finite_float(bounds["hi"], "bounds.hi")
        raw_units = doc["controls"]
    except KeyError as exc:
        raise InputError(f"控制数 JSON 缺少必需键: {exc}") from exc
    if lo <= 0 or hi <= 0 or lo > hi:
        raise InputError(f"非法全局边界 lo={lo}, hi={hi}（需为正且 lo<=hi）")
    spec = DesignSpec(fields=fields, global_lo=lo, global_hi=hi)

    units: list[ControlUnit] = []
    seen_codes: set[str] = set()
    for item in raw_units:
        code = str(item.get("code") or "")
        if not code:
            raise InputError("控制单元缺少非空 code")
        if code in seen_codes:
            raise InputError(f"控制单元 code 重复: {code}")
        seen_codes.add(code)
        target = _finite_float(item.get("target"), f"control {code}.target")
        if target < 0:
            raise InputError(f"控制 {code} 目标必须非负，收到 {target}")
        selector = item.get("selector")
        members = tuple(item.get("members") or ())
        # selector 键缺失才是未给出；空 dict {} 是合法的总体选择器
        if "selector" not in item and not members:
            raise InputError(f"控制 {code} 必须显式给出 selector 或 members")
        if selector is not None:
            if not isinstance(selector, dict):
                raise InputError(f"控制 {code} selector 必须是对象")
            for fname, cat in selector.items():
                if fname not in fields:
                    raise InputError(f"控制 {code} 引用了未登记的设计字段 {fname}")
                if cat not in fields[fname]:
                    raise InputError(
                        f"控制 {code} 的 {fname}={cat!r} 不在登记类别 {list(fields[fname])} 内"
                    )
        if members and any(not isinstance(m, str) or not m for m in members):
            raise InputError(f"控制 {code} members 含空 ID")
        if len(set(members)) != len(members):
            raise InputError(f"控制 {code} members 含重复 ID")
        units.append(
            ControlUnit(
                code=code,
                target=float(target),
                selector=dict(selector) if selector is not None else None,
                members=members,
                label=str(item.get("label", "")),
            )
        )
    if not units:
        raise InputError("控制数 JSON 未包含任何控制单元")
    return spec, tuple(units)


def resolve_membership(
    sample: Sample, units: tuple[ControlUnit, ...]
) -> dict[str, frozenset[int]]:
    """把控制单元解析为样本行下标集合。

    * ``selector`` 为空对象 ``{}`` 表示总体（所有人）；
    * 显式 ``members`` 必须全部存在，否则登记失败；
    * 两种判定可同时给出，取并集（便于维护重叠单元）。
    """
    id_to_idx = {p.person_id: i for i, p in enumerate(sample.persons)}
    out: dict[str, frozenset[int]] = {}
    for u in units:
        idx: set[int] = set()
        if u.selector is not None:
            for i, p in enumerate(sample.persons):
                if all(p.design.get(k) == v for k, v in u.selector.items()):
                    idx.add(i)
        for pid in u.members:
            if pid not in id_to_idx:
                raise InputError(f"控制 {u.code} 的 members 引用了不存在的样本 ID: {pid}")
            idx.add(id_to_idx[pid])
        out[u.code] = frozenset(idx)
    return out


def load_responses_csv(path: str, sample: Sample) -> dict[str, ResponseBatch]:
    """长表回答 CSV：列 ``person_id,indicator,value``。

    * ``value`` 留空 => 该人该指标**缺失**；``0`` => 真实零值，二者区分；
    * 同一 ``(person_id, indicator)`` 重复、或出现样本外 ID => 整批拒绝；
    * 某人对某指标没有任何行，按缺失处理（在统计模块中自然落入缺失权重）。
    """
    raw = open(path, "rb").read()
    known = set(sample.ids)
    per_indicator: dict[str, dict[str, float]] = {}
    missing: dict[str, list[str]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not {"person_id", "indicator", "value"} <= set(reader.fieldnames or []):
            raise InputError("回答 CSV 必须包含 person_id,indicator,value 三列")
        for line_no, row in enumerate(reader, start=2):
            pid = (row.get("person_id") or "").strip()
            ind = (row.get("indicator") or "").strip()
            if not pid or not ind:
                raise InputError(f"回答 CSV 第 {line_no} 行 person_id/indicator 为空")
            if pid not in known:
                raise IdentityError(f"回答出现未知样本 ID: {pid}（第 {line_no} 行），整批拒绝")
            if (pid, ind) in seen_pairs:
                raise IdentityError(f"回答存在重复 (person_id, indicator): ({pid}, {ind})，整批拒绝")
            seen_pairs.add((pid, ind))
            raw_val = (row.get("value") or "").strip()
            if raw_val == "":
                missing.setdefault(ind, []).append(pid)
                continue
            per_indicator.setdefault(ind, {})[pid] = _finite_float(
                raw_val, f"{pid}.{ind}"
            )
    digest = _sha256_bytes(raw)
    batches: dict[str, ResponseBatch] = {}
    for ind in sorted(set(per_indicator) | set(missing)):
        vals = per_indicator.get(ind, {})
        miss = tuple(sorted(missing.get(ind, [])))
        bid = f"resp_{hashlib.sha256((ind + digest).encode()).hexdigest()[:12]}"
        batches[ind] = ResponseBatch(bid, ind, dict(vals), miss, digest)
    if not batches:
        raise InputError("回答 CSV 没有任何指标数据")
    return batches


def parse_external_product(doc: dict) -> WeightProduct:
    """从已解析的 JSON 字典构造 :class:`WeightProduct`（不重新执行校准优化）。

    结构与身份完整性在此校验；约束残差、边界、目标值由
    :func:`surveycal.calibration.verify_product` 独立重算。
    """
    required = {
        "product_id", "kind", "sample_ids", "design_summary", "controls",
        "global_bounds", "person_bounds_ratio", "base_weights", "weights",
        "residuals", "objective", "tol", "solver", "representation",
        "created_from",
    }
    absent = required - set(doc)
    if absent:
        raise InputError(f"外部权重产品缺少字段: {sorted(absent)}")
    ids = list(doc["sample_ids"])
    if len(ids) != len(set(ids)):
        raise InputError("外部权重产品 sample_ids 含重复 ID")
    n = len(ids)
    # base_weights/weights 按人；residuals 按控制单元（长度与 controls 一致）
    for key in ("base_weights", "weights"):
        if len(doc[key]) != n:
            raise InputError(f"外部权重产品 {key} 长度与 sample_ids 不一致")
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in doc[key]):
            raise InputError(f"外部权重产品 {key} 含非有限值")
    if "residuals" not in doc or len(doc["residuals"]) != len(doc["controls"]):
        raise InputError("外部权重产品 residuals 长度必须与 controls 一致（每单元一个残差）")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in doc["residuals"]):
        raise InputError("外部权重产品 residuals 含非有限值")
    if len(doc["person_bounds_ratio"]) != n:
        raise InputError("外部权重产品 person_bounds_ratio 长度不一致")
    if len(doc.get("person_design", [])) != n:
        raise InputError("外部权重产品 person_design 长度不一致或缺失")
    try:
        kind = ProductKind(doc["kind"])
    except ValueError as exc:
        raise InputError(f"外部权重产品 kind 非法: {doc['kind']!r}") from exc
    return WeightProduct(
        product_id=str(doc["product_id"]),
        kind=kind,
        sample_ids=tuple(ids),
        design_summary=dict(doc["design_summary"]),
        controls=tuple(doc["controls"]),
        global_bounds=(float(doc["global_bounds"][0]), float(doc["global_bounds"][1])),
        person_bounds_ratio=tuple(dict(x) for x in doc["person_bounds_ratio"]),
        person_design=tuple(dict(x) for x in doc["person_design"]),
        base_weights=tuple(float(v) for v in doc["base_weights"]),
        weights=tuple(float(v) for v in doc["weights"]),
        residuals=tuple(float(v) for v in doc["residuals"]),
        objective=doc["objective"],
        tol=dict(doc["tol"]),
        solver=str(doc["solver"]),
        representation=str(doc["representation"]),
        created_from=dict(doc["created_from"]),
        note=str(doc.get("note", "")),
    )


def load_external_product(path: str) -> WeightProduct:
    """从文件消费合法的外部权重产品 JSON（不重新执行校准优化）。"""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return parse_external_product(doc)


def file_sha256(path: str) -> str:
    return _sha256_file(path)
