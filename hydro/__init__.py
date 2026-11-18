"""降雨响应核与河网汇流事实仓（精确有理数离散模型）。

子模块
------
rational  : 精确有理数（fractions.Fraction）与文本编解码
catalog   : 独立流域响应目录（DAG 河网、响应核、单位脉冲查询）
storage   : SQLite 事实仓（冻结版本、降雨格、分析档案）
rainfall  : 降雨/损失批次导入、修订、外部产品交换
engine    : 离散汇流分析（卷积、零延迟拓扑求值、守恒对账、峰值）
explain   : 来源分解（哪些节点/输入时间步/路径贡献某时刻出流）
validate  : 交付验证入口与自建小例
"""

from .rational import F, frac, frac_text
from .catalog import Catalog, CatalogDraft, CatalogError
from .rainfall import RainBatch, RainRepo, RainError
from .engine import Engine, AnalysisError, AnalysisReport
from .explain import SourceExplainer, ExplainError
from .storage import Store, StoreError

__all__ = [
    "F",
    "frac",
    "frac_text",
    "Catalog",
    "CatalogDraft",
    "CatalogError",
    "RainBatch",
    "RainRepo",
    "RainError",
    "Engine",
    "AnalysisError",
    "AnalysisReport",
    "SourceExplainer",
    "ExplainError",
    "Store",
    "StoreError",
]
