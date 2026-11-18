"""不可变数据模型（台账视图与成本产品）。

克数一律 ``int``；成本精确值一律 :class:`fractions.Fraction`，
发布金额一律整数分 ``int``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional


# ---------------------------------------------------------------- 生产台账视图


@dataclass(frozen=True)
class LotView:
    """一个批号在某一发布切面下的状态视图。"""

    lot_id: str
    produced_grams: int          # 最近一次有效修订中的产出克数（根批号为期初量）
    consumed_grams: int          # 被下游生产实际投入的累计克数
    is_root: bool
    origin_batch: Optional[str]  # 产出该批号的生产批；根批号为 None
    cancelled: bool = False      # 来源生产批已撤销（视图中视为不可再用）

    @property
    def available_grams(self) -> int:
        """可继续投入的整数克（已撤销批的产出不可再用）。"""
        if self.cancelled:
            return 0
        return self.produced_grams - self.consumed_grams


@dataclass(frozen=True)
class OutputView:
    """一条产出记录（批号 + 整数克）。"""

    lot_id: str
    grams: int


@dataclass(frozen=True)
class BatchView:
    """一个生产批在某一发布切面下的完整视图。

    只暴露最新有效修订；历史修订通过 :meth:`ProductionLedger.list_revisions` 查询。
    """

    batch_id: str
    revision_no: int
    input_lot_id: str
    input_grams: int
    outputs: tuple[OutputView, ...]
    loss_grams: int
    depth: int                   # 根原料=0，直接消费根的批=1，逐级递增
    cancelled: bool
    superseded_by: Optional[int]

    @property
    def output_lot_ids(self) -> tuple[str, ...]:
        return tuple(o.lot_id for o in self.outputs)

    @property
    def output_total_grams(self) -> int:
        return sum(o.grams for o in self.outputs)


@dataclass(frozen=True)
class Snapshot:
    """成本仓消费的只读生产切面。

    成本侧只能通过该对象读取真实产量，不能从中写回，也不存在
    任何“从成本表造产量”的路径。
    """

    batches: dict[str, BatchView]
    lots: dict[str, LotView]
    roots: tuple[str, ...]
    max_depth: int

    def topological_batches(self) -> list[BatchView]:
        """按投入依赖返回拓扑序（被依赖批在前）。"""
        ordered: list[BatchView] = []
        visited: set[str] = set()

        def visit(bid: str) -> None:
            if bid in visited:
                return
            b = self.batches[bid]
            if b.input_lot_id in self.lots:
                parent = self.lots[b.input_lot_id].origin_batch
                if parent is not None:
                    visit(parent)
            visited.add(bid)
            ordered.append(b)

        for bid in sorted(self.batches):
            visit(bid)
        return ordered


# ---------------------------------------------------------------- 成本产品


@dataclass(frozen=True)
class CostProduct:
    """成本仓中的一个批号产品（根留存/生产产出均使用该结构）。"""

    lot_id: str
    exact_total_cost: Optional[Fraction]   # 精确有理数总成本，单位：分
    published_total_cost: Optional[int]    # 发布整数分总成本
    exact_unit_cost: Optional[Fraction]    # 精确单位成本（分/克）
    reference_unit_value: Fraction         # 计价所用参考单位价值
    grams: int
    priceable: bool
    reason: Optional[str] = None           # 不可计价原因（可计价为 None）
    origin_batch: Optional[str] = None
    cut_id: Optional[str] = None           # 所属成本切面

    def describe(self) -> str:
        if not self.priceable:
            return f"{self.lot_id}: 不可计价（{self.reason}）"
        return (
            f"{self.lot_id}: {self.grams}g, "
            f"精确成本={self.exact_total_cost} 分, "
            f"发布={self.published_total_cost} 分, "
            f"精确单位={self.exact_unit_cost} 分/g"
        )


@dataclass(frozen=True)
class BatchCostView:
    """一个生产批在某一切面下的成本分摊结果。"""

    batch_id: str
    cut_id: str
    input_lot_id: str
    input_grams: int
    input_exact_cost: Optional[Fraction]
    processing_fee: Fraction
    pool_exact: Optional[Fraction]         # 待分配精确池
    pool_published: Optional[int]          # 统一四舍五入后的整数分池
    weight_total: Fraction                 # 分母 Σ 产量×参考单位价值
    priceable: bool
    reason: Optional[str]
    products: tuple[CostProduct, ...] = field(default_factory=tuple)
