"""netobs：有序双端地址规则产品、精确遮蔽分析、决策单元编译与流量事实仓。"""

from .rules import (
    Prefix,
    Rule,
    RuleSet,
    RuleValidationError,
    format_ipv4,
    load_ruleset,
    parse_ipv4,
    reorder,
    save_ruleset,
)
from .spatial import Region, ResourceLimitError
from .compile import (
    Cell,
    CompiledRules,
    CompilationResourceError,
    compile_rules,
    equivalence_report,
)
from .shadow import RuleCatalog, ShadowVerdict
from .index import RuleIndex
from .traffic import (
    Analysis,
    Fact,
    Packet,
    TrafficValidationError,
    TrafficWarehouse,
    WindowSummary,
    read_packets,
    save_analysis,
    write_packets,
)

__all__ = [
    "Prefix",
    "Rule",
    "RuleSet",
    "RuleValidationError",
    "format_ipv4",
    "parse_ipv4",
    "load_ruleset",
    "reorder",
    "save_ruleset",
    "Region",
    "ResourceLimitError",
    "Cell",
    "CompiledRules",
    "CompilationResourceError",
    "compile_rules",
    "equivalence_report",
    "RuleCatalog",
    "ShadowVerdict",
    "RuleIndex",
    "Analysis",
    "Fact",
    "Packet",
    "TrafficValidationError",
    "TrafficWarehouse",
    "WindowSummary",
    "read_packets",
    "save_analysis",
    "write_packets",
]
