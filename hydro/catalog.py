"""独立流域响应目录。

目录只描述**河网结构与单位响应**，不接触任何真实降雨：

* :class:`Node`：本地汇水区，正有理面积 ``A`` 与有限非负响应核
  ``k[0..m]``，核之和严格等于 1；
* :class:`Edge`：有向边，非负整数延迟 ``d``、非负流出比例 ``p``；
  同一节点出边比例之和 <= 1，差额表示「离开本模型」；
* :class:`Catalog`：冻结的已发布产品，内容哈希即身份，不可变；
* :class:`CatalogDraft`：可变编辑件，:meth:`CatalogDraft.publish`
  做全量校验（无环、核、比例、限额），失败抛出且不产生任何新版本。

目录可独立回答单位脉冲在各节点何时贡献多少体积（不依赖仓库）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import CatalogError
from .graph import longest_delay_matrix, topo_order
from .rational import frac, frac_text

# 题面限额
MAX_NODES = 30
MAX_KERNEL = 20
MAX_OUT_EDGES = 3


@dataclass(frozen=True)
class Node:
    """本地汇水区（冻结值对象）。"""

    node_id: str
    area: Fraction
    kernel: Tuple[Fraction, ...]

    def validate(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id:
            raise CatalogError("节点 id 必须是非空字符串")
        if not isinstance(self.area, Fraction) or self.area <= 0:
            raise CatalogError(f"节点 {self.node_id!r} 面积必须是正有理数")
        if not self.kernel:
            raise CatalogError(f"节点 {self.node_id!r} 响应核不能为空")
        if len(self.kernel) > MAX_KERNEL:
            raise CatalogError(
                f"节点 {self.node_id!r} 核长 {len(self.kernel)} 超过 {MAX_KERNEL}"
            )
        for j, k in enumerate(self.kernel):
            if not isinstance(k, Fraction) or k < 0:
                raise CatalogError(
                    f"节点 {self.node_id!r} 核项 k[{j}] 必须是非负有理数"
                )
        total = sum(self.kernel, Fraction(0))
        if total != 1:
            raise CatalogError(
                f"节点 {self.node_id!r} 核之和必须等于 1，实际为 {total}"
            )


@dataclass(frozen=True)
class Edge:
    """有向河网边（冻结值对象）。"""

    from_id: str
    to_id: str
    delay: int
    proportion: Fraction

    def validate(self, known: Mapping[str, Node]) -> None:
        if self.from_id not in known:
            raise CatalogError(f"边起点 {self.from_id!r} 不存在")
        if self.to_id not in known:
            raise CatalogError(f"边终点 {self.to_id!r} 不存在")
        if self.from_id == self.to_id:
            raise CatalogError(f"不允许自环边: {self.from_id!r}")
        if not isinstance(self.delay, int) or self.delay < 0:
            raise CatalogError(
                f"边 {self.from_id}->{self.to_id} 延迟必须是非负整数"
            )
        if not isinstance(self.proportion, Fraction) or self.proportion < 0:
            raise CatalogError(
                f"边 {self.from_id}->{self.to_id} 比例必须是非负有理数"
            )


@dataclass(frozen=True)
class Catalog:
    """冻结的已发布目录产品。"""

    nodes: Tuple[Node, ...]
    edges: Tuple[Edge, ...]
    content_hash: str
    node_ids: Tuple[str, ...] = field(init=False)
    order: Tuple[str, ...] = field(init=False)
    _node_map: Dict[str, Node] = field(init=False, repr=False, compare=False)
    _out: Dict[str, Tuple[Edge, ...]] = field(init=False, repr=False, compare=False)
    _in: Dict[str, Tuple[Edge, ...]] = field(init=False, repr=False, compare=False)
    _longest: object = field(init=False, repr=False, compare=False)
    _index: Dict[str, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_ids", tuple(n.node_id for n in self.nodes))
        object.__setattr__(self, "_node_map", {n.node_id: n for n in self.nodes})
        out: Dict[str, List[Edge]] = {n.node_id: [] for n in self.nodes}
        inn: Dict[str, List[Edge]] = {n.node_id: [] for n in self.nodes}
        for e in self.edges:
            out[e.from_id].append(e)
            inn[e.to_id].append(e)
        object.__setattr__(
            self,
            "_out",
            {k: tuple(sorted(v, key=lambda x: (x.to_id, x.delay))) for k, v in out.items()},
        )
        object.__setattr__(self, "_in", {k: tuple(v) for k, v in inn.items()})
        order = tuple(topo_order(self.node_ids, self.edges))
        object.__setattr__(self, "order", order)
        mat, index = longest_delay_matrix(self.node_ids, self._out, order)
        object.__setattr__(self, "_longest", mat)
        object.__setattr__(self, "_index", index)

    # ---- 基本查询 -----------------------------------------------------
    def node(self, node_id: str) -> Node:
        if node_id not in self._node_map:
            raise CatalogError(f"未知节点 {node_id!r}")
        return self._node_map[node_id]

    def has_node(self, node_id: str) -> bool:
        return node_id in self._node_map

    def out_edges(self, node_id: str) -> Tuple[Edge, ...]:
        self.node(node_id)
        return self._out[node_id]

    def in_edges(self, node_id: str) -> Tuple[Edge, ...]:
        self.node(node_id)
        return self._in[node_id]

    def leave_fraction(self, node_id: str) -> Fraction:
        """该节点「离开本模型」的比例 = 1 - 出边比例之和。"""
        s = sum((e.proportion for e in self.out_edges(node_id)), Fraction(0))
        return Fraction(1) - s

    def parents(self, node_id: str) -> Tuple[str, ...]:
        return tuple(e.from_id for e in self.in_edges(node_id))

    def ancestors(self, node_id: str) -> frozenset:
        """节点的全部上游祖先（不含自身）。"""
        seen: set = set()
        stack = [node_id]
        while stack:
            cur = stack.pop()
            for e in self.in_edges(cur):
                if e.from_id not in seen:
                    seen.add(e.from_id)
                    stack.append(e.from_id)
        return frozenset(seen)

    def longest_delay(self, src: str, dst: str) -> Optional[int]:
        """src 到 dst 的最长路径延迟，不可达返回 None（自身为 0）。"""
        val = self._delay_at(self._index[src], self._index[dst])
        return None if val < 0 else val

    def _delay_at(self, i: int, j: int) -> int:
        """最长延迟矩阵取值（NumPy 数组与纯 Python 回退列表统一入口）。"""
        mat = self._longest
        return int(mat[i][j]) if isinstance(mat, list) else int(mat[i, j])

    # ---- 单位脉冲（不依赖降雨仓库） -----------------------------------
    def unit_pulse(self, origin: str, horizon: Optional[int] = None) -> Dict[str, Dict[int, Fraction]]:
        """单位脉冲（1 个体积单位、时间 0 注入 origin 本地）的全网传播。

        返回 ``{node_id: {time: volume}}`` 的稀疏映射：origin 本地先做自身
        响应核卷积，再沿 DAG 按边延迟/比例逐节点拓扑传播。上游传来的体积
        **不再**乘接收节点面积或核。零延迟边因拓扑序在同一步内即时求值。

        ``horizon`` 给定时只保留 ``t <= horizon`` 的项（截断不改变计算）；
        缺省自动覆盖全部核尾与最长延迟尾（所有可达节点体积衰减为零之后）。
        """
        self.node(origin)
        # 稀疏序列：每个节点一个 {t: Fraction}
        series: Dict[str, Dict[int, Fraction]] = {n: {} for n in self.node_ids}
        # 1) origin 本地：单位脉冲与核卷积 = 核本身
        for j, k in enumerate(self.node(origin).kernel):
            if k:
                series[origin][j] = k
        if horizon is None:
            # 自动时域：核尾 + origin 到任一可达节点的最长路径延迟
            tail = len(self.node(origin).kernel) - 1
            oi = self._index[origin]
            longest = max(self._delay_at(oi, j) for j in range(len(self.node_ids)))
            horizon = tail + longest
        # 2) 沿拓扑序传播（origin 之后的节点）
        started = False
        for nid in self.order:
            if nid == origin:
                started = True
            if not started:
                continue
            cur = series[nid]
            for e in self._out[nid]:
                dst = series[e.to_id]
                shift, p = e.delay, e.proportion
                if p == 0:
                    continue
                for t, v in cur.items():
                    tt = t + shift
                    if tt > horizon:
                        continue
                    dst[tt] = dst.get(tt, Fraction(0)) + v * p
        # 3) 去掉零项（比例差量等可能造成的精确零）
        return {
            nid: {t: v for t, v in sorted(seq.items()) if v != 0}
            for nid, seq in series.items()
            if seq and any(v != 0 for v in seq.values())
        }

    # ---- 序列化 / 身份 -------------------------------------------------
    def to_payload(self) -> dict:
        """规范 JSON 可序列化字典（外部同格式产品交换用）。"""
        return {
            "format": "hydro-catalog/1",
            "nodes": [
                {
                    "node_id": n.node_id,
                    "area": frac_text(n.area),
                    "kernel": [frac_text(k) for k in n.kernel],
                }
                for n in sorted(self.nodes, key=lambda x: x.node_id)
            ],
            "edges": [
                {
                    "from_id": e.from_id,
                    "to_id": e.to_id,
                    "delay": e.delay,
                    "proportion": frac_text(e.proportion),
                }
                for e in sorted(
                    self.edges, key=lambda x: (x.from_id, x.to_id, x.delay)
                )
            ],
        }

    def canonical_text(self) -> str:
        """规范文本：键排序、无多余空白，供内容哈希与字节级交换。"""
        return json.dumps(
            self.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def hash_payload(payload: Mapping) -> str:
        text = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def from_payload(cls, payload: Mapping, verify_hash: Optional[str] = None) -> "Catalog":
        """从外部同格式产品重建冻结目录并完整复验。

        传入 ``verify_hash`` 时身份必须匹配，否则拒绝导入。
        """
        if not isinstance(payload, Mapping) or payload.get("format") != "hydro-catalog/1":
            raise CatalogError("非法目录产品：缺少 hydro-catalog/1 格式标识")
        h = cls.hash_payload(payload)
        if verify_hash is not None and h != verify_hash:
            raise CatalogError(
                f"目录身份不匹配: 期望 {verify_hash[:12]}… 实际 {h[:12]}…"
            )
        nodes: List[Node] = []
        for item in payload["nodes"]:
            nodes.append(
                Node(
                    node_id=item["node_id"],
                    area=frac(item["area"]),
                    kernel=tuple(frac(x) for x in item["kernel"]),
                )
            )
        node_map = {n.node_id: n for n in nodes}
        edges = [
            Edge(
                from_id=item["from_id"],
                to_id=item["to_id"],
                delay=int(item["delay"]),
                proportion=frac(item["proportion"]),
            )
            for item in payload["edges"]
        ]
        draft = CatalogDraft()
        for n in nodes:
            draft.add_node(n.node_id, n.area, list(n.kernel))
        for e in edges:
            draft.add_edge(e.from_id, e.to_id, e.delay, e.proportion)
        # 走完整发布校验路径，外部产品同样受无环/核/比例/限额约束
        return draft.publish(expected_hash=h)


class CatalogDraft:
    """可变目录编辑件；发布前不产生任何冻结产品。"""

    def __init__(self) -> None:
        self._nodes: Dict[str, Node] = {}
        # 边以 (from,to,delay) 为键，允许同起讫不同延迟的平行边
        self._edges: Dict[Tuple[str, str, int], Edge] = {}

    # ---- 编辑 ----------------------------------------------------------
    # 说明：编辑期只做结构性防呆（重复 id、键冲突、限额），所有语义校验
    # （核和=1、比例和<=1、无环、非负等）统一在 publish() 前执行，因此
    # 非法内容可以进入草稿，但永远无法冻结为产品，旧产品不受影响。
    def add_node(
        self,
        node_id: str,
        area,
        kernel: Sequence,
    ) -> "CatalogDraft":
        if node_id in self._nodes:
            raise CatalogError(f"节点 {node_id!r} 已存在")
        if len(self._nodes) >= MAX_NODES:
            raise CatalogError(f"节点数超过限额 {MAX_NODES}")
        self._nodes[node_id] = Node(
            node_id, frac(area), tuple(frac(x) for x in kernel)
        )
        return self

    def replace_kernel(self, node_id: str, kernel: Sequence) -> "CatalogDraft":
        """替换某节点核（编辑件操作；发布后旧产品仍绑定旧核）。"""
        if node_id not in self._nodes:
            raise CatalogError(f"节点 {node_id!r} 不存在")
        old = self._nodes[node_id]
        self._nodes[node_id] = Node(node_id, old.area, tuple(frac(x) for x in kernel))
        return self

    def replace_area(self, node_id: str, area) -> "CatalogDraft":
        if node_id not in self._nodes:
            raise CatalogError(f"节点 {node_id!r} 不存在")
        old = self._nodes[node_id]
        self._nodes[node_id] = Node(node_id, frac(area), old.kernel)
        return self

    def add_edge(self, from_id: str, to_id: str, delay: int, proportion) -> "CatalogDraft":
        key = (from_id, to_id, int(delay))
        if key in self._edges:
            raise CatalogError(f"边 {from_id}->{to_id}(d={delay}) 已存在")
        out_count = sum(1 for e in self._edges.values() if e.from_id == from_id)
        if out_count >= MAX_OUT_EDGES:
            raise CatalogError(
                f"节点 {from_id!r} 出边数超过限额 {MAX_OUT_EDGES}"
            )
        self._edges[key] = Edge(from_id, to_id, int(delay), frac(proportion))
        return self

    def remove_edge(self, from_id: str, to_id: str, delay: Optional[int] = None) -> "CatalogDraft":
        keys = [
            k for k in self._edges
            if k[0] == from_id and k[1] == to_id and (delay is None or k[2] == delay)
        ]
        if not keys:
            raise CatalogError(f"待删除边不存在: {from_id}->{to_id}")
        for k in keys:
            del self._edges[k]
        return self

    def remove_node(self, node_id: str) -> "CatalogDraft":
        if node_id not in self._nodes:
            raise CatalogError(f"节点 {node_id!r} 不存在")
        for k in [k for k in self._edges if node_id in (k[0], k[1])]:
            del self._edges[k]
        del self._nodes[node_id]
        return self

    # ---- 全量校验与发布 ------------------------------------------------
    def _validate(self) -> None:
        if not self._nodes:
            raise CatalogError("目录至少需要一个节点")
        for n in self._nodes.values():
            n.validate()
        for e in self._edges.values():
            e.validate(self._nodes)
        # 出边比例之和 <= 1
        sums: Dict[str, Fraction] = {n: Fraction(0) for n in self._nodes}
        counts: Dict[str, int] = {n: 0 for n in self._nodes}
        for e in self._edges.values():
            sums[e.from_id] += e.proportion
            counts[e.from_id] += 1
        for nid, total in sums.items():
            if total > 1:
                raise CatalogError(
                    f"节点 {nid!r} 出边比例之和 {total} 超过 1"
                )
            if counts[nid] > MAX_OUT_EDGES:
                raise CatalogError(
                    f"节点 {nid!r} 出边数 {counts[nid]} 超过 {MAX_OUT_EDGES}"
                )
        # 无环（拓扑排序会抛错）
        topo_order(list(self._nodes), list(self._edges.values()))

    def publish(self, expected_hash: Optional[str] = None) -> Catalog:
        """全量校验并冻结为新 Catalog；任何失败都不产生产品。

        这是一个纯函数式操作：草稿本身不被清空，调用方持久化成功与否
        由存储层负责，旧版本在存储层永远不会被覆盖。
        """
        self._validate()
        cat = Catalog(
            nodes=tuple(sorted(self._nodes.values(), key=lambda x: x.node_id)),
            edges=tuple(
                sorted(
                    self._edges.values(),
                    key=lambda x: (x.from_id, x.to_id, x.delay),
                )
            ),
            content_hash="",
        )
        h = Catalog.hash_payload(cat.to_payload())
        if expected_hash is not None and h != expected_hash:
            raise CatalogError(
                f"发布内容哈希与期望不符: {h[:12]}… != {expected_hash[:12]}…"
            )
        object.__setattr__(cat, "content_hash", h)
        return cat

    # ---- 从已冻结目录开新草稿（核/边修订的工作流） ----------------------
    @classmethod
    def from_catalog(cls, cat: Catalog) -> "CatalogDraft":
        d = cls()
        for n in cat.nodes:
            d.add_node(n.node_id, n.area, list(n.kernel))
        for e in cat.edges:
            d.add_edge(e.from_id, e.to_id, e.delay, e.proportion)
        return d
