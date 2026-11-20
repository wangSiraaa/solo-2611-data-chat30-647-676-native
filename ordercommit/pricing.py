"""全量阶梯价与采购规则的纯函数实现（不接触数据库，便于穷举与测试）。"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RuleViolation


@dataclass(frozen=True)
class SkuRule:
    """单个 SKU 在一个规则版本下的完整采购规则。

    tiers 为 ``[(lower_bound, unit_price), ...]``，按 lower_bound 升序。
    采用**全量阶梯价**：采购总量落在哪档，全部件数都按该档单价计费。
    """

    pack_multiple: int
    min_qty: int
    capacity: int
    tiers: list[tuple[int, int]]

    def validate_registration(self, sku: str) -> None:
        """登记时自检规则内部一致性。"""
        if self.pack_multiple < 1:
            raise RuleViolation(f"{sku}: 包装倍数必须 >= 1", details={"sku": sku})
        if self.min_qty < 0 or self.capacity < 0:
            raise RuleViolation(f"{sku}: 最低量/上限不能为负", details={"sku": sku})
        if self.min_qty > self.capacity:
            raise RuleViolation(
                f"{sku}: 最低采购量 {self.min_qty} 超过可供上限 {self.capacity}",
                details={"sku": sku},
            )
        if self.min_qty and self.min_qty % self.pack_multiple != 0:
            raise RuleViolation(
                f"{sku}: 最低量 {self.min_qty} 不是包装倍数 {self.pack_multiple} 的整数倍",
                details={"sku": sku},
            )
        if self.capacity % self.pack_multiple != 0:
            raise RuleViolation(
                f"{sku}: 可供上限 {self.capacity} 不是包装倍数 {self.pack_multiple} 的整数倍",
                details={"sku": sku},
            )
        if not self.tiers or self.tiers[0][0] != 0:
            raise RuleViolation(
                f"{sku}: 阶梯必须包含 lower_bound=0 的基准档（零采购不收费用）",
                details={"sku": sku},
            )
        bounds = [b for b, _ in self.tiers]
        if bounds != sorted(set(bounds)):
            raise RuleViolation(f"{sku}: 阶梯下界必须严格升序且唯一", details={"sku": sku})
        for _, price in self.tiers:
            if price < 0:
                raise RuleViolation(f"{sku}: 单价必须为非负整数", details={"sku": sku})

    def applicable_unit_price(self, qty: int) -> int:
        """全量阶梯价：返回 qty 所在档的单价（qty=0 返回 0 档单价，计费另算为 0）。"""
        price = self.tiers[0][1]
        for lower_bound, unit_price in self.tiers:
            if qty >= lower_bound:
                price = unit_price
            else:
                break
        return price

    def check_purchase(self, qty: int) -> None:
        """校验一次正采购是否满足包装、最低量、上限；零采购直接通过且不计费。"""
        if qty == 0:
            return
        if qty < 0:
            raise RuleViolation(f"采购数量不能为负: {qty}", details={"qty": qty})
        if qty < self.min_qty:
            raise RuleViolation(
                f"数量 {qty} 低于最低采购量 {self.min_qty}",
                details={"qty": qty, "min_qty": self.min_qty},
            )
        if qty % self.pack_multiple != 0:
            raise RuleViolation(
                f"数量 {qty} 不是包装倍数 {self.pack_multiple} 的整数倍",
                details={"qty": qty, "pack_multiple": self.pack_multiple},
            )
        if qty > self.capacity:
            raise RuleViolation(
                f"数量 {qty} 超过可供上限 {self.capacity}",
                details={"qty": qty, "capacity": self.capacity},
            )

    def line_price(self, qty: int) -> int:
        """单行计费：零采购不收费用；正采购按所在档全量单价 × 件数。"""
        self.check_purchase(qty)
        if qty == 0:
            return 0
        return qty * self.applicable_unit_price(qty)

    def feasible_quantities(self, limit: int | None = None) -> list[int]:
        """返回该规则下所有合法采购量（含 0），供穷举使用。"""
        upper = self.capacity if limit is None else min(self.capacity, limit)
        qs = [0]
        if self.min_qty == 0:
            # 最低量为 0 时，正采购从一个包装倍数起即可
            q = self.pack_multiple
        else:
            q = self.min_qty
        while q <= upper:
            qs.append(q)
            q += self.pack_multiple
        return qs
