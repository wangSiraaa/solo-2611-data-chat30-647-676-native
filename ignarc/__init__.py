"""ignarc — 独立 .ignore 规则解释服务与可追溯目录归档器。

仅处理调用方显式指定的合成采集根，绝不扫描根之外的用户文件。
"""

from .engine import Decision, RuleEngine
from .rules import Rule
from .paths import FILE, DIR, LINK, InvalidPath
from .archiver import collect, inspect_package, verify_package
from .planner import build_plan

__all__ = [
    "Decision",
    "RuleEngine",
    "Rule",
    "FILE",
    "DIR",
    "LINK",
    "InvalidPath",
    "collect",
    "inspect_package",
    "verify_package",
    "build_plan",
]

__version__ = "1.0.0"
