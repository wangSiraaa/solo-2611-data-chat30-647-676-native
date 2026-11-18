"""事实仓领域错误层级。"""

from __future__ import annotations


class HydroError(Exception):
    """所有本包领域错误的基类。"""


class CatalogError(HydroError):
    """目录编辑/发布/校验错误（发布失败时旧产品保持不变）。"""


class StoreError(HydroError):
    """SQLite 事实仓读写/身份错误。"""


class RainError(HydroError):
    """降雨批次导入/修订错误。"""


class AnalysisError(HydroError):
    """分析请求错误（含 INCOMPLETE 缺格）。"""


class ExplainError(HydroError):
    """来源分解查询错误。"""
