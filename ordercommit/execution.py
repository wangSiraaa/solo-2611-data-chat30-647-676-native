"""计划执行编排：穷举得到的采购计划 → 真实报价 → 接受 → 承诺 → 分配仓登记。

关键纪律：
    * 计划只产生候选数量向量；真实总价只取自供应商服务返回的不可变报价与承诺，
      绝不把拆开的单价拼出从未得到的承诺；
    * 报价请求号确定性生成，重跑同文复读；
    * 任一供应商报价被规则拒绝或接受时额度不足，则整体中止并回报，
      已建立的承诺保留原始事实、由调用方决定后续。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .planner import Plan
from .supplier import SupplierService
from .warehouse import Warehouse


@dataclass
class ExecutionResult:
    plan: Plan
    quotes: list[dict[str, Any]]
    commitments: list[dict[str, Any]]
    actual_cost: int
    estimated_cost: int
    cost_matches_estimate: bool
    request_numbers: dict[str, str]


class ProcurementExecutor:
    def __init__(self, supplier_service: SupplierService,
                 warehouse: Warehouse | None = None) -> None:
        self.supplier = supplier_service
        self.warehouse = warehouse

    def execute(self, plan: Plan, plan_ref: str,
                now: str) -> ExecutionResult:
        quotes: list[dict[str, Any]] = []
        commitments: list[dict[str, Any]] = []
        request_numbers: dict[str, str] = {}
        for supplier_id in sorted(plan.supplier_items()):
            items = [it for it in plan.supplier_items()[supplier_id] if it["qty"] > 0]
            if not items:
                continue
            request_no = f"REQ-{plan_ref}-{supplier_id}"
            request_numbers[supplier_id] = request_no
            quote = self.supplier.quote(request_no, supplier_id, items, now)
            quotes.append(quote)
            commitment_no = f"C-{plan_ref}-{supplier_id}"
            if quote["status"] == "ACCEPTED":
                # 同请求号复读：原报价已接受，复用同一承诺（整段执行天然幂等）
                commitment = self.supplier.get_commitment(
                    quote["commitment_no"] or commitment_no)
            else:
                commitment = self.supplier.accept(quote["quote_no"], commitment_no, now)
            commitments.append(commitment)
            if self.warehouse is not None:
                self.warehouse.register_commitment(
                    commitment_no=commitment["commitment_no"],
                    supplier_id=commitment["supplier_id"],
                    version_id=commitment["version_id"],
                    lines=commitment["lines"],
                    now=now,
                )
        actual_cost = sum(c["total_price"] for c in commitments)
        plan.actual_cost = actual_cost
        plan.commitment_nos = [c["commitment_no"] for c in commitments]
        return ExecutionResult(
            plan=plan,
            quotes=quotes,
            commitments=commitments,
            actual_cost=actual_cost,
            estimated_cost=plan.estimated_cost,
            cost_matches_estimate=actual_cost == plan.estimated_cost,
            request_numbers=request_numbers,
        )
