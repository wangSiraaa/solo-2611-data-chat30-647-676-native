"""核心数据模型。

所有模型都是不可变的值对象（``frozen=True``），权重产品一经构造即固定，
不会随后续源数据更正而改变；更正只能产生*新的*产品。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from .errors import InputError


class CalibStatus(str, Enum):
    """校准结论的三态分类（未决与不可行严格分开）。"""

    PUBLISHABLE = "publishable"          # 独立复核通过，可正式发布
    INFEASIBLE = "infeasible"            # 有依据地判定数学不可行
    UNDECIDED = "undecided"              # 数值上未能判定，不等于不可行
    DIAGNOSTIC = "diagnostic"            # 诊断性放宽结果，禁止冒充正式权重


class ProductKind(str, Enum):
    """权重产品种类：正式产品与诊断产品在产品文件中显式区分。"""

    OFFICIAL = "official"
    DIAGNOSTIC = "diagnostic"


def canonical_json(obj) -> str:
    """稳定序列化，用于内容寻址 ID 与文件导出。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_id(prefix: str, payload: dict) -> str:
    """根据产品完整内容计算确定性 ID；内容不变则 ID 不变。"""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _finite(name: str, value) -> None:
    if not isinstance(value, (int, float)) or not float("-inf") < float(value) < float("inf"):
        raise InputError(f"{name} 必须是有限数值，收到: {value!r}")


@dataclass(frozen=True)
class Person:
    """一名受访样本。

    ``base_weight`` 为正基础权重；``lo``/``hi`` 为该人专属的权重比上下界，
    缺省回退到全局边界。零回答与缺失回答在 :class:`ResponseBatch` 中区分。
    """

    person_id: str
    base_weight: float
    design: dict[str, str]
    lo: float | None = None
    hi: float | None = None

    def __post_init__(self):
        if not isinstance(self.person_id, str) or not self.person_id:
            raise InputError("样本 ID 必须是非空字符串")
        _finite("base_weight", self.base_weight)
        if self.base_weight <= 0:
            raise InputError(f"样本 {self.person_id} 基础权重必须为正，收到 {self.base_weight}")
        if self.lo is not None:
            _finite(f"{self.person_id}.lo", self.lo)
            if self.lo <= 0:
                raise InputError(f"样本 {self.person_id} 下界必须为正")
        if self.hi is not None:
            _finite(f"{self.person_id}.hi", self.hi)
            if self.hi <= 0:
                raise InputError(f"样本 {self.person_id} 上界必须为正")
        if self.lo is not None and self.hi is not None and self.lo > self.hi:
            raise InputError(f"样本 {self.person_id} 下界 {self.lo} 大于上界 {self.hi}")


@dataclass(frozen=True)
class ControlUnit:
    """一个控制单元。

    成员以两种方式**显式**登记，绝不从样本猜测缺失类别：

    * ``selector``：按设计字段类别选取（类别必须在登记的类别清单内）；
    * ``members``：直接列出样本 ID（导入时校验存在）。

    控制单元允许互相重叠（同一人可属于多个单元）。
    """

    code: str
    target: float
    selector: dict[str, str] | None = None
    members: tuple[str, ...] = field(default_factory=tuple)
    label: str = ""

    def __post_init__(self):
        if not isinstance(self.code, str) or not self.code:
            raise InputError("控制单元 code 必须为非空字符串")
        _finite(f"控制 {self.code} 目标", self.target)
        if self.target < 0:
            raise InputError(f"控制 {self.code} 目标必须非负，收到 {self.target}")
        # selector 为 None 表示未给出；空 dict {} 是合法的“总体”选择器
        if self.selector is None and not self.members:
            raise InputError(f"控制 {self.code} 必须显式给出 selector 或 members，不允许从样本猜类别")
        if self.selector is not None and any(not v or not k for k, v in self.selector.items()):
            raise InputError(f"控制 {self.code} selector 含空字段或空类别")


