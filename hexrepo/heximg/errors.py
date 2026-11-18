"""全部自定义异常。

所有面向用户的错误都携带足够的定位信息（文件、行号、原字节范围、地址等），
解析器不会静默跳过任何坏行。
"""


class HexError(Exception):
    """本包所有异常的公共基类。"""


class HexParseError(HexError):
    """HEX 文本行级解析错误。

    Attributes
    ----------
    source:
        出错的文件路径（解析入口传入的标识）。
    line_no:
        1 起始行号；文件级（非单行）问题也给出关联行号。
    byte_range:
        该行在文件中的原始字节范围 ``(起始偏移, 结束偏移)``（半开区间），
        与物理文件字节偏移一致，行尾换行不计入。
    """

    def __init__(self, message, source=None, line_no=None, byte_range=None):
        self.source = source
        self.line_no = line_no
        self.byte_range = byte_range
        location = ""
        if source is not None:
            location = f"{source}:"
            if line_no is not None:
                location += f"{line_no}:"
        prefix = f"{location} " if location else ""
        super().__init__(f"{prefix}{message}")


class UnsupportedRecordType(HexParseError):
    """记录类型不在允许集合内（仅 00/01/02/04 受支持）。"""


class AddressOutOfRange(HexError):
    """data 记录落到 0..2^32-1 之外。"""


class OverlayConflict(HexError):
    """同一 overlay 层次内，相同地址被写入不同字节值。

    冲突会拒绝整份产品；``conflicts`` 保留全部冲突点，便于一次性定位。
    """

    def __init__(self, conflicts, layer=None):
        self.conflicts = conflicts
        self.layer = layer
        n = len(conflicts)
        first = conflicts[0] if conflicts else None
        super().__init__(
            f"同层冲突{('（层 ' + str(layer) + '）') if layer is not None else ''}："
            f"共 {n} 处，首处 {first}"
        )


class PartitionError(HexError):
    """分区目录非法（越界、长度非法或分区重叠）。"""


class ReadRangeError(HexError):
    """范围读取参数非法（起始越界、长度非正或超过 1 MiB 上限）。"""


class RepositoryError(HexError):
    """持久化仓读写错误（产品不存在、格式损坏等）。"""


class ViewMismatch(HexError):
    """差量结果跨视图混用（unknown 与 materialized 结果不可合并比较）。"""
