"""全局约束与固定前缀。

两个模块共享一个 SQLite 文件，但表名前缀隔离：
- led_*  ：调拨账本（库存唯一事实源）
- cmt_*  ：交付承诺仓（只持有“来源占额”，不复制库存）
"""

# 题目给定的规模上限（超出即拒绝，防止系统被当作通用 WMS 扩散）
MAX_WAREHOUSES = 4
MAX_SKUS = 5
MAX_TRANSFERS = 20
MAX_ORDERS = 30

# 各类产品的固定前缀（“每次变更取得固定前缀产品”）
PREFIX_LEDGER_EVENT = "LEV"     # 账本事件
PREFIX_TRANSFER = "TRF"         # 调拨单
PREFIX_RECEIPT = "RCP"          # 签收凭据
PREFIX_COMMIT_EVENT = "CEV"     # 承诺仓事件
PREFIX_ORDER = "ORD"            # 客户订单
PREFIX_PORTION = "PTN"          # 逐份来源占额