@dataclass(frozen=True)
class DesignSpec:
    """设计字段登记：字段名 -> 显式声明的全部合法类别（含样本中未出现的类别）。"""

    fields: dict[str, tuple[str, ...]]
    global_lo: float
    global_hi: float

    def __post_init__(self):
        _finite("global_lo", self.global_lo)
        _finite("global_hi", self.global_hi)
        if self.global_lo <= 0 or self.global_hi <= 0:
            raise InputError("全局权重比上下界必须为正")
        if self.global_lo > self.global_hi:
            raise InputError(f"全局下界 {self.global_lo} 大于上界 {self.global_hi}")
        for fname, cats in self.fields.items():
            if not cats or len(set(cats)) != len(cats):
                raise InputError(f"设计字段 {fname} 的类别清单为空或含重复类别")


@dataclass(frozen=True)
class Sample:
    """登记完成的样本集合与设计登记。"""

    persons: tuple[Person, ...]
    design: DesignSpec
    source_sha256: str = ""

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(p.person_id for p in self.persons)

    @property
    def base_weights(self):
        import numpy as np

        return np.array([p.base_weight for p in self.persons], dtype=float)

    def person_bounds(self):
        """返回每人的 (w 下界, w 上界)，即基础权重乘以权重比边界。"""
        import numpy as np

        lo = np.array(
            [(p.lo if p.lo is not None else self.design.global_lo) * p.base_weight for p in self.persons]
        )
        hi = np.array(
            [(p.hi if p.hi is not None else self.design.global_hi) * p.base_weight for p in self.persons]
        )
        return lo, hi


@dataclass(frozen=True)
class CalibrationResult:
    """校准模块的计算结论（尚未持久化）。"""

    status: CalibStatus
    weights: tuple[float, ...] | None
    residuals: tuple[float, ...] | None
    objective: float | None
    diagnostics: dict
    tol: dict
    iterations: int
    solver: str
    representation: str  # "dense" / "sparse"


@dataclass(frozen=True)
class WeightProduct:
    """固定的权重产品。保存完整快照，源数据更正不改变旧产品。"""

    product_id: str
    kind: ProductKind
    sample_ids: tuple[str, ...]
    design_summary: dict
    controls: tuple[dict, ...]
    global_bounds: tuple[float, float]
    person_bounds_ratio: tuple[dict, ...]
    person_design: tuple[dict, ...]
    base_weights: tuple[float, ...]
    weights: tuple[float, ...]
    residuals: tuple[float, ...]
    objective: float | None
    tol: dict
    solver: str
    representation: str
    created_from: dict
    note: str = ""

    def weight_map(self) -> dict[str, float]:
        return dict(zip(self.sample_ids, self.weights))

    def to_export_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "kind": self.kind.value,
            "schema_version": 1,
            "sample_ids": list(self.sample_ids),
            "design_summary": self.design_summary,
            "controls": list(self.controls),
            "global_bounds": list(self.global_bounds),
            "person_bounds_ratio": list(self.person_bounds_ratio),
            "person_design": list(self.person_design),
            "base_weights": list(self.base_weights),
            "weights": list(self.weights),
            "residuals": list(self.residuals),
            "objective": self.objective,
            "tol": self.tol,
            "solver": self.solver,
            "representation": self.representation,
            "created_from": self.created_from,
            "note": self.note,
        }


@dataclass(frozen=True)
class ResponseBatch:
    """一次导入的数值回答（长表）。

    ``values`` 为 ``{指标: {person_id: 值}}``；缺失的人不在内层字典中。
    零值以真实的 0.0 保存，与缺失严格区分。
    """

    batch_id: str
    indicator: str
    values: dict[str, float]
    missing_ids: tuple[str, ...]
    source_sha256: str = ""


@dataclass(frozen=True)
class GroupCell:
    """一个分组单元的发布结果。缺失绝不填零；分母为零时显式无结果。"""

    indicator: str
    group_field: str
    group_value: str
    total_weight: float
    valid_weight: float
    missing_weight: float
    weighted_total: float | None
    mean: float | None
    valid_n: int
    missing_n: int
    note: str
