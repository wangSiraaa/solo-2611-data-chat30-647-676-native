"""离散汇流分析引擎。

求值模型（全部为精确有理算术）
------------------------------
对节点 ``n``、时间步 ``t``：

1. 本地有效量  ``e_n(t) = max(r_n(t) - l_n(t), 0)``（区间外为 0）；
2. 本地水体积  ``v_n(t) = A_n * e_n(t)``；
3. 本地产流    ``local_n(t) = sum_j k_n[j] * v_n(t-j)``（与响应核卷积）；
4. 上游贡献    沿每条父边 ``p -> n``（延迟 d、比例 p）：
               ``contrib = sum_edges p_e * out_p(t-d_e)``；
5. 总出流      ``out_n(t) = local_n(t) + contrib_n(t)``；
6. 离开体积    ``leave_n(t) = out_n(t) * (1 - sum 出边比例)``；
   边流量      ``flow_e(t) = out_from(t) * p_e``。

**上游传来的体积不再乘接收节点面积或响应核**——第 4 步只做延迟与比例。
同一时间步的零延迟边按 DAG 拓扑序即时求值；每一步内按拓扑序扫描即可
保证父节点（含零延迟祖先）先算。

时域
----
请求覆盖 ``[L, R)``，输出不截断于 R，而延伸到全部本地响应尾与最长
路径延迟尾结束：``T = R - 1 + max_kernel_len - 1 + max_path_delay``。

守恒
----
每节点 ``本地体积和 = 离开和 + 出边流量和``；
全局   ``输入体积和（A*max(r-l,0)）= 全网离开和``，尾后无残留。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .catalog import Catalog
from .errors import AnalysisError
from .rainfall import RainBatch
from .rational import frac, frac_text

ZERO = Fraction(0)
ONE = Fraction(1)


@dataclass(frozen=True)
class AnalysisRequest:
    """分析请求：显式节点集合与半开时间区间。"""

    nodes: Tuple[str, ...]
    lo: int
    hi: int

    @classmethod
    def make(cls, nodes: Sequence[str], lo: int, hi: int) -> "AnalysisRequest":
        if not isinstance(lo, int) or not isinstance(hi, int) or lo >= hi:
            raise AnalysisError(f"请求时间区间非法: [{lo}, {hi})")
        if not nodes:
            raise AnalysisError("请求至少指定一个节点")
        return cls(tuple(dict.fromkeys(nodes)), lo, hi)


@dataclass
class NodeReport:
    """单节点逐时间步报告（序列下标 0..T 对应绝对时间 L..L+T）。"""

    node_id: str
    start: int
    local: List[Fraction]
    upstream: List[Fraction]
    outflow: List[Fraction]
    leave: List[Fraction]
    edge_flows: Dict[Tuple[str, str, int], List[Fraction]]
    local_volume: List[Fraction]
    peak_outflow: Fraction
    peak_steps: List[int]
    local_volume_total: Fraction
    leave_total: Fraction
    edge_flow_totals: Dict[Tuple[str, str, int], Fraction]

    def at(self, name: str, t: int) -> Fraction:
        idx = t - self.start
        if idx < 0 or idx >= len(self.outflow):
            raise AnalysisError(f"时间 {t} 超出报告时域 [{self.start}, {self.start + len(self.outflow)})")
        table = {
            "local": self.local,
            "upstream": self.upstream,
            "outflow": self.outflow,
            "leave": self.leave,
            "local_volume": self.local_volume,
        }
        if name in table:
            return table[name][idx]
        for key, seq in self.edge_flows.items():
            if f"edge:{key[0]}->{key[1]}:{key[2]}" == name:
                return seq[idx]
        raise AnalysisError(f"未知报告字段 {name!r}")


@dataclass
class AnalysisReport:
    """完整分析报告（冻结身份，绑定目录与降雨两个哈希）。"""

    analysis_id: str
    catalog_hash: str
    rain_hash: str
    lo: int
    hi: int
    horizon_end: int
    requested_nodes: Tuple[str, ...]
    closure_nodes: Tuple[str, ...]
    node_reports: Dict[str, NodeReport]
    input_volume_total: Fraction
    leave_volume_total: Fraction
    residual: Fraction
    conserved: bool
    incomplete: bool = False
    missing_cells: List[Tuple[str, int]] = field(default_factory=list)
    status: str = "OK"

    # ---- 序列化为可冻结 JSON（有理量全部转文本） -----------------------
    def to_payload(self) -> dict:
        def edge_key(k: Tuple[str, str, int]) -> str:
            return f"{k[0]}->{k[1]}:d{k[2]}"

        nodes_out = {}
        for nid, rep in self.node_reports.items():
            nodes_out[nid] = {
                "start": rep.start,
                "local": [frac_text(x) for x in rep.local],
                "upstream": [frac_text(x) for x in rep.upstream],
                "outflow": [frac_text(x) for x in rep.outflow],
                "leave": [frac_text(x) for x in rep.leave],
                "local_volume": [frac_text(x) for x in rep.local_volume],
                "edge_flows": {
                    edge_key(k): [frac_text(x) for x in seq]
                    for k, seq in rep.edge_flows.items()
                },
                "peak_outflow": frac_text(rep.peak_outflow),
                "peak_steps": rep.peak_steps,
                "totals": {
                    "local_volume": frac_text(rep.local_volume_total),
                    "leave": frac_text(rep.leave_total),
                    "edge_flows": {
                        edge_key(k): frac_text(v)
                        for k, v in rep.edge_flow_totals.items()
                    },
                },
            }
        return {
            "format": "hydro-analysis/1",
            "analysis_id": self.analysis_id,
            "catalog_hash": self.catalog_hash,
            "rain_hash": self.rain_hash,
            "window": [self.lo, self.hi],
            "horizon_end": self.horizon_end,
            "requested_nodes": list(self.requested_nodes),
            "closure_nodes": list(self.closure_nodes),
            "status": self.status,
            "incomplete": self.incomplete,
            "missing_cells": [[n, t] for n, t in self.missing_cells],
            "conservation": {
                "input_volume": frac_text(self.input_volume_total),
                "leave_volume": frac_text(self.leave_volume_total),
                "residual": frac_text(self.residual),
                "conserved": self.conserved,
            },
            "nodes": nodes_out,
        }

    def canonical_text(self) -> str:
        return json.dumps(
            self.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


class Engine:
    """汇流分析引擎；无外部服务、无状态副作用。"""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    # ---- 公共入口 ------------------------------------------------------
    def analyze(
        self,
        request: AnalysisRequest,
        rain: RainBatch,
        store=None,
    ) -> AnalysisReport:
        """执行完整分析；缺格返回 INCOMPLETE 报告且不产生水文曲线。"""
        cat = self.catalog
        if rain.catalog_hash != cat.content_hash:
            raise AnalysisError("降雨批次与目录身份不匹配")
        for nid in request.nodes:
            if not cat.has_node(nid):
                raise AnalysisError(f"请求节点 {nid!r} 不在目录中")

        # 闭包：请求节点 + 全部祖先（其降雨是闭包内出流的真实来源）
        closure_set = set(request.nodes)
        for nid in request.nodes:
            closure_set.update(cat.ancestors(nid))
        closure = tuple(n for n in cat.order if n in closure_set)

        # 缺格检查：未收到 ≠ 零，缺任一必需格即 INCOMPLETE
        missing = [
            (nid, t)
            for nid in closure
            for t in range(request.lo, request.hi)
            if (nid, t) not in rain.cells
        ]
        if missing:
            report = self._incomplete_report(request, rain, closure, missing)
            if store is not None:
                store.save_analysis(
                    report.analysis_id, cat.content_hash, rain.rain_hash,
                    request.lo, request.hi, request.nodes, "INCOMPLETE", None,
                )
            return report

        return self._run(request, rain, closure, store)

    # ---- INCOMPLETE ----------------------------------------------------
    def _incomplete_report(self, request, rain, closure, missing) -> AnalysisReport:
        aid = self._analysis_id(request, rain, incomplete=True)
        return AnalysisReport(
            analysis_id=aid,
            catalog_hash=self.catalog.content_hash,
            rain_hash=rain.rain_hash,
            lo=request.lo,
            hi=request.hi,
            horizon_end=request.hi - 1,
            requested_nodes=request.nodes,
            closure_nodes=closure,
            node_reports={},
            input_volume_total=ZERO,
            leave_volume_total=ZERO,
            residual=ZERO,
            conserved=False,
            incomplete=True,
            missing_cells=sorted(missing),
            status="INCOMPLETE",
        )

    # ---- 实际求值 ------------------------------------------------------
    def _run(self, request: AnalysisRequest, rain: RainBatch, closure: Tuple[str, ...], store) -> AnalysisReport:
        cat = self.catalog
        L, R = request.lo, request.hi

        # 时域终点：核尾 + 最长路径延迟尾，保证所有水团都已离开或到达出口
        max_kernel = max(len(cat.node(n).kernel) for n in cat.node_ids)
        longest_path = 0
        for src in cat.node_ids:
            for dst in cat.node_ids:
                d = cat.longest_delay(src, dst)
                if d is not None:
                    longest_path = max(longest_path, d)
        T = (R - 1) + (max_kernel - 1) + longest_path
        n_steps = T - L + 1

        def zeros() -> List[Fraction]:
            return [ZERO for _ in range(n_steps)]

        local_vol: Dict[str, List[Fraction]] = {n: zeros() for n in cat.node_ids}
        local_run: Dict[str, List[Fraction]] = {n: zeros() for n in cat.node_ids}
        upstream: Dict[str, List[Fraction]] = {n: zeros() for n in cat.node_ids}
        outflow: Dict[str, List[Fraction]] = {n: zeros() for n in cat.node_ids}
        leave: Dict[str, List[Fraction]] = {n: zeros() for n in cat.node_ids}
        edge_flows: Dict[Tuple[str, str, int], List[Fraction]] = {
            (e.from_id, e.to_id, e.delay): zeros() for e in cat.edges
        }

        # 1) 本地有效量 -> 本地体积 -> 与核卷积（仅闭包节点可能有输入）
        for nid in closure:
            node = cat.node(nid)
            kernel = node.kernel
            eff: Dict[int, Fraction] = {}
            for t in range(L, R):
                r, l = rain.cell(nid, t)
                e = r - l
                if e > 0:
                    eff[t] = e
                    local_vol[nid][t - L] = node.area * e
            for tau, e in eff.items():
                v = node.area * e
                for j, k in enumerate(kernel):
                    if k:
                        idx = (tau + j) - L
                        if 0 <= idx < n_steps:
                            local_run[nid][idx] += v * k

        # 2) 逐时间步、按拓扑序汇流；零延迟边在同一步内即时可见
        for idx in range(n_steps):
            t = L + idx
            for nid in cat.order:
                up = ZERO
                for e in cat.in_edges(nid):
                    if e.proportion == 0:
                        continue
                    src_idx = idx - e.delay
                    if src_idx >= 0:
                        up += outflow[e.from_id][src_idx] * e.proportion
                upstream[nid][idx] = up
                total = local_run[nid][idx] + up
                outflow[nid][idx] = total
                leave_frac = cat.leave_fraction(nid)
                if leave_frac:
                    leave[nid][idx] = total * leave_frac
                for e in cat.out_edges(nid):
                    if e.proportion:
                        edge_flows[(e.from_id, e.to_id, e.delay)][idx] = (
                            total * e.proportion
                        )

        # 3) 组装节点报告与峰值（并列峰值返回全部步）
        node_reports: Dict[str, NodeReport] = {}
        input_total = ZERO
        leave_total = ZERO
        for nid in cat.node_ids:
            out_seq = outflow[nid]
            peak = max(out_seq) if out_seq else ZERO
            peak_steps = [L + i for i, v in enumerate(out_seq) if v == peak] if peak > 0 else []
            edge_totals = {
                key: sum(seq, ZERO) for key, seq in edge_flows.items() if key[0] == nid
            }
            rep = NodeReport(
                node_id=nid,
                start=L,
                local=local_run[nid],
                upstream=upstream[nid],
                outflow=out_seq,
                leave=leave[nid],
                edge_flows={k: v for k, v in edge_flows.items() if k[0] == nid},
                local_volume=local_vol[nid],
                peak_outflow=peak,
                peak_steps=peak_steps,
                local_volume_total=sum(local_vol[nid], ZERO),
                leave_total=sum(leave[nid], ZERO),
                edge_flow_totals=edge_totals,
            )
            node_reports[nid] = rep
            input_total += rep.local_volume_total
            leave_total += rep.leave_total

            # 节点级守恒：入 = 本地体积 + 上游来水；出 = 离开 + 各出边流量。
            # 上游来水只做延迟/比例过路，不乘本地面积或核。
            upstream_total = sum(upstream[nid], ZERO)
            routed = rep.leave_total + sum(edge_totals.values(), ZERO)
            expected = rep.local_volume_total + upstream_total
            if routed != expected:
                raise AnalysisError(
                    f"节点 {nid!r} 守恒校验失败: {routed} != {expected}"
                )

        residual = input_total - leave_total
        report = AnalysisReport(
            analysis_id=self._analysis_id(request, rain, incomplete=False),
            catalog_hash=cat.content_hash,
            rain_hash=rain.rain_hash,
            lo=L,
            hi=R,
            horizon_end=T,
            requested_nodes=request.nodes,
            closure_nodes=closure,
            node_reports=node_reports,
            input_volume_total=input_total,
            leave_volume_total=leave_total,
            residual=residual,
            conserved=(residual == 0),
            status="OK",
        )
        if store is not None:
            store.save_analysis(
                report.analysis_id, cat.content_hash, rain.rain_hash,
                L, R, request.nodes, "OK", report.canonical_text(),
            )
        return report

    def _analysis_id(self, request: AnalysisRequest, rain: RainBatch, incomplete: bool) -> str:
        basis = json.dumps(
            {
                "catalog": self.catalog.content_hash,
                "rain": rain.rain_hash,
                "lo": request.lo,
                "hi": request.hi,
                "nodes": list(request.nodes),
                "incomplete": incomplete,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()
