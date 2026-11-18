"""明确区分的错误类型，调用方可以据此区分输入问题、不可行与未决。"""

from __future__ import annotations


class SurveyCalError(Exception):
    """本工具所有自定义异常的基类。"""


class InputError(SurveyCalError):
    """输入数据不满足登记规则（非有限值、重复 ID、未知类别等）。"""


class IdentityError(SurveyCalError):
    """回答批次身份校验失败：出现未知或重复的样本 ID。"""


class InfeasibleError(SurveyCalError):
    """数学上判定为不可行（控制矛盾 / 必要单元无样本 / 边界使其不可行）。

    :param reason: 机器可读的不可行原因代码。
    :param diagnostics: 相关约束与诊断依据，绝不静默丢约束或放宽边界。
    """

    def __init__(self, reason: str, message: str, diagnostics: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.diagnostics = diagnostics or {}


class UndecidedError(SurveyCalError):
    """数值求解在预算内未能判定可行性/最优性；这不等于数学不可行。"""

    def __init__(self, message: str, diagnostics: dict | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class VerificationError(SurveyCalError):
    """独立复核未通过（外部权重产品或库内产品容差检查失败）。"""
