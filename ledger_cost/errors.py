"""领域错误类型。台账与成本仓的所有业务拒绝都抛出这些异常的子类。"""


class LedgerError(Exception):
    """台账/成本仓领域错误基类。"""


class ConstraintViolation(LedgerError):
    """违反生产台账业务约束（守恒、可用量、谱系、规模等）。"""


class NotFound(LedgerError):
    """引用的批次或批号不存在。"""


class AlreadyExists(LedgerError):
    """批次/批号/版本重复登记。"""


class RejectedByConsumption(LedgerError):
    """更正或撤销被下游实际消耗阻止。

    ``blocking`` 列出 ``(lot_id, 修订拟保留克数, 已被下游消耗克数)``，
    只要存在被消耗克数大于拟保留克数的批号，整次修订/撤销即被拒绝，
    台账状态保持原样（不落任何新修订）。
    """

    def __init__(self, message, blocking):
        super().__init__(message)
        self.blocking = list(blocking)


class Unpriceable(LedgerError):
    """某批缺少参考价值且存在非零产出，无法计价。"""


class VersionError(LedgerError):
    """成本输入版本不存在或已存在等版本类错误。"""
