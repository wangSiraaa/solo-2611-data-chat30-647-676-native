"""缠绕相位约束目录与位移范围事实仓。

两个互不耦合的部件：

* :mod:`phaseware.catalog` —— 独立相位约束目录（生产侧，SQLite 编辑表 + 不可变产品）；
* :mod:`phaseware.repository` —— 位移分析仓（消费侧，只吃公开产品，精确整数/有理分析）。
"""

__all__ = ["catalog", "exact", "repository"]
