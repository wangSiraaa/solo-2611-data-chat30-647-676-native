"""联副产物生产台账与独立批次成本仓。

模块边界：

* :mod:`ledger_cost.production` —— 生产登记、可用量、谱系与修订/撤销；
  没有成本数据时仍可独立登记与查询物料去向。
* :mod:`ledger_cost.cost` —— 成本仓，消费完整的生产快照，按真实消耗比例
  以精确有理数传递成本，不能从成本表反推产量。

两模块共享 SQLite 持久化（追加/版本化，旧产品不删除），关闭重开仍可查。
"""

from .production import ProductionLedger
from .cost import CostWarehouse

__all__ = ["ProductionLedger", "CostWarehouse"]
