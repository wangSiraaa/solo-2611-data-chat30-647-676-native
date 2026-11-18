"""整数分票值分摊：最大余数法（本题虚构规则）。

规则：
1. 按正整数权重计算精确比例，先下取整；
2. 剩余的分（total - sum(floors)）依次补给余数最大者；
3. 余数相同（或余数不可用、需要定序时）按 coupon_id 升序定序。

该模块是纯函数，票联原票值分摊与收入仓沿祖先链重算分摊基数共用同一套算法，
保证两侧对同一组 (权重, 总分) 永远得到同一份整数结果。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class AllocItem:
    key: str
    weight: int
    value: int
    exact: Fraction  # 精确比例值（分），仅用于解释与测试


def allocate(total: int, weighted: list[tuple[str, int]]) -> list[AllocItem]:
    """把整数 ``total`` 分按权重分摊给若干 key。

    ``weighted`` 为 ``(key, 正整数权重)``，key 即 coupon_id 定序键。
    返回顺序按 key 升序（与 coupon_id 定序一致）。
    """
    if total < 0:
        raise ValueError("分摊总分不能为负")
    if not weighted:
        raise ValueError("至少需要一个分摊项")
    if any(w <= 0 for _, w in weighted):
        raise ValueError("分摊权重必须为正整数")

    ordered = sorted(weighted, key=lambda kv: kv[0])
    weight_sum = sum(w for _, w in ordered)
    floors: dict[str, int] = {}
    exacts: dict[str, Fraction] = {}
    remainders: dict[str, Fraction] = {}
    for key, weight in ordered:
        exact = Fraction(total * weight, weight_sum)
        exacts[key] = exact
        floors[key] = exact.numerator // exact.denominator
        remainders[key] = exact - floors[key]

    leftover = total - sum(floors.values())
    # 余数降序；余数相同按 key（coupon_id）升序。
    ranked = sorted(ordered, key=lambda kv: (-remainders[kv[0]], kv[0]))
    for key, _ in ranked[:leftover]:
        floors[key] += 1

    return [
        AllocItem(key=key, weight=dict(ordered)[key], value=floors[key], exact=exacts[key])
        for key, _ in ordered
    ]


def allocate_values(total: int, weighted: list[tuple[str, int]]) -> dict[str, int]:
    """便捷版本：返回 {key: 整数票值}。"""
    return {item.key: item.value for item in allocate(total, weighted)}
