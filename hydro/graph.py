"""DAG 图算法：拓扑排序与全节点对最长延迟路径。

约束很小（<=30 节点），最长延迟路径用 NumPy 做 Floyd 松弛；
NumPy 不可用时回退到等价的纯 Python 三重循环，结果完全一致。
"""

from __future__ import annotations

from collections import deque
from typing import Dict, List, Mapping, Sequence

try:  # pragma: no cover - 环境差异分支
    import numpy as np

    _HAVE_NUMPY = True
except Exception:  # pragma: no cover
    _HAVE_NUMPY = False

# 不可达标记（延迟本身是非负整数，-1 安全）
_UNREACH = -1


def topo_order(nodes: Sequence[str], edges: Sequence) -> List[str]:
    """Kahn 拓扑排序。

    同层候选中按节点名排序，保证零延迟边场景下求值顺序确定；
    拓扑序天然满足「父节点先于子节点」，故同一时间步零延迟边可直接求值。
    检测到环时抛出 :class:`ValueError`。
    """
    indeg: Dict[str, int] = {n: 0 for n in nodes}
    adj: Dict[str, List[str]] = {n: [] for n in nodes}
    for e in edges:
        indeg[e.to_id] += 1
        adj[e.from_id].append(e.to_id)
    for v in adj.values():
        v.sort()
    ready = deque(sorted(n for n, d in indeg.items() if d == 0))
    order: List[str] = []
    while ready:
        n = ready.popleft()
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    if len(order) != len(nodes):
        remaining = sorted(n for n, d in indeg.items() if d > 0)
        raise ValueError(f"河网存在环，无法拓扑排序，环上节点: {remaining}")
    return order


def longest_delay_matrix(
    nodes: Sequence[str], edge_map: Mapping, order: Sequence[str]
):
    """返回 ``(mat, index)``：全节点对最长路径延迟矩阵与节点下标。

    ``mat[i, j]`` 是节点 ``i`` 到节点 ``j`` 的最长（延迟之和最大）有向
    路径延迟；不可达为 -1，到自身为 0。同一起讫点存在多条路径时取最长，
    用于确定分析必须延伸到的延迟尾部。
    """
    n = len(nodes)
    index = {name: i for i, name in enumerate(nodes)}
    if _HAVE_NUMPY:
        mat = np.full((n, n), _UNREACH, dtype=np.int64)
        np.fill_diagonal(mat, 0)
        for u, outs in edge_map.items():
            iu = index[u]
            for e in outs:
                iv = index[e.to_id]
                # 平行边取延迟较大者
                if int(e.delay) > mat[iu, iv]:
                    mat[iu, iv] = int(e.delay)
        for k in order:
            ik = index[k]
            row_k = mat[ik]
            reachable = np.nonzero(row_k >= 0)[0]
            if reachable.size == 0:
                continue
            col_k = mat[:, ik]
            incoming = np.nonzero(col_k >= 0)[0]
            if incoming.size == 0:
                continue
            # 所有 i->k 与 k->j 的候选延长
            cand = col_k[incoming, None] + row_k[None, reachable]
            block = mat[incoming[:, None], reachable[None, :]]
            mat[incoming[:, None], reachable[None, :]] = np.maximum(block, cand)
        return mat, index

    # 纯 Python 回退：拓扑序上的动态规划
    mat = [[_UNREACH] * n for _ in range(n)]
    for i in range(n):
        mat[i][i] = 0
    for u, outs in edge_map.items():
        iu = index[u]
        for e in outs:
            iv = index[e.to_id]
            mat[iu][iv] = max(mat[iu][iv], int(e.delay))
    for k in order:
        ik = index[k]
        for i in range(n):
            dik = mat[i][ik]
            if dik < 0:
                continue
            row_i = mat[i]
            row_k = mat[ik]
            for j in range(n):
                dkj = row_k[j]
                if dkj < 0:
                    continue
                cand = dik + dkj
                if cand > row_i[j]:
                    row_i[j] = cand
    return mat, index
