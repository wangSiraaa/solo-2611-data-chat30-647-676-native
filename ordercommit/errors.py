"""业务异常类型。所有拒绝路径都带明确的错误码与可读信息。"""

from __future__ import annotations


class BusinessError(Exception):
    """全部业务异常的基类，携带稳定错误码。"""

    code = "BUSINESS_ERROR"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class RuleViolation(BusinessError):
    """包装倍数 / 最低量 / 上限 / 阶梯等规则不满足。"""

    code = "RULE_VIOLATION"


class NotFound(BusinessError):
    """引用的报价、承诺、需求或到货不存在。"""

    code = "NOT_FOUND"


class DuplicateRequest(BusinessError):
    """同号异文：request_no 已存在但请求体不同。"""

    code = "DUPLICATE_REQUEST"


class QuoteExpired(BusinessError):
    """在有效窗口之外接受报价；过期接受不产生承诺。"""

    code = "QUOTE_EXPIRED"


class OverCapacity(BusinessError):
    """接受时重新核对剩余额度失败（含并发超卖）。"""

    code = "OVER_CAPACITY"


class StateConflict(BusinessError):
    """对象当前状态不允许该操作（已接受、已取消、已部分发放等）。"""

    code = "STATE_CONFLICT"


class ReceiptAlreadyProcessed(BusinessError):
    """同一 receipt_id 重复送达 / 重复分配。"""

    code = "RECEIPT_ALREADY_PROCESSED"


class InfeasiblePlan(BusinessError):
    """采购计划无可行解，携带逐 SKU 缺口。"""

    code = "INFEASIBLE_PLAN"
