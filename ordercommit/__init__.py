"""供应商报价承诺服务与门店到货分配仓。

两个子系统各自独立持有原始状态：

* :mod:`ordercommit.supplier` —— 供应商报价承诺服务（规则、不可变报价、供货承诺、额度）。
* :mod:`ordercommit.warehouse` —— 门店到货分配仓（需求、到货绑定承诺、优先级分配、中央库存）。

分配仓只消费真实存在的承诺与到货，绝不从报价推导"已收到实物"。
"""

from .errors import (
    BusinessError,
    DuplicateRequest,
    InfeasiblePlan,
    NotFound,
    OverCapacity,
    QuoteExpired,
    ReceiptAlreadyProcessed,
    RuleViolation,
    StateConflict,
)

__all__ = [
    "BusinessError",
    "DuplicateRequest",
    "InfeasiblePlan",
    "NotFound",
    "OverCapacity",
    "QuoteExpired",
    "ReceiptAlreadyProcessed",
    "RuleViolation",
    "StateConflict",
]
