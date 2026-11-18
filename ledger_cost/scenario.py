"""合成生产场景：一次性覆盖题目要求的全部案例。

规模：5 个根批号、11 个生产批（上限 20）、单批最多 3 个产出（上限 5）、
最大深度 4（上限 6）。全部数据为合成规则数据，不模拟任何现实财务准则。

覆盖点：
  B1  一投入两产出 + 部分投入（R1 留存）+ 损耗
  B2/B3/B11 三级再加工链（深度 2/3/4）
  B4  部分投入（L2B 留存 30g）
  B5  参考价值为零 → 分母为零 → 本批及依赖批 B6 不可计价，不填零
  B7  一分钱分摊（池四舍五入=1 分，三路等权重，余数并列按 lot_id 补分）
  B8  下游实际消耗 → 阻止 B1 减量更正
  B9  无下游占用 → 更正成功并触发后代/本批成本重算
  B10 无下游占用 → 撤销成功，投入批号可用量恢复
"""

from __future__ import annotations

from fractions import Fraction

from .cost import CostWarehouse
from .production import ProductionLedger
from .storage import Storage

# --------------------------------------------------------------- 基础台账数据

ROOTS = [
    ("R1", 1000),   # 期初 1000g，成本 10000 分
    ("R2", 400),    # 期初 400g，成本 2000 分（零参考价值分支）
    ("R3", 10),     # 期初 10g，成本 10 分（一分钱案例：投入 3g 后池=3 分）
    ("R4", 200),   # 期初 200g，成本 1600 分（更正案例）
    ("R5", 50),    # 期初 50g，成本 500 分（撤销案例）
]

OPENING_COSTS_V1 = {
    "R1": 10000,
    "R2": 2000,
    "R3": 10,
    "R4": 1600,
    "R5": 500,
}

# (batch_id, input_lot, input_g, [(lot, g)...], loss)
BATCHES_V1 = [
    ("B1", "R1", 600, [("L1A", 400), ("L1B", 180)], 20),
    ("B2", "L1A", 400, [("L2A", 300), ("L2B", 90)], 10),
    ("B3", "L2A", 300, [("L3A", 250)], 50),
    ("B4", "L2B", 60, [("L4A", 55)], 5),
    ("B5", "R2", 100, [("L5A", 100)], 0),
    ("B6", "L5A", 40, [("L6A", 40)], 0),
    ("B7", "R3", 3, [("L7A", 1), ("L7B", 1), ("L7C", 1)], 0),
    # R3 单位成本 1 分/g，投入 3g → 池精确 3 分；为构造“一分钱”案例，
    # 见 demo 中的 cut-c1-cent：把 R3 期初成本改为 1 分（池 0.3 分→四舍五入 1 分）。
    ("B8", "L1B", 100, [("L8A", 90), ("L8B", 8)], 2),
    ("B9", "R4", 100, [("L9A", 60), ("L9B", 30)], 10),
    ("B10", "R5", 20, [("L10A", 18)], 2),
    ("B11", "L3A", 100, [("L11A", 100)], 0),
]

FEES_V1 = {
    "B1": 500,
    "B2": 300,
    "B3": 200,
    "B4": 100,
    "B5": 0,
    "B6": 0,
    "B7": 0,
    "B8": 0,
    "B9": 0,
    "B10": 0,
    "B11": 0,
}

REFS_V1 = {
    "L1A": 10, "L1B": 10,
    "L2A": 12, "L2B": 8,
    "L3A": 20,
    "L4A": 10,
    "L5A": 0,                       # 零参考价值 → B5 分母为零
    "L6A": 30,
    "L7A": 1, "L7B": 1, "L7C": 1,  # 等权重，一分钱案例
    "L8A": 5, "L8B": 5,
    "L9A": 10, "L9B": 10,
    "L10A": 10,
    "L11A": 15,
}

# 价格更正：L2A 参考单位价值 12 → 18（分/克）。B2 是多产出批，价值改版会
# 改变 L2A/L2B 的分摊比例与单位成本，并沿 B3→B11 向未直接改动的后代传导。
REFS_V2 = dict(REFS_V1)
REFS_V2["L2A"] = 18

# 一分钱专用期初成本版本：R3 总成本 10 → 2 分，单位 0.2 分/g，B7 投入 3g 的
# 精确池 0.6 分，统一四舍五入后为 1 分；三路等权 → 仅 lot_id 最前者补到 1 分。
OPENING_COSTS_CENT = dict(OPENING_COSTS_V1)
OPENING_COSTS_CENT["R3"] = 2

# B9 成功更正：L9A 60→55、L9B 30→35，投入与损耗不变（55+35+10=100）
B9_CORRECTION = {
    "outputs": [("L9A", 55), ("L9B", 35)],
}


def build_ledger(storage: Storage) -> ProductionLedger:
    """只建生产台账（用于演示“无成本数据也能独立登记/查询”）。"""
    ledger = ProductionLedger(storage)
    for lot_id, grams in ROOTS:
        ledger.register_root_lot(lot_id, grams)
    for batch_id, in_lot, in_g, outputs, loss in BATCHES_V1:
        ledger.register_batch(batch_id, in_lot, in_g, outputs, loss)
    return ledger


def build_cost_inputs_v1(warehouse: CostWarehouse) -> None:
    for lot_id, cents in OPENING_COSTS_V1.items():
        warehouse.set_opening_cost("root-v1", lot_id, cents)
    # 一分钱案例版本（仅 R3 与 v1 不同）
    for lot_id, cents in OPENING_COSTS_CENT.items():
        warehouse.set_opening_cost("root-cent", lot_id, cents)
    for batch_id, cents in FEES_V1.items():
        warehouse.set_processing_fee("fee-v1", batch_id, cents)
    for lot_id, cents in REFS_V1.items():
        warehouse.set_reference_value("ref-v1", lot_id, cents)


def build_cost_inputs_v2(warehouse: CostWarehouse) -> None:
    # 仅参考价值改版；期初成本与加工费沿用 v1 版本
    for lot_id, cents in REFS_V2.items():
        warehouse.set_reference_value("ref-v2", lot_id, cents)


def open_systems(path: str) -> tuple[Storage, ProductionLedger, CostWarehouse]:
    """从数据库文件重开两个模块（持久化验证用）。"""
    storage = Storage(path)
    ledger = ProductionLedger(storage)
    warehouse = CostWarehouse(storage, ledger)
    return storage, ledger, warehouse


def build_full_system(path: str = ":memory:") -> tuple[Storage, ProductionLedger, CostWarehouse]:
    """端到端构建：台账 + v1 成本输入。"""
    storage = Storage(path)
    ledger = build_ledger(storage)
    warehouse = CostWarehouse(storage, ledger)
    build_cost_inputs_v1(warehouse)
    return storage, ledger, warehouse
