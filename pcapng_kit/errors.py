"""pcapng_kit 的异常类型。

所有异常都携带出错字节偏移，便于目录把损坏点和已恢复扫描的结果
一起报告给调用方。
"""

from __future__ import annotations


class PcapngError(Exception):
    """pcapng_kit 所有异常的共同基类。"""


class PcapngCorruptionError(PcapngError):
    """块框架级损坏：长度未对齐、前后长度不一致、截断等。

    ``offset`` 为出错块的起始文件偏移；目录捕获该异常后会从下一个
    Section Header 的 byte-order magic 重新同步。
    """

    def __init__(self, offset: int, reason: str) -> None:
        super().__init__(f"pcapng corruption at byte {offset}: {reason}")
        self.offset = offset
        self.reason = reason


class PcapngContractError(PcapngError):
    """块框架合法、但违反本题声明的有限合同。

    例如 ``if_tsresol`` / ``if_tsoffset`` 重复出现、时间分辨率指数
    越界、EPB 引用不存在的接口等。目录在当前节内跳过该块并记录。
    """

    def __init__(self, offset: int, reason: str) -> None:
        super().__init__(f"pcapng contract violation at byte {offset}: {reason}")
        self.offset = offset
        self.reason = reason


class BodyBufferLimitError(PcapngError):
    """单次正文读取超过目录允许的缓冲上限（默认 1 MiB）。"""

    def __init__(self, offset: int, requested: int, limit: int) -> None:
        super().__init__(
            f"body read of {requested} bytes at offset {offset} "
            f"exceeds buffer limit {limit}"
        )
        self.offset = offset
        self.requested = requested
        self.limit = limit
