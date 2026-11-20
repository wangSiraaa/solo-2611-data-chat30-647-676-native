"""二分图匹配引擎（纯函数，不碰数据库）。

优化目标（严格按题面三级字典序，不做最近邻贪心）：
  1) 最大化匹配数 K；
  2) 在所有 K 条边的匹配中最小化夹角平方和 C；
  3) 在匹配数仍为 K 且总代价 <= C + eps 的方案里，
     取“排序后 (star_id, detection_id) 列表”字典序最小者。

实现：
  * 候选边仅按夹角阈值产生，亮度不进入任何规则；
  * K 由增广路求出，C 由带虚拟节点的线性指派（scipy LSA，匈牙利法）求出；
  * 第 3 级用逐条边的可行性裁决贪心，每次裁决调用一次 LSA 预言机，
    预言机在“已固定边 + 仍需达到 K + 总代价预算 C+eps”下判定可行性，
    并记录完整裁决轨迹供 explain 使用；
  * brute_force_optimal 仅供小例（<=5 星 5 点）独立枚举核对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from scipy.optimize import linear_sum_assignment

from .vectors import angle_matrix, validate_unit_direction

# 指派矩阵参数：
# 真实边 = 夹角^2（<= pi^2 < 10）；离配（虚拟格）= P_UNMATCH；禁边 = BIG。
# 取 P_UNMATCH=100 > min(n,m)*pi^2，则基数为 k 的指派总成本落在
# [k*P, k*P + k*pi^2) ⊂ [k*P, (k+1)*P)，基数区间互不重叠 => LSA 先最大化基数，
# 同基数下 k*P 为常数，再最小化夹角平方和。
P_UNMATCH = 1.0e2
BIG = 1.0e100


@dataclass(frozen=True)
class Candidate:
    star_id: str
    detection_id: str
    angle: float

    @property
    def cost(self) -> float:
        return self.angle * self.angle

    def as_pair(self) -> Tuple[str, str]:
        return (self.star_id, self.detection_id)


@dataclass
class MatchOutcome:
    exposure_id: str
    threshold: float
    eps: float
    matches: List[Tuple[str, str, float]]          # (star_id, detection_id, angle)
    unmatched_stars: List[str]
    unmatched_detections: List[str]
    candidates: List[Candidate]
    max_cardinality: int
    min_cost: float
    total_cost: float
    trace: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def match_pairs(self) -> List[Tuple[str, str]]:
        return [(s, d) for s, d, _ in self.matches]


# ------------------------------------------------------------- input helpers
def prepare_detections(
    raw: Sequence[Dict[str, Any]], *, unit_tol: float
) -> Tuple[List[str], np.ndarray, List[int]]:
    """校验曝光亮点：detection_id 本曝光唯一、方向有限且单位长度合规、亮度为非负整数。"""
    ids: List[str] = []
    dirs: List[np.ndarray] = []
    brights: List[int] = []
    seen: Set[str] = set()
    for item in raw:
        did = str(item["detection_id"])
        if did in seen:
            raise ValueError(f"detection_id 在本曝光内重复: {did}")
        seen.add(did)
        u = validate_unit_direction(item["direction"], unit_tol, name=f"direction[{did}]")
        bright = item.get("brightness", 0)
        if not isinstance(bright, int) or isinstance(bright, bool) or bright < 0:
            raise ValueError(f"brightness 必须是非负整数，detection={did}: {bright!r}")
        ids.append(did)
        dirs.append(u)
        brights.append(bright)
    mat = np.vstack(dirs) if dirs else np.zeros((0, 3), dtype=np.float64)
    return ids, mat, brights


def prepare_stars(
    rows: Sequence[Dict[str, Any]], *, unit_tol: float
) -> Tuple[List[str], np.ndarray]:
    ids: List[str] = []
    dirs: List[np.ndarray] = []
    seen: Set[str] = set()
    for item in rows:
        sid = str(item["star_id"])
        if sid in seen:
            raise ValueError(f"快照内 star_id 重复: {sid}")
        seen.add(sid)
        dirs.append(validate_unit_direction(item["direction"], unit_tol, name=f"direction[{sid}]"))
        ids.append(sid)
    mat = np.vstack(dirs) if dirs else np.zeros((0, 3), dtype=np.float64)
    return ids, mat


# ------------------------------------------------------------- candidates
def build_candidates(
    star_ids: Sequence[str], star_dirs: np.ndarray,
    det_ids: Sequence[str], det_dirs: np.ndarray,
    threshold: float,
) -> List[Candidate]:
    """夹角 <= threshold 建边；向量化全量角度矩阵，无逐点贪心。"""
    if len(star_ids) == 0 or len(det_ids) == 0:
        return []
    if threshold < 0:
        raise ValueError("threshold 必须非负")
    ang = angle_matrix(np.asarray(star_dirs), np.asarray(det_dirs))
    ii, jj = np.nonzero(ang <= threshold + 1e-15)
    out = [
        Candidate(star_id=star_ids[i], detection_id=det_ids[j], angle=float(ang[i, j]))
        for i, j in zip(ii.tolist(), jj.tolist())
    ]
    out.sort(key=lambda c: (c.star_id, c.detection_id))
    return out


# ------------------------------------------------------------- cardinality
def max_cardinality(edges: Sequence[Candidate]) -> int:
    """Kuhn 增广路求二分图最大匹配数（只用于基数，不做最终分配）。"""
    adj: Dict[str, List[str]] = {}
    for e in edges:
        adj.setdefault(e.star_id, []).append(e.detection_id)
    match_d: Dict[str, str] = {}

    def augment(s: str, visited: Set[str]) -> bool:
        for d in adj.get(s, []):
            if d in visited:
                continue
            visited.add(d)
            if d not in match_d or augment(match_d[d], visited):
                match_d[d] = s
                return True
        return False

    k = 0
    for s in sorted(adj):
        if augment(s, set()):
            k += 1
    return k


# ------------------------------------------------------------- LSA oracle
def _oracle(
    n: int, m: int, edge_cost: Dict[Tuple[int, int], float],
    forced: Set[Tuple[int, int]], need_k: int, budget: float,
) -> Tuple[bool, List[Tuple[int, int]], float]:
    """可行性预言机：是否存在包含 forced、基数为 need_k、总代价 <= budget 的匹配。

    构造 (n+m)×(n+m) 完整置换矩阵（行/列：前 n 个是星/星槽，后 m 个是亮点/亮点槽）：
      * 真实块 行 i∈[0,n) × 列 j∈[0,m)：候选边 = 夹角^2，非候选 = BIG；
      * 星离配对角 行 i × 列 m+i：P_UNMATCH；
      * 亮点离配对角 行 n+j × 列 j：P_UNMATCH；
      * 右下角虚拟块 行 n+j × 列 m+i：0（虚拟节点互配，固定常数不影响比较）。
    LSA 在完整置换下先最大化真实边数（离配昂贵），同基数再最小化平方和。
    """
    size = n + m
    mx = np.full((size, size), BIG, dtype=np.float64)
    for (i, j), c in edge_cost.items():
        mx[i, j] = c
    for i in range(n):
        mx[i, m + i] = P_UNMATCH
    for j in range(m):
        mx[n + j, j] = P_UNMATCH
    # 右下角虚拟-虚拟块：允许置换闭合；其总代价恒为 0，不影响方案比较
    mx[n:, m:] = 0.0
    for i, j in forced:
        if (i, j) not in edge_cost:
            return False, [], float("inf")
        # 固定该格：封掉同行/同列所有格，再开放该格
        mx[i, :] = BIG
        mx[:, j] = BIG
        mx[i, j] = edge_cost[(i, j)]

    row_ind, col_ind = linear_sum_assignment(mx)
    pairs = [(int(i), int(j)) for i, j in zip(row_ind, col_ind) if i < n and j < m]
    if any(mx[i, j] >= BIG / 2 for i, j in pairs):
        return False, pairs, float("inf")
    if len(pairs) != need_k or not forced.issubset(set(pairs)):
        return False, pairs, float("inf")
    total = float(sum(edge_cost[(i, j)] for i, j in pairs))
    return (total <= budget), pairs, total


# ------------------------------------------------------------- main solver
def solve_matching(
    exposure_id: str,
    star_ids: Sequence[str], det_ids: Sequence[str],
    candidates: Sequence[Candidate], eps: float, threshold: float,
) -> MatchOutcome:
    """三级目标求解，返回完整结果与逐条裁决轨迹。"""
    edges = sorted(candidates, key=lambda c: (c.star_id, c.detection_id))
    s_index = {s: i for i, s in enumerate(star_ids)}
    d_index = {d: j for j, d in enumerate(det_ids)}
    n, m = len(star_ids), len(det_ids)
    edge_cost = {
        (s_index[e.star_id], d_index[e.detection_id]): e.cost for e in edges
    }
    edge_by_pair = {e.as_pair(): e for e in edges}

    k = max_cardinality(edges)
    trace: List[Dict[str, Any]] = []
    if k == 0:
        return MatchOutcome(
            exposure_id=exposure_id, threshold=threshold, eps=eps,
            matches=[], unmatched_stars=list(star_ids),
            unmatched_detections=list(det_ids), candidates=edges,
            max_cardinality=0, min_cost=0.0, total_cost=0.0, trace=trace,
        )

    # 第 2 级：无固定边求最小代价 C
    ok0, pairs0, c_min = _oracle(n, m, edge_cost, set(), k, budget=float("inf"))
    assert ok0 and len(pairs0) == k
    budget = c_min + eps

    # 第 3 级：字典序贪心 + 可行性预言机（预算 C+eps）
    chosen: Set[Tuple[str, str]] = set()
    used_s: Set[str] = set()
    used_d: Set[str] = set()
    for e in edges:
        if len(chosen) == k:
            trace.append({
                "edge": e.as_pair(), "angle": e.angle, "decision": "not_considered",
                "reason": f"已固定 {k} 条边，达到最大匹配数，剩余边不再裁决",
            })
            continue
        if e.star_id in used_s or e.detection_id in used_d:
            occ_s = next((p for p in chosen if p[0] == e.star_id), None)
            occ_d = next((p for p in chosen if p[1] == e.detection_id), None)
            trace.append({
                "edge": e.as_pair(), "angle": e.angle, "decision": "skip",
                "reason": "顶点已被占用",
                "occupied_by": {"star": occ_s, "detection": occ_d},
            })
            continue
        forced_idx = {
            (s_index[s], d_index[d]) for s, d in chosen
        }
        forced_idx.add((s_index[e.star_id], d_index[e.detection_id]))
        feasible, _completion, cost = _oracle(n, m, edge_cost, forced_idx, k, budget)
        event: Dict[str, Any] = {
            "edge": e.as_pair(), "angle": e.angle,
            "forced_before": sorted(chosen), "best_completion_cost": cost,
            "budget": budget,
        }
        if feasible:
            chosen.add(e.as_pair())
            used_s.add(e.star_id)
            used_d.add(e.detection_id)
            event["decision"] = "include"
            event["reason"] = (
                f"包含该边后仍存在 {k} 条边且总代价 {cost:.12g} <= C+eps={budget:.12g} 的方案")
        else:
            event["decision"] = "exclude"
            if cost == float("inf"):
                event["reason"] = f"包含该边后无法再凑出最大匹配数 K={k}（基数约束落选）"
            else:
                event["reason"] = (
                    f"包含该边的最优 {k} 边方案总代价 {cost:.12g} > C+eps={budget:.12g}，"
                    "代价预算约束落选")
        trace.append(event)

    assert len(chosen) == k
    final_pairs = sorted(chosen)
    total = float(sum(edge_by_pair[p].cost for p in final_pairs))
    matched_s = {s for s, _ in final_pairs}
    matched_d = {d for _, d in final_pairs}
    matches = [(s, d, edge_by_pair[(s, d)].angle) for s, d in final_pairs]
    return MatchOutcome(
        exposure_id=exposure_id, threshold=threshold, eps=eps,
        matches=matches,
        unmatched_stars=[s for s in star_ids if s not in matched_s],
        unmatched_detections=[d for d in det_ids if d not in matched_d],
        candidates=edges, max_cardinality=k, min_cost=c_min,
        total_cost=total, trace=trace,
    )


# ------------------------------------------------------------- brute force
def brute_force_optimal(
    star_ids: Sequence[str], det_ids: Sequence[str],
    candidates: Sequence[Candidate], eps: float,
) -> Dict[str, Any]:
    """独立枚举全部匹配（仅用于 <=5x5 小例核对），逻辑与主求解器分开写。

    枚举每个匹配（顶点不相交边集），按 (基数, 代价, 字典序列表) 同规则选优。
    """
    edges = sorted(candidates, key=lambda c: (c.star_id, c.detection_id))
    best: Optional[Dict[str, Any]] = None
    best_k = -1
    best_cost = float("inf")

    def consider(pairs: List[Tuple[str, str]]) -> None:
        nonlocal best, best_k, best_cost
        k = len(pairs)
        cost = float(sum(next(e.cost for e in edges if e.as_pair() == p) for p in pairs))
        key = sorted(pairs)
        if k > best_k or (k == best_k and cost < best_cost - 1e-15):
            best_k, best_cost, best = k, cost, {"pairs": key, "cost": cost, "k": k}

    def dfs(idx: int, used_s: Set[str], used_d: Set[str], picked: List[Tuple[str, str]]) -> None:
        consider(list(picked))
        for q in range(idx, len(edges)):
            e = edges[q]
            if e.star_id in used_s or e.detection_id in used_d:
                continue
            picked.append(e.as_pair())
            used_s.add(e.star_id)
            used_d.add(e.detection_id)
            dfs(q + 1, used_s, used_d, picked)
            picked.pop()
            used_s.remove(e.star_id)
            used_d.remove(e.detection_id)

    dfs(0, set(), set(), [])
    assert best is not None
    # 第 3 级：在 C+eps 带内取字典序最小 —— 再枚举一次做带内裁决
    budget = best_cost + eps
    lex_best: Optional[List[Tuple[str, str]]] = None

    def consider_band(pairs: List[Tuple[str, str]]) -> None:
        nonlocal lex_best
        if len(pairs) != best_k:
            return
        cost = float(sum(next(e.cost for e in edges if e.as_pair() == p) for p in pairs))
        if cost <= budget + 1e-15:
            key = sorted(pairs)
            if lex_best is None or key < lex_best:
                lex_best = key

    def dfs2(idx: int, used_s: Set[str], used_d: Set[str], picked: List[Tuple[str, str]]) -> None:
        consider_band(list(picked))
        for q in range(idx, len(edges)):
            e = edges[q]
            if e.star_id in used_s or e.detection_id in used_d:
                continue
            picked.append(e.as_pair())
            used_s.add(e.star_id)
            used_d.add(e.detection_id)
            dfs2(q + 1, used_s, used_d, picked)
            picked.pop()
            used_s.remove(e.star_id)
            used_d.remove(e.detection_id)

    dfs2(0, set(), set(), [])
    best["pairs"] = lex_best
    best["cost"] = float(sum(next(e.cost for e in edges if e.as_pair() == p) for p in lex_best))
    return best
