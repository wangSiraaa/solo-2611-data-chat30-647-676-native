"""错误类型。

两类错误严格区分：

* ``CompileError``：计划级类型/结构错误，拒绝整个计划，不产生任何输出批次。
* ``DataError``：行级运行时数据错误，只拒绝对应行，其余行仍有明确去向。
"""


class CompileError(Exception):
    """计划无法编译（结构、列引用、类型推导、schema 绑定问题）。"""

    def __init__(self, message, code="compile_error", node_id=None):
        super().__init__(message)
        self.code = code
        self.node_id = node_id


class DataError(Exception):
    """单行数据错误（除零、precision 溢出、输入不可表示）。

    始终携带抛出节点 id，便于错误清单回溯到计划中的具体节点来源。
    """

    def __init__(self, message, code, node_id):
        super().__init__(message)
        self.code = code
        self.node_id = node_id


DIV_BY_ZERO = "div_by_zero"
OVERFLOW = "overflow"
INPUT_ERROR = "input_error"
SCHEMA_MISMATCH = "schema_mismatch"
