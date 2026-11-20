"""合成数据：3 供应商 × 5 SKU × 20 需求，每 SKU 总量 ≤30 件。

刻意构造的性质：
    * SKU-A 全量阶梯：10 件单价 8（80），8 件单价 12（96）——多买反而便宜；
    * SKU-B 包装=3：非 3 倍数的需求无法恰好满足，必须借超买凑整；
    * SKU-C 低价档只在早期版本窗口有效，过期后同数量更贵；
    * SKU-D 总可供小于两门店需求之和，制造短缺争抢；
    * SKU-E 验证多供应商阶梯组合与确定性定序。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .pricing import SkuRule

#: 规则版本有效窗口（ISO 时刻，左闭右开）；early 与 main 首尾相接、不重叠
WINDOW_EARLY = ("2026-11-01T00:00:00", "2026-11-09T00:00:00")
WINDOW_MAIN = ("2026-11-09T00:00:00", "2026-12-01T00:00:00")
WINDOW_LATE = ("2026-12-01T00:00:00", "2026-12-31T00:00:00")

#: 报价/接受时刻
T_EARLY = "2026-11-05T10:00:00"
T_MAIN = "2026-11-15T10:00:00"
T_JUST_AFTER_EARLY = "2026-11-09T09:00:00"  # early 已结束、main 已开始
T_EXPIRED = "2026-12-02T10:00:00"           # main 也已结束

SKUS = ["SKU-A", "SKU-B", "SKU-C", "SKU-D", "SKU-E"]

#: 逐 SKU 超买上限（件）
OVERBUY_CAPS = {"SKU-A": 4, "SKU-B": 4, "SKU-C": 3, "SKU-D": 0, "SKU-E": 2}


@dataclass(frozen=True)
class SyntheticDemand:
    demand_id: str
    store_id: str
    sku: str
    qty: int
    priority: int
    due_date: str


def build_versions() -> dict[str, dict]:
    """返回 {version_id: {supplier_id, window, sku_rules}}。"""
    versions: dict[str, dict] = {}

    # ---------------- S1：通用大供应商，包装 2/5，阶梯平缓
    versions["V-S1-MAIN"] = {
        "supplier_id": "S1",
        "window": WINDOW_MAIN,
        "sku_rules": {
            # 多买反而便宜：8 件=96，10 件=80
            "SKU-A": SkuRule(pack_multiple=2, min_qty=0, capacity=20,
                             tiers=[(0, 12), (10, 8)]),
            "SKU-B": SkuRule(pack_multiple=5, min_qty=0, capacity=20,
                             tiers=[(0, 10), (10, 9)]),
            "SKU-C": SkuRule(pack_multiple=2, min_qty=2, capacity=20,
                             tiers=[(0, 15), (8, 12)]),
            "SKU-D": SkuRule(pack_multiple=2, min_qty=0, capacity=8,
                             tiers=[(0, 20)]),
            "SKU-E": SkuRule(pack_multiple=2, min_qty=0, capacity=16,
                             tiers=[(0, 7), (10, 6)]),
        },
    }

    # ---------------- S2：包装 3 的专业供应商（SKU-B 凑整关键），SKU-D 小量现货
    versions["V-S2-MAIN"] = {
        "supplier_id": "S2",
        "window": WINDOW_MAIN,
        "sku_rules": {
            "SKU-A": SkuRule(pack_multiple=3, min_qty=0, capacity=12,
                             tiers=[(0, 11), (9, 9)]),
            "SKU-B": SkuRule(pack_multiple=3, min_qty=0, capacity=15,
                             tiers=[(0, 11), (9, 8)]),
            "SKU-C": SkuRule(pack_multiple=3, min_qty=3, capacity=12,
                             tiers=[(0, 14)]),
            "SKU-D": SkuRule(pack_multiple=1, min_qty=0, capacity=4,
                             tiers=[(0, 22)]),
            "SKU-E": SkuRule(pack_multiple=3, min_qty=0, capacity=9,
                             tiers=[(0, 8)]),
        },
    }

    # ---------------- S3：SKU-C 早期低价窗口（过期接受案例），其余常态供应
    versions["V-S3-EARLY"] = {
        "supplier_id": "S3",
        "window": WINDOW_EARLY,
        "sku_rules": {
            "SKU-A": SkuRule(pack_multiple=4, min_qty=0, capacity=12,
                             tiers=[(0, 13)]),
            "SKU-B": SkuRule(pack_multiple=4, min_qty=0, capacity=12,
                             tiers=[(0, 12)]),
            "SKU-C": SkuRule(pack_multiple=2, min_qty=2, capacity=18,
                             tiers=[(0, 9), (10, 7)]),  # 早鸟低价
            "SKU-E": SkuRule(pack_multiple=4, min_qty=0, capacity=12,
                             tiers=[(0, 9)]),
        },
    }
    versions["V-S3-MAIN"] = {
        "supplier_id": "S3",
        "window": WINDOW_MAIN,
        "sku_rules": {
            "SKU-A": SkuRule(pack_multiple=4, min_qty=0, capacity=12,
                             tiers=[(0, 13)]),
            "SKU-B": SkuRule(pack_multiple=4, min_qty=0, capacity=12,
                             tiers=[(0, 12)]),
            "SKU-C": SkuRule(pack_multiple=2, min_qty=2, capacity=18,
                             tiers=[(0, 16), (10, 13)]),  # 低价窗口结束，价格回升
            "SKU-D": SkuRule(pack_multiple=2, min_qty=0, capacity=4,
                             tiers=[(0, 25)]),
            "SKU-E": SkuRule(pack_multiple=4, min_qty=0, capacity=12,
                             tiers=[(0, 9)]),
        },
    }
    return versions


def build_demands() -> list[SyntheticDemand]:
    """20 条需求，两个门店；同 SKU 跨门店争抢，优先级/到期日交错。"""
    raw = [
        # (id, store, sku, qty, priority, due)
        ("D01", "STORE-1", "SKU-A", 6,  5, "2026-11-20"),
        ("D02", "STORE-2", "SKU-A", 3,  3, "2026-11-22"),
        ("D03", "STORE-1", "SKU-A", 2,  4, "2026-11-25"),
        # B 总量 8，pack 5 / 3 / 4 下无法恰好满足，必须超买凑整（穷举选最省）
        ("D04", "STORE-1", "SKU-B", 4,  5, "2026-11-21"),
        ("D05", "STORE-2", "SKU-B", 3,  2, "2026-11-23"),
        # C 正常需求
        ("D06", "STORE-1", "SKU-C", 5,  4, "2026-11-24"),
        ("D07", "STORE-2", "SKU-C", 3,  4, "2026-11-26"),
        ("D08", "STORE-1", "SKU-C", 2,  1, "2026-11-28"),
        # D 总需求 14 > 总可供 6+4+4=14 恰好可满足；再由测试人为收紧制造短缺
        ("D09", "STORE-1", "SKU-D", 4,  5, "2026-11-19"),
        ("D10", "STORE-2", "SKU-D", 4,  5, "2026-11-20"),
        ("D11", "STORE-1", "SKU-D", 3,  3, "2026-11-22"),
        ("D12", "STORE-2", "SKU-D", 3,  2, "2026-11-24"),
        # E 普通组合
        ("D13", "STORE-1", "SKU-E", 4,  3, "2026-11-23"),
        ("D14", "STORE-2", "SKU-E", 4,  3, "2026-11-23"),
        ("D15", "STORE-1", "SKU-E", 2,  2, "2026-11-27"),
        # 同优先级/同到期日，用 demand_id 定序的案例
        ("D16", "STORE-1", "SKU-A", 2,  4, "2026-11-25"),
        ("D17", "STORE-2", "SKU-B", 1,  5, "2026-11-21"),
        ("D18", "STORE-1", "SKU-C", 2,  5, "2026-11-18"),
        ("D19", "STORE-2", "SKU-E", 1,  1, "2026-11-29"),
        ("D20", "STORE-1", "SKU-D", 1,  1, "2026-11-30"),
    ]
    return [SyntheticDemand(*row) for row in raw]


def export_json(path: str | Path) -> Path:
    """把合成数据落盘为 JSON，便于独立复跑与审阅。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "versions": {
            vid: {
                "supplier_id": v["supplier_id"],
                "window": v["window"],
                "sku_rules": {
                    sku: {
                        "pack_multiple": r.pack_multiple,
                        "min_qty": r.min_qty,
                        "capacity": r.capacity,
                        "tiers": r.tiers,
                    }
                    for sku, r in v["sku_rules"].items()
                },
            }
            for vid, v in build_versions().items()
        },
        "overbuy_caps": OVERBUY_CAPS,
        "demands": [asdict(d) for d in build_demands()],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
