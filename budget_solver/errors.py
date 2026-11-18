"""领域错误类型。整次修订拒绝等场景携带结构化错误明细。"""


class BudgetError(Exception):
    """所有可预期业务错误的基类。"""


class ValidationError(BudgetError):
    """模型 JSON 非法（结构、有理数、项形状等）。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


class ReferenceRejectedError(BudgetError):
    """引用非法或会悬挂，整次修订必须拒绝。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


class CompileError(BudgetError):
    """编译期引用核对失败（理论上快照内不应发生，防御性抛出）。"""


class NotFoundError(BudgetError):
    """模型 / 快照 / 编译产物 / 报告不存在。"""


class VerifyError(BudgetError):
    """独立验证发现产物与目录事实不符。"""
