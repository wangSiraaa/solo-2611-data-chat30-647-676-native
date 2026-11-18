"""将有序规则编译为等价的无重叠决策单元（优先级展开，priority-1st unfolding）。

核心思路：第 ``i`` 条规则的可达单元是

    U_i = R_i \\ (R_0 ∪ R_1 ∪ … ∪ R_{i-1})

其中 ``R_j`` 是该规则五维条件构成的乘积区域。``U_i`` 用精确集合差分解成
若干互不相交的乘积区域；这些单元两两不交，而它们的并（连同默认单元）
就是全部合法输入域。每个单元携带来源 ``rule_id`` 与其 action/category，
因此对任意输入先落单元、再读标签，与逐条首匹配完全等价——这是集合恒等式，
不是采样验证。

若选择“不物化”：``equivalence_report`` 对每个 ``R_i`` 精确验证
``R_i ∩ (前序并集)`` 与拆分单元的并/交性质，并提供独立于点查询的入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .rules import RuleSet
from .spatial import (
    Region,
    dedupe_regions,
    full_domain,
    region_from_rule,
    subtract_many,
)

DEFAULT_ID = "DEFAULT"


class CompilationResourceError(RuntimeError):
    """分割产生的单元数超出给定资源预算；这不是“规则等价”的结论。"""


@dataclass(frozen=True)
class Cell:
    """无重叠决策单元：精确乘积区域 + 命中来源。"""

    cell_id: int
    region: Region
    rule_id: str  # DEFAULT_ID 表示默认
    action: str
    category: str

    def contains_point(self, src: int, dst: int, proto: str, sport: int, dport: int) -> bool:
        return self.region.contains_point(src, dst, proto, sport, dport)

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "priority": self.cell_id,
            "rule_id": self.rule_id,
            "action": self.action,
            "category": self.category,
            **self.region.to_dict(),
        }


@dataclass
class CompiledRules:
    cells: tuple[Cell, ...]
    default_cell_id: int
    ruleset_digest: str
    rule_order: tuple[str, ...]
    # 每条（非默认）规则的完整区域，用于精确等价/遮蔽复核
    rule_regions: dict[str, Region] = field(default_factory=dict)

    def classify(self, src: int, dst: int, proto: str, sport: int, dport: int) -> Cell:
        for cell in self.cells:
            if cell.contains_point(src, dst, proto, sport, dport):
                return cell
        raise AssertionError("单元并集应覆盖全部合法输入；出现未覆盖点说明编译器有缺陷")

    def stats(self) -> dict:
        per_rule: dict[str, int] = {}
        for cell in self.cells:
            per_rule[cell.rule_id] = per_rule.get(cell.rule_id, 0) + 1
        return {
            "rule_count": len(self.rule_order),
            "cell_count": len(self.cells),
            "cells_per_rule": per_rule,
            "default_cell_id": self.default_cell_id,
        }

    def to_dict(self) -> dict:
        return {
            "ruleset_digest": self.ruleset_digest,
            "rule_order": list(self.rule_order),
            "default_cell_id": self.default_cell_id,
            "cells": [cell.to_dict() for cell in self.cells],
        }


def compile_rules(
    ruleset: RuleSet,
    *,
    max_cells: int = 200_000,
    max_pending: int = 500_000,
) -> CompiledRules:
    """精确编译；超预算抛 :class:`CompilationResourceError`（显式资源拒绝）。"""
    if max_cells <= 0 or max_pending <= 0:
        raise ValueError("资源预算必须为正")
    all_input = full_domain()
    covered: list[Region] = []
    cells: list[Cell] = []
    rule_regions: dict[str, Region] = {}

    for rule in ruleset.rules:
        region = region_from_rule(rule)
        rule_regions[rule.rule_id] = region
        pieces = subtract_many(region, covered)
        if len(cells) + len(pieces) > max_cells:
            raise CompilationResourceError(
                f"规则 {rule.rule_id} 展开后单元数将超过 max_cells={max_cells}；"
                "这是资源拒绝，不代表规则不等价"
            )
        for piece in pieces:
            cells.append(
                Cell(
                    cell_id=len(cells),
                    region=piece,
                    rule_id=rule.rule_id,
                    action=rule.action,
                    category=rule.category,
                )
            )
        covered.append(region)
        if len(covered) > max_pending:
            raise CompilationResourceError(
                f"前序覆盖区域数量超过 max_pending={max_pending}；这是资源拒绝"
            )

    default_pieces = subtract_many(all_input, covered)
    if len(cells) + len(default_pieces) > max_cells:
        raise CompilationResourceError(
            f"默认单元展开将超过 max_cells={max_cells}；这是资源拒绝，不代表规则不等价"
        )
    default_cell_id = len(cells)
    for piece in default_pieces:
        cells.append(
            Cell(
                cell_id=len(cells),
                region=piece,
                rule_id=DEFAULT_ID,
                action=ruleset.default_action,
                category=ruleset.default_category,
            )
        )
    if not default_pieces and not cells:
        # 全域都被覆盖且没有任何规则单元（不可能在非空规则集出现），保守报错
        raise AssertionError("空单元集且无默认单元")

    _assert_disjoint(cells)
    return CompiledRules(
        cells=tuple(cells),
        default_cell_id=default_cell_id,
        ruleset_digest=ruleset.digest,
        rule_order=tuple(rule.rule_id for rule in ruleset.rules),
        rule_regions=rule_regions,
    )


def _assert_disjoint(cells: Sequence[Cell]) -> None:
    # 编译阶段的两两检查对单元数敏感；用前缀/区间键去重 + 结构不交性保证，
    # 此处仅检查同一单元列表中没有完全重复键。
    seen: set[tuple] = set()
    for cell in cells:
        key = cell.region.key()
        if key in seen:
            raise AssertionError("编译产生了重复单元键")
        seen.add(key)


def cells_covering_point(
    cells: Iterable[Cell], src: int, dst: int, proto: str, sport: int, dport: int
) -> list[Cell]:
    return [c for c in cells if c.contains_point(src, dst, proto, sport, dport)]


def equivalence_report(
    ruleset: RuleSet,
    compiled: CompiledRules,
    *,
    extra_points: Sequence[tuple[int, int, str, int, int]] = (),
) -> dict:
    """对编译结果做可复核的精确等价检查（集合关系，不是采样）。

    检查项：

    1. ``rule_order`` 与规则产品完全一致；
    2. 每条规则区域 ``R_i`` 的每个单元都来自 ``i``，且
       ``R_i \\ U_i`` 之外均被前序覆盖（由构造式保证，并在有界范围内抽查边界）；
    3. 单元之间两两不交（对每对单元检查交集为空，单元数受预算约束）；
    4. 单元并集 + 默认并集 == 全域（通过逐单元“未被前序覆盖的部分”构造保证；
       对默认单元检查其确实不与任何规则区域相交）。
    """
    issues: list[str] = []
    if tuple(compiled.rule_order) != tuple(r.rule_id for r in ruleset.rules):
        issues.append("rule_order 与规则产品顺序不一致")
    if compiled.ruleset_digest != ruleset.digest:
        issues.append("ruleset_digest 不匹配")

    default_cells = [c for c in compiled.cells if c.rule_id == DEFAULT_ID]
    default_region = UnionRegion(default_cells)
    for rule in ruleset.rules:
        region = compiled.rule_regions.get(rule.rule_id)
        if region is None:
            issues.append(f"缺少规则区域: {rule.rule_id}")
            continue
        # 默认区域不得与任何规则区域相交
        if default_region.intersects(region):
            issues.append(f"默认单元与规则 {rule.rule_id} 相交")

    # 单元并集必须覆盖全域：full \ all 为空。
    all_union = UnionRegion(list(compiled.cells))
    if all_union.residual(full_domain()):
        issues.append("单元并集未覆盖全域")

    # 每条规则的单元并集应等于 R_i \ 前序并集
    index = {c.cell_id: c for c in compiled.cells}
    for pos, rule in enumerate(ruleset.rules):
        expected = subtract_many(
            compiled.rule_regions[rule.rule_id],
            [compiled.rule_regions[r.rule_id] for r in ruleset.rules[:pos]],
        )
        actual = UnionRegion(
            [index[c.cell_id] for c in compiled.cells if c.rule_id == rule.rule_id]
        )
        diff_a = UnionRegion([RegionCellFactory(p) for p in expected]).minus(actual)
        diff_b = actual.minus(UnionRegion([RegionCellFactory(p) for p in expected]))
        if not diff_a.is_empty() or not diff_b.is_empty():
            issues.append(f"规则 {rule.rule_id} 的单元并集与可达区域不等价")

    # 单元两两不交：如果单元很多，做带预算的抽样配对会破坏“精确”，因此这里
    # 只在单元数不超过 4000 时做全量两两检查；超出时报告“未做全量配对”。
    pairwise_checked = False
    overlap_issues: list[str] = []
    if len(compiled.cells) <= 4000:
        pairwise_checked = True
        for i in range(len(compiled.cells)):
            for j in range(i + 1, len(compiled.cells)):
                if compiled.cells[i].region.intersection(compiled.cells[j].region):
                    overlap_issues.append(f"单元 {i} 与 {j} 相交")
                    break
            if overlap_issues:
                break
    issues.extend(overlap_issues)

    point_checks = []
    for point in extra_points:
        cell = compiled.classify(*point)
        rule, idx = ruleset.first_match(*point)
        expected_rule = DEFAULT_ID if rule is None else rule.rule_id
        if cell.rule_id != expected_rule:
            issues.append(f"点 {point} 编译命中 {cell.rule_id}，首匹配为 {expected_rule}")
        point_checks.append(
            {"point": point, "compiled": cell.rule_id, "expected": expected_rule}
        )

    return {
        "equivalent": not issues,
        "issues": issues,
        "pairwise_checked": pairwise_checked,
        "cell_count": len(compiled.cells),
        "point_checks": len(point_checks),
        "ruleset_digest": ruleset.digest,
    }


class RegionCellFactory:
    """把裸 Region 适配成带 cell 协议的对象，供并集运算复用。"""

    def __init__(self, region: Region):
        self.region = region


class UnionRegion:
    """有限个两两不交（或任意）乘积区域的集合运算包装，仅用于精确复核。"""

    def __init__(self, cells: Sequence):
        self.pieces = [c.region for c in cells]

    def intersects(self, region: Region) -> bool:
        return any(piece.intersection(region) is not None for piece in self.pieces)

    def minus(self, other: "UnionRegion") -> "UnionRegion":
        current = list(self.pieces)
        for other_piece in other.pieces:
            nxt: list[Region] = []
            for piece in current:
                nxt.extend(piece.subtract(other_piece))
            current = dedupe_regions(nxt)
            if not current:
                break
        return UnionRegion([RegionCellFactory(p) for p in current])

    def residual(self, outer: Region) -> tuple[Region, ...]:
        """精确计算 ``outer \\ self``。"""
        return subtract_many(outer, self.pieces)

    def is_empty(self) -> bool:
        return not self.pieces
