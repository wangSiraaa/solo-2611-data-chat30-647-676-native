"""可保存的十进制表达式编译器与批量执行器。

数值路径只允许 decimal.Decimal 与整数/Fraction，禁止 binary float。
"""

from .api import compile, run, explain, verify
from .errors import CompileError, DataError
from .planner import Plan, compile_plan
from .types import DecimalType

__all__ = [
    "compile", "run", "explain", "verify",
    "CompileError", "DataError", "Plan", "DecimalType", "compile_plan",
]
