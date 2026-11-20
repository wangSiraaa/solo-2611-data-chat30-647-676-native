"""精确整数约束求解器（无浮点）。

实现 AC-3 弧一致性 + MRV 回溯，所有运算为整数。
同时提供独立的全空间枚举 :func:`brute_force_enumerate`，在小图上用于交叉核对
主求解器的可行性、节点极值与唯一解判断。

约束分为两类：

* 二元约束：两个节点之间的允许差值集合 ``x_v - x_u in allowed``；
* 一元约束：单节点允许值集合（用于全局相位界、锚点、极值试探）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# 约束数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BinaryConstraint:
    """二元约束：x_v - x_u ∈ allowed（allowed 为有限整数集合）。"""

    cid: str
    u: str
    v: str
    allowed: frozenset[int]

    def scope(self) -> tuple[str, str]:
        return self.u, self.v

    def other(self, node: str) -> str:
        if node == self.u:
            return self.v
        if node == self.v:
            return self.u
        raise KeyError(node)

    def holds(self, xu: int, xv: int) -> bool:
        return xv - xu in self.allowed


@dataclass(frozen=True)
class UnaryConstraint:
    """一元约束：x_node ∈ allowed。"""

    cid: str
    node: str
    allowed: frozenset[int]

    def holds(self, x: int) -> bool:
        return x in self.allowed


@dataclass
class SolveResult:
    """一次求解的完整结果。

    ``solutions`` 按节点 ID 字典序升序返回（回溯本身按该顺序取值）。
    当解的数量超过调用方给出的上限时，``truncated`` 为 True。
    """

    feasible: bool
    solutions: list[dict[str, int]]
    truncated: bool
    revisions: list[tuple[str, str, str, tuple[int, ...]]] = field(default_factory=list)
    """AC-3 修订记录 (约束id, 被修订节点, 邻节点, 删除的值)，可重放。"""
    wipeout: Optional[tuple[str, tuple[int, ...]]] = None
    """导致空域的节点及其被清空时的域。"""


# ---------------------------------------------------------------------------
# 求解器
# ---------------------------------------------------------------------------


class Solver:
    """固定节点集合上的有限域 CSP 求解器。"""

    def __init__(
        self,
        nodes: Iterable[str],
        binary: Iterable[BinaryConstraint],
        unary: Optional[dict[str, list[UnaryConstraint]]] = None,
        domains: Optional[dict[str, tuple[int, int]]] = None,
    ) -> None:
        self.nodes: list[str] = list(nodes)
        self.binary: list[BinaryConstraint] = list(binary)
        self.unary: dict[str, list[UnaryConstraint]] = {n: [] for n in self.nodes}
        for uc in (unary or {}).values():
            for c in uc:
                if c.node not in self.unary:
                    raise ValueError(f"一元约束引用未知节点 {c.node!r}")
                self.unary[c.node].append(c)

        # 邻接表：node -> [(constraint, neighbor)]；自环只登记一次。
        self._adj: dict[str, list[BinaryConstraint, str]] = {n: [] for n in self.nodes}
        for c in self.binary:
            if c.u not in self._adj or c.v not in self._adj:
                raise ValueError(f"二元约束 {c.cid} 引用未知节点")
            self._adj[c.u].append((c, c.v))
            if c.u != c.v:
                self._adj[c.v].append((c, c.u))

        if domains is None:
            raise ValueError("必须显式提供节点整数域 [lo, hi]")
        self.domains: dict[str, set[int]] = {
            n: set(range(domains[n][0], domains[n][1] + 1)) for n in self.nodes
        }
        if any(len(self.domains[n]) == 0 for n in self.nodes):
            raise ValueError("初始域为空")
        self.wipeout: Optional[tuple[str, tuple[int, ...]]] = None

    # -- AC-3 ---------------------------------------------------------------

    def _revise(
        self,
        dom: dict[str, set[int]],
        constraint: BinaryConstraint,
        revised_node: str,
        neighbor: str,
        revisions: list[tuple[str, str, str, tuple[int, ...]]],
        record: bool,
    ) -> Optional[set[int]]:
        """弧一致性修订；返回被删除的值集合，空域时返回 None。"""
        removed: set[int] = set()
        if constraint.u == constraint.v:
            # 自环：x_u - x_u = 0 必须在允许集合内，否则该节点无支持值。
            support = 0 in constraint.allowed
            if not support:
                removed = set(dom[constraint.u])
        elif revised_node == constraint.u:
            # 删去不存在 y 使 y - x ∈ allowed 的 x
            for x in dom[constraint.u]:
                if not any((y - x) in constraint.allowed for y in dom[constraint.v]):
                    removed.add(x)
        else:
            # 删去不存在 x 使 y - x ∈ allowed 的 y
            for y in dom[constraint.v]:
                if not any((y - x) in constraint.allowed for x in dom[constraint.u]):
                    removed.add(y)
        if removed:
            dom[revised_node] = dom[revised_node] - removed
            if record:
                revisions.append(
                    (constraint.cid, revised_node, neighbor, tuple(sorted(removed)))
                )
            if not dom[revised_node]:
                self.wipeout = (revised_node, tuple(sorted(removed)))
                return None
        return removed

    def ac3(
        self,
        dom: dict[str, set[int]],
        extra_unary: Optional[dict[str, frozenset[int]]] = None,
        revisions: Optional[list[tuple[str, str, str, tuple[int, ...]]]] = None,
        record: bool = False,
    ) -> bool:
        """在 ``dom`` 上原地执行 AC-3（含一元约束），空域返回 False。

        修订记录写入调用方传入的 ``revisions``，保证多次求解互不污染。
        """
        if revisions is None:
            revisions = []
        # 一元约束先做域压缩。
        for node in self.nodes:
            allowed: set[int] = set(dom[node])
            for uc in self.unary[node]:
                allowed &= uc.allowed
            if extra_unary and node in extra_unary:
                allowed &= extra_unary[node]
            removed = dom[node] - allowed
            if removed and record:
                revisions.append(
                    ("<unary>", node, node, tuple(sorted(removed)))
                )
            dom[node] = allowed
            if not dom[node]:
                self.wipeout = (node, tuple(sorted(removed)))
                return False

        queue: list[tuple[BinaryConstraint, str, str]] = []
        for c in self.binary:
            queue.append((c, c.u, c.v))
            if c.u != c.v:  # 自环只有一个方向，避免重复弧
                queue.append((c, c.v, c.u))
        in_queue = set(queue)

        while queue:
            constraint, revised_node, neighbor = queue.pop()
            in_queue.discard((constraint, revised_node, neighbor))
            removed = self._revise(dom, constraint, revised_node, neighbor, revisions, record)
            if removed is None:
                return False
            if removed:
                for c2, nb2 in self._adj[revised_node]:
                    if c2 is constraint:
                        continue
                    arc = (c2, nb2, revised_node)
                    if arc not in in_queue:
                        in_queue.add(arc)
                        queue.append(arc)
        return True

    # -- 回溯 ---------------------------------------------------------------

    def _dfs(
        self,
        dom: dict[str, set[int]],
        assignment: dict[str, int],
        found: list[dict[str, int]],
        cap: int,
    ) -> bool:
        """按节点 ID 顺序、值域升序枚举；返回 True 表示已达上限可停止。"""
        if len(assignment) == len(self.nodes):
            # 终检：check_assignment 返回违规列表，空列表表示通过（含自环兜底）。
            if self.check_assignment(assignment):
                return False
            found.append(dict(assignment))
            return len(found) >= cap
        unassigned = [n for n in self.nodes if n not in assignment]
        node = min(unassigned, key=lambda n: (len(dom[n]), n))
        for value in sorted(dom[node]):
            child = {n: set(vals) for n, vals in dom.items()}
            child[node] = {value}
            if not self.ac3(child):
                continue
            assignment[node] = value
            if self._dfs(child, assignment, found, cap):
                del assignment[node]
                return True
            del assignment[node]
        return False

    def solve(
        self,
        cap: int = 2,
        extra_unary: Optional[dict[str, frozenset[int]]] = None,
        record: bool = False,
    ) -> SolveResult:
        """求至多 ``cap`` 个解（按字典序）。cap<=0 表示枚举全部（仅小图）。"""
        self.wipeout = None
        revisions: list[tuple[str, str, str, tuple[int, ...]]] = []
        dom = {n: set(vals) for n, vals in self.domains.items()}
        if not self.ac3(dom, extra_unary=extra_unary, revisions=revisions, record=record):
            return SolveResult(False, [], False, revisions, self.wipeout)
        if cap <= 0:
            found: list[dict[str, int]] = []
            self._dfs_all(dom, {}, found)
            if not found:
                return SolveResult(False, [], False, revisions, self.wipeout)
            return SolveResult(True, found, False, revisions, None)
        found = []
        truncated = self._dfs(dom, {}, found, cap)
        if not found:
            # 初始弧一致性通过但所有完整分支均失败（如自环/紧约束在深层清空域）。
            return SolveResult(False, [], False, revisions, self.wipeout)
        return SolveResult(True, found, truncated, revisions, self.wipeout)

    def _dfs_all(
        self,
        dom: dict[str, set[int]],
        assignment: dict[str, int],
        found: list[dict[str, int]],
    ) -> None:
        if len(assignment) == len(self.nodes):
            if self.check_assignment(assignment):
                return
            found.append(dict(assignment))
            return
        unassigned = [n for n in self.nodes if n not in assignment]
        node = min(unassigned, key=lambda n: (len(dom[n]), n))
        for value in sorted(dom[node]):
            child = {n: set(vals) for n, vals in dom.items()}
            child[node] = {value}
            if not self.ac3(child):
                continue
            assignment[node] = value
            self._dfs_all(child, assignment, found)
            del assignment[node]
    # -- 单节点极值 ----------------------------------------------------------

    def extrema(
        self,
        node: str,
        extra_unary: Optional[dict[str, frozenset[int]]] = None,
    ) -> Optional[tuple[int, int, dict[str, int], dict[str, int]]]:
        """返回同一全图可行域内该节点的 (最小值, 最大值, 最小见证, 最大见证)。

        见证为完整赋值，证明极值确实来自全图可行域而非局部路径相加。
        """
        base = {n: set(vals) for n, vals in self.domains.items()}
        if not self.ac3(base, extra_unary=extra_unary):
            return None
        lo_w = self._search_extremum(
            {n: set(vals) for n, vals in base.items()}, {}, node, False
        )
        hi_w = self._search_extremum(
            {n: set(vals) for n, vals in base.items()}, {}, node, True
        )
        if lo_w is None or hi_w is None:
            return None
        return lo_w[node], hi_w[node], lo_w, hi_w

    def _search_extremum(
        self,
        dom: dict[str, set[int]],
        assignment: dict[str, int],
        target: str,
        descending: bool,
    ) -> Optional[dict[str, int]]:
        """求目标节点取最小（降序则最大）值的首个完整可行赋值。

        目标节点一旦未赋值就优先选取并按要求的序枚举其值，保证返回的是
        真正的全局极值；其余节点按 MRV 选择、值升序，仅用于快速找见证。
        """
        if len(assignment) == len(self.nodes):
            if not self.check_assignment(assignment):
                return dict(assignment)
            return None
        if target not in assignment:
            node = target
            values = sorted(dom[node], reverse=descending)
        else:
            unassigned = [n for n in self.nodes if n not in assignment]
            node = min(unassigned, key=lambda n: (len(dom[n]), n))
            values = sorted(dom[node])
        for value in values:
            child = {n: set(vals) for n, vals in dom.items()}
            child[node] = {value}
            if not self.ac3(child):
                continue
            assignment[node] = value
            out = self._search_extremum(child, assignment, target, descending)
            del assignment[node]
            if out is not None:
                return out
        return None

    def check_assignment(
        self,
        assignment: dict[str, int],
        extra_unary: Optional[dict[str, frozenset[int]]] = None,
    ) -> list[str]:
        """校验完整赋值，返回被违反的约束 id 列表（空列表即满足）。"""
        violated: list[str] = []
        for node in self.nodes:
            if node not in assignment:
                violated.append(f"<missing:{node}>")
                continue
            x = assignment[node]
            for uc in self.unary[node]:
                if not uc.holds(x):
                    violated.append(uc.cid)
            if extra_unary and node in extra_unary and x not in extra_unary[node]:
                violated.append(f"<extra-unary:{node}>")
        for c in self.binary:
            if not c.holds(assignment[c.u], assignment[c.v]):
                violated.append(c.cid)
        return violated


# ---------------------------------------------------------------------------
# 独立全枚举（交叉核对用，与上面的 AC-3/回溯完全独立的代码路径）
# ---------------------------------------------------------------------------


def brute_force_enumerate(
    nodes: list[str],
    binary: list[BinaryConstraint],
    unary: dict[str, list[UnaryConstraint]],
    domains: dict[str, tuple[int, int]],
    cap: int = 2_000_000,
) -> Optional[list[dict[str, int]]]:
    """按节点 ID 字典序全枚举可行赋值。

    组合空间超过 ``cap`` 时返回 None（跳过核对，不做假核对）。
    """
    ordered = sorted(nodes)
    spans = [domains[n][1] - domains[n][0] + 1 for n in ordered]
    total = 1
    for s in spans:
        total *= s
        if total > cap:
            return None

    pool: list[list[int]] = [list(range(domains[n][0], domains[n][1] + 1)) for n in ordered]
    solutions: list[dict[str, int]] = []

    def rec(i: int, acc: dict[str, int]) -> None:
        if i == len(ordered):
            for c in binary:
                if not c.holds(acc[c.u], acc[c.v]):
                    return
            for n in ordered:
                for uc in unary[n]:
                    if not uc.holds(acc[n]):
                        return
            solutions.append(dict(acc))
            return
        n = ordered[i]
        for x in pool[i]:
            acc[n] = x
            rec(i + 1, acc)
        acc.pop(n, None)

    rec(0, {})
    return solutions


# ---------------------------------------------------------------------------
# 连通分量
# ---------------------------------------------------------------------------


def connected_components(
    nodes: Iterable[str], binary: Iterable[BinaryConstraint]
) -> list[list[str]]:
    """按二元约束（边）划分连通分量，孤立节点各自成分量。"""
    adj: dict[str, set[str]] = {n: set() for n in nodes}
    for c in binary:
        adj[c.u].add(c.v)
        adj[c.v].add(c.u)
    seen: set[str] = set()
    components: list[list[str]] = []
    for start in sorted(adj):
        if start in seen:
            continue
        stack = [start]
        comp: list[str] = []
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            comp.append(n)
            stack.extend(adj[n] - seen)
        components.append(sorted(comp))
    return components


# ---------------------------------------------------------------------------
# 回路基（生成元）与残差
# ---------------------------------------------------------------------------


def cycle_basis(
    nodes: list[str], binary: list[BinaryConstraint]
) -> list[list[BinaryConstraint]]:
    """返回无向多重图的基本回路集合（每条二元约束视为一条无向边）。

    用生成森林构造：每条加入后形成环的边，沿森林路径闭合出一个基本回路。
    平行边产生长度为 2 的回路，自环产生长度为 1 的回路。
    """
    parent: dict[str, str] = {n: n for n in nodes}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    forest_adj: dict[str, list[tuple[str, BinaryConstraint]]] = {n: [] for n in nodes}
    cycles: list[list[BinaryConstraint]] = []

    for c in binary:
        if c.u == c.v:
            cycles.append([c])
            continue
        ru, rv = find(c.u), find(c.v)
        if ru == rv:
            # 沿森林找 u -> v 路径
            path_edges: list[BinaryConstraint] = []
            path_nodes = _forest_path(forest_adj, c.u, c.v)
            for a, b in zip(path_nodes, path_nodes[1:]):
                edge = next(
                    e for (nb, e) in forest_adj[a] if nb == b
                )
                path_edges.append(edge)
            cycles.append(path_edges + [c])
        else:
            parent[ru] = rv
            forest_adj[c.u].append((c.v, c))
            forest_adj[c.v].append((c.u, c))
    return cycles


def _forest_path(
    forest_adj: dict[str, list[tuple[str, BinaryConstraint]]], src: str, dst: str
) -> list[str]:
    """BFS 求森林中两节点间唯一路径的节点序列。"""
    prev: dict[str, Optional[tuple[str, str]]] = {src: None}
    queue = [src]
    while queue:
        n = queue.pop(0)
        if n == dst:
            break
        for nb, _e in forest_adj[n]:
            if nb not in prev:
                prev[nb] = (n, nb)
                queue.append(nb)
    path = [dst]
    while path[-1] != src:
        p = prev[path[-1]]
        assert p is not None
        path.append(p[0])
    path.reverse()
    return path


def orient_cycle(
    cyc: list[BinaryConstraint],
) -> Optional[list[tuple[BinaryConstraint, bool]]]:
    """把基本回路整理为首尾相接的 (约束, 是否正向) 序列；结构不闭合返回 None。

    正向指沿约束自身 u->v 方向行走，反向行走时步长集合取相反数。
    """
    if not cyc:
        return None
    first = cyc[0]
    ordered: list[tuple[BinaryConstraint, bool]] = [(first, True)]
    current_head = first.v
    used = {first.cid}
    rest = [e for e in cyc if e.cid != first.cid]
    while rest:
        nxt = None
        for e in rest:
            if e.cid in used:
                continue
            if e.u == current_head:
                nxt = (e, True)
                break
            if e.v == current_head:
                nxt = (e, False)
                break
        if nxt is None:
            return None
        e, forward = nxt
        used.add(e.cid)
        ordered.append((e, forward))
        current_head = e.v if forward else e.u
        rest = [x for x in rest if x.cid not in used]
    return ordered if current_head == first.u else None


def signed_step_set(edge: BinaryConstraint, forward: bool) -> frozenset[int]:
    """沿遍历方向的单步候选差值集合。

    正向 (u->v)：x_v - x_u ∈ allowed；
    反向 (v->u)：x_u - x_v ∈ {-d | d ∈ allowed}。
    """
    if forward:
        return edge.allowed
    return frozenset(-d for d in edge.allowed)


def minkowski_sum(sets: Iterable[Iterable[int]]) -> frozenset[int]:
    """有限整数集合的闵可夫斯基和。"""
    acc: frozenset[int] = frozenset({0})
    for s in sets:
        acc = frozenset({a + b for a in acc for b in s})
    return acc
