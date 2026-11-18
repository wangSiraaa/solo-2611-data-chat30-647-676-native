"""领域异常。所有业务规则冲突均以显式异常表达，不静默吞掉。"""


class InventoryError(Exception):
    """调拨账本规则冲突（超量、非法状态、签收凭据重投/异文等）。"""


class CommitmentError(Exception):
    """承诺仓规则冲突（无可分配真实来源、对未签收份额发货、版本过期等）。"""


class VerificationError(Exception):
    """独立逐事件解释器重放后发现守恒或占额不变量被破坏。"""
