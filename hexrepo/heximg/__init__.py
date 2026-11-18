"""有限 Intel HEX 映像目录与分区差量核对仓。

只处理合成的 Intel HEX 文本文件，不连接、不写入任何硬件。

子模块
------
errors   : 全部自定义异常
records  : Intel HEX 文本记录解析（含行号与原字节范围）
image    : 稀疏映像产品、显式 overlay 层次、范围读取与来源回查
catalog  : 接入/overlay 清单（manifest）
partition: 设备分区目录（不可变、带修订号、禁止重叠）
delta    : 分区差量分析（只消费映像的公开 API）
repo     : 产品与报告的本地持久化仓
"""

__all__ = [
    "errors",
    "records",
    "image",
    "catalog",
    "partition",
    "delta",
    "repo",
]
__version__ = "1.0.0"

MAX_READ_SIZE = 1 << 20  # 范围读取单次上限 1 MiB
ADDR_MAX = (1 << 32) - 1  # 32 位地址空间上界
