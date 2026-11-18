"""规则目录：逐规则回答“是否被前序规则完全遮蔽”，并给出可独立复核的证据。

遮蔽判定是集合关系 ``R_i ⊆ R_0 ∪ … ∪ R_{i-1}``，其中 ``R_j`` 是该规则在
五维（源、目的、协议、源端口、目端口）上的完整乘积区域。前序规则可以
**联合**遮蔽后规则，因此不能只找单条包含。

* 完全遮蔽时返回覆盖证据：按最早可覆盖顺序把 ``R_i`` 精确拆成若干
  乘积区域，每片标注来源规则；这些片两两不交且并集恰为 ``R_i``。
* 未完全遮蔽时返回一个合法五元组反例；并附上独立校验（反例确实在 ``R_i``
  内，且不被任何前序规则匹配）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from .rules import RuleSet
from .spatial import (
    Region,
    ResourceLimitError,
    dedupe_regions,
    format_point,
    region_from_rule,
)


@dataclass
class ShadowVerdict:
    rule_id: str
    index: int
    fully_shadowed: bool
    # 覆盖证据：[(rule_id, (Region, ...)), ...]，按前序顺序
    cover: tuple[tuple[str, tuple[Region, ...]], ...] = ()
    # 未覆盖反例（原始五元组）
    counterexample: tuple[int, int, str, int, int] | None = None
    # 未覆盖区域（可能为空，与 fully_shadowed 互斥）
    residual_regions: tuple[Region, ...] = ()
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "rule_id": self.rule_id,
            "index": self.index,
            "fully_shadowed": self.fully_shadowed,
        }
        if self.fully_shadowed:
            out["evidence"] = {
                "kind": "single" if len(self.cover) == 1 else "union",
                "cover": [
                    {"rule_id": rid, "regions": [r.to_dict() for r in regions]}
                    for rid, regions in self.cover
                ],
                "covered_region": None,  # 由调用方按需填充
            }
        else:
            if self.counterexample is not None:
                out["counterexample"] = format_point(self.counterexample)
                out["counterexample_raw"] = list(self.counterexample)
            out["residual"] = [r.to_dict() for r in self.residual_regions]
        if self.notes:
            out["notes"] = self.notes
        return out


class RuleCatalog:
    """不可变规则产品的遮蔽分析目录（不依赖任何流量样本）。"""

    def __init__(self, ruleset: RuleSet, *, max_pieces: int = 100_000) -> None:
        self.ruleset = ruleset
        self.max_pieces = max_pieces
        self._verdicts: dict[str, ShadowVerdict] = {}
        self._regions = {rule.rule_id: region_from_rule(rule) for rule in ruleset.rules}

    # -- 核心 ---------------------------------------------------------
    def analyze(self, rule_id: str) -> ShadowVerdict:
        if rule_id not in self._regions:
            raise KeyError(f"未知 rule_id: {rule_id}")
        if rule_id in self._verdicts:
            return self._verdicts[rule_id]
        index = next(i for i, r in enumerate(self.ruleset.rules) if r.rule_id == rule_id)
        target = self._regions[rule_id]
        remaining: list[Region] = [target]
        cover: list[tuple[str, tuple[Region, ...]]] = []

        for prev in self.ruleset.rules[:index]:
            prev_region = self._regions[prev.rule_id]
            taken: list[Region] = []
            nxt: list[Region] = []
            for piece in remaining:
                inter = piece.intersection(prev_region)
                if inter is not None:
                    taken.append(inter)
                nxt.extend(piece.subtract(prev_region))
                if len(taken) + len(nxt) > self.max_pieces:
                    raise ResourceLimitError(
                        f"遮蔽分析 {rule_id} 超过 max_pieces={self.max_pieces}；"
                        "这是资源拒绝，不代表遮蔽结论"
                    )
            if taken:
                cover.append((prev.rule_id, tuple(dedupe_regions(taken))))
            remaining = dedupe_regions(nxt)
            if not remaining:
                break

        if not remaining:
            verdict = ShadowVerdict(
                rule_id=rule_id,
                index=index,
                fully_shadowed=True,
                cover=tuple(cover),
            )
            verdict.notes.append(
                f"由 {len(cover)} 条前序规则的并集完全遮蔽"
                + ("（单条包含）" if len(cover) == 1 else "（联合覆盖，非单条包含）")
            )
        else:
            witness = min(remaining, key=lambda r: r.key()).witness()
            verdict = ShadowVerdict(
                rule_id=rule_id,
                index=index,
                fully_shadowed=False,
                counterexample=witness,
                residual_regions=tuple(remaining),
            )
            verdict.notes.append(f"存在 {len(remaining)} 个未覆盖区域片段")
        self._verdicts[rule_id] = verdict
        return verdict

    def analyze_all(self) -> list[ShadowVerdict]:
        return [self.analyze(rule.rule_id) for rule in self.ruleset.rules]

    def shadowed_ids(self) -> list[str]:
        return [v.rule_id for v in self.analyze_all() if v.fully_shadowed]

    # -- 独立校验 -----------------------------------------------------
    def verify_verdict(self, verdict: ShadowVerdict) -> dict:
        """独立于分析过程的复核：证据覆盖 / 反例合法性。"""
        rule = next(r for r in self.ruleset.rules if r.rule_id == verdict.rule_id)
        region = region_from_rule(rule)
        prior = [r for r in self.ruleset.rules[: verdict.index]]
        issues: list[str] = []

        if verdict.fully_shadowed:
            pieces: list[Region] = []
            for owner, regions in verdict.cover:
                owner_region = self._regions.get(owner)
                if owner_region is None:
                    issues.append(f"证据来源 {owner} 不是已知规则")
                    continue
                for piece in regions:
                    if piece.intersection(owner_region) is None or not _region_subset(
                        piece, owner_region
                    ):
                        issues.append(f"证据片段不属于来源规则 {owner}")
                    pieces.append(piece)
            # 证据并集 ⊇ R_i 且 ⊆ R_i（每片都同时落在 target 与来源内）
            residual = _subtract_all([region], pieces)
            if residual:
                issues.append("覆盖证据不能覆盖目标规则的完整区域")
            extra = _subtract_all(pieces, [region])
            if extra:
                issues.append("覆盖证据包含目标区域之外的片段")
            # 反例不应存在：尝试用首匹配随机/边界点验证
            for point in _boundary_points(region):
                rule_hit, idx = self.ruleset.first_match(*point)
                if idx >= verdict.index:
                    issues.append(f"声称完全遮蔽但边界点 {point} 未被前序命中")
                    break
        else:
            if verdict.counterexample is None:
                issues.append("未遮蔽但缺少反例")
            else:
                if not region.contains_point(*verdict.counterexample):
                    issues.append("反例不在目标规则区域内")
                rule_hit, idx = self.ruleset.first_match(*verdict.counterexample)
                if idx < verdict.index:
                    issues.append(f"反例被前序规则 {rule_hit.rule_id if rule_hit else idx} 命中")
        return {"verified": not issues, "issues": issues}

    # -- 产品化 -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "ruleset_digest": self.ruleset.digest,
            "ruleset_name": self.ruleset.name,
            "analysis": [self.analyze(r.rule_id).to_dict() for r in self.ruleset.rules],
        }

    def digest(self) -> str:
        body = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _region_subset(a: Region, b: Region) -> bool:
    return not a.subtract(b)


def _subtract_all(owners: Sequence[Region], excluded: Sequence[Region]) -> list[Region]:
    current = list(owners)
    for cut in excluded:
        nxt: list[Region] = []
        for piece in current:
            nxt.extend(piece.subtract(cut))
        current = dedupe_regions(nxt)
        if not current:
            break
    return current


def _boundary_points(region: Region, limit: int = 64) -> list[tuple[int, int, str, int, int]]:
    """区域边界上的确定性代表点，用于对“完全遮蔽”做独立抽查。"""
    return region.sample_points(limit)
