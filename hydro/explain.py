"""来源分解：某时刻某节点的出流来自哪些节点与输入时间步。

模型是线性的，每个本地水团（节点 ``a``、时间 ``tau`` 的本地体积
``V(a,tau) = A_a * max(r-l,0)``）沿 DAG 的**每一条有向路径**独立传播：

* 在起源节点先与响应核卷积，沿路径每过一条边只做延迟移位与比例相乘；
* 到达查询节点 ``n``、查询时刻 ``t`` 的贡献为
  ``V(a,tau) * k_a[t - tau - D(path)] * prod(p_e)``，
  其中 ``D(path)`` 为路径延迟之和；
* 多条路径汇入同一节点时**份额相加**；即使原始降雨格相同，不同真实
  分流路径也分别列示，绝不按降雨 id 去重。

同时提供按 ``(起源节点, 输入时间步)`` 聚合的视图，两视图之和都必须
精确等于该时刻该节点的总出流。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from .catalog import Catalog
from .engine import AnalysisReport
from .errors import ExplainError
from .rainfall import RainBatch

ZERO = Fraction(0)

# 路径枚举保护：DAG 路径理论上指数增长，超过上限拒绝而非静默截断
_PATH_CAP = 200_000


@dataclass(frozen=True)
class PathContribution:
    """单条真实路径上的单个水团贡献。"""

    origin: str
    input_step: int
    path: Tuple[str, ...]
    path_delay: int
    path_proportion: Fraction
    kernel_index: int
    kernel_weight: Fraction
    local_volume: Fraction
    volume: Fraction


@dataclass(frozen=True)
class SourceExplanation:
    """某节点某时刻出流的完整来源解释。"""

    node_id: str
    time: int
    outflow: Fraction
    paths: Tuple[PathContribution, ...]
    by_source: Dict[Tuple[str, int], Fraction]

    def origins(self) -> List[str]:
        return sorted({s[0] for s in self.by_source})

    def verify(self) -> bool:
        """路径和与聚合和都必须精确等于总出流。"""
        if sum((p.volume for p in self.paths), ZERO) != self.outflow:
            return False
        if sum(self.by_source.values(), ZERO) != self.outflow:
            return False
        return True


class SourceExplainer:
    """来源分解器；只依赖冻结目录与降雨批次，目录脉冲查询不经过本类。"""

    def __init__(self, catalog: Catalog, rain: RainBatch) -> None:
        if rain.catalog_hash != catalog.content_hash:
            raise ExplainError("降雨批次与目录身份不匹配")
        self.catalog = catalog
        self.rain = rain
        self._paths_to: Dict[str, Tuple[Tuple[Tuple[str, ...], int, Fraction], ...]] = {}

    # ---- 路径枚举（带记忆） --------------------------------------------
    def _all_paths_to(self, target: str):
        """枚举到 target 的全部路径（含 target 自身空路径）。

        返回 ``[(path_nodes, total_delay, proportion_product), ...]``。
        空路径 ``(target,)`` 延迟 0、比例 1，表示本地来源。
        """
        if target in self._paths_to:
            return self._paths_to[target]

        def build(nid: str):
            if nid in memo:
                return memo[nid]
            result = [((nid,), 0, Fraction(1))]  # 本地空路径
            for e in self.catalog.in_edges(nid):
                for up_path, up_d, up_p in build(e.from_id):
                    result.append(
                        (
                            up_path + (nid,),
                            up_d + e.delay,
                            up_p * e.proportion,
                        )
                    )
            if len(result) > _PATH_CAP:
                raise ExplainError(
                    f"到节点 {nid!r} 的路径数超过保护上限 {_PATH_CAP}"
                )
            memo[nid] = result
            return result

        memo: Dict[str, list] = {}
        paths = tuple(build(target))
        self._paths_to[target] = paths
        return paths

    # ---- 主查询 ---------------------------------------------------------
    def explain(
        self,
        node_id: str,
        time: int,
        lo: Optional[int] = None,
        hi: Optional[int] = None,
    ) -> SourceExplanation:
        """解释 ``node_id`` 在 ``time`` 时刻总出流的全部来源。

        ``lo/hi`` 缺省取降雨批次窗口；只有窗口内的输入格可能产生水团。
        """
        cat = self.catalog
        node = cat.node(node_id)
        if lo is None:
            lo = self.rain.lo
        if hi is None:
            hi = self.rain.hi

        paths = self._all_paths_to(node_id)
        contributions: List[PathContribution] = []
        by_source: Dict[Tuple[str, int], Fraction] = {}

        for path, d_path, p_path in paths:
            origin = path[0]
            if not cat.has_node(origin):  # 防御性
                continue
            kernel = cat.node(origin).kernel
            for tau in range(lo, hi):
                # 该水团在 time 时刻命中的核下标
                j = time - tau - d_path
                if j < 0 or j >= len(kernel) or kernel[j] == 0:
                    continue
                if (origin, tau) not in self.rain.cells:
                    # 窗口内但批次缺格：调用方应先经分析 INCOMPLETE 拦截
                    continue
                r, l = self.rain.cell(origin, tau)
                eff = r - l
                if eff <= 0:
                    continue
                v_local = cat.node(origin).area * eff
                vol = v_local * kernel[j] * p_path
                if vol == 0:
                    continue
                pc = PathContribution(
                    origin=origin,
                    input_step=tau,
                    path=path,
                    path_delay=d_path,
                    path_proportion=p_path,
                    kernel_index=j,
                    kernel_weight=kernel[j],
                    local_volume=v_local,
                    volume=vol,
                )
                contributions.append(pc)
                key = (origin, tau)
                by_source[key] = by_source.get(key, ZERO) + vol

        contributions.sort(key=lambda c: (c.origin, c.input_step, c.path))
        agg = dict(sorted(by_source.items()))
        outflow = sum(agg.values(), ZERO)
        explanation = SourceExplanation(
            node_id=node_id,
            time=time,
            outflow=outflow,
            paths=tuple(contributions),
            by_source=agg,
        )
        if not explanation.verify():
            raise ExplainError("来源分解内部守恒校验失败")
        return explanation

    # ---- 与已完成报告对账 -----------------------------------------------
    def cross_check(self, report: AnalysisReport, times: Optional[List[int]] = None) -> List[Tuple[str, int, Fraction, Fraction]]:
        """把来源分解结果与分析报告的逐时总出流逐格精确核对。

        返回 ``[(node, t, explained, reported), ...]``，两者必须逐项相等。
        """
        if report.incomplete:
            raise ExplainError("INCOMPLETE 报告没有可对账的曲线")
        diffs: List[Tuple[str, int, Fraction, Fraction]] = []
        check_nodes = report.requested_nodes
        for nid in check_nodes:
            rep = report.node_reports[nid]
            ts = times if times is not None else list(range(rep.start, rep.start + len(rep.outflow)))
            for t in ts:
                ex = self.explain(nid, t, report.lo, report.hi)
                reported = rep.at("outflow", t)
                diffs.append((nid, t, ex.outflow, reported))
                if ex.outflow != reported:
                    raise ExplainError(
                        f"对账失败 {nid}@t={t}: 来源和 {ex.outflow} != 报告 {reported}"
                    )
        return diffs
