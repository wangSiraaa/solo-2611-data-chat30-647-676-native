"""履约分配求解器（与音频完全解耦，只消费监测匹配）。

输入
----
* 排期：唯一 schedule_id、material_id、半开窗口 [win_start, win_end)、
  最低连续播放时长 min_contiguous（采样点）、要求次数 1..3；
* 监测匹配：只用 certainty == "confirmed" 的播放，且素材一致、
  开始点落在窗口内、连续时长达标。

优化目标（按优先级，严格字典序）
--------------------------------
1. 最大化已满足要求的总次数（最大流）；
2. 在 1 的前提下，最小化 sum(2 * |play_start - window_midpoint|)。
   窗口中点可能为半整数，故距离统一乘 2，全程整数运算无浮点；
3. 仍并列时，把被分配的 (schedule_id, play_id) 对按字典序排序，
   取字典序最小的列表。

实现
----
第 1、2 目标用单源单汇最小费用最大流（SPFA 增广，容量均为 1）。
第 3 目标不能靠边权微扰可靠表达，因此对候选配对按字典序逐一做
"强制接受/可行性" 判定：强制后若仍能取得与目标相等的最大流量，
该配对才进入最终解；否则永久排除。每次播放最多被一条排期消费一次。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

INF = 10**18


@dataclass(frozen=True)
class Schedule:
    schedule_id: str
    material_id: str
    win_start: int          # 半开窗
    win_end: int
    min_contiguous: int
    required_count: int

    def doubled_distance(self, play_start: int) -> int:
        # 2 * |start - (win_start+win_end)/2| = |2*start - win_start - win_end|
        return abs(2 * play_start - self.win_start - self.win_end)


@dataclass
class Allocation:
    schedule_id: str
    play_id: str
    material_id: str
    prog_start: int
    prog_end: int
    contiguous_frames: int
    doubled_distance: int
    basis: dict


# --------------------------------------------------------------------------- #
# 最小费用最大流（容量 1 的二分图，SPFA 即可；节点数小，无需复杂势函数）
# --------------------------------------------------------------------------- #
class _MinCostFlow:
    def __init__(self, n: int):
        self.n = n
        self.g: list[list[dict]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int):
        fwd = {"to": v, "cap": cap, "cost": cost, "rev": len(self.g[v])}
        rev = {"to": u, "cap": 0, "cost": -cost, "rev": len(self.g[u])}
        self.g[u].append(fwd)
        self.g[v].append(rev)

    def flow(self, s: int, t: int, maxf: int,
             disabled_edges: frozenset[int] | None = None) -> tuple[int, int]:
        """返回 (流量, 最小费用)。disabled_edges 中的边下标对（u, 序号）禁用。"""
        disabled = disabled_edges or frozenset()
        flow = cost = 0
        while flow < maxf:
            dist = [INF] * self.n
            prev_v = [-1] * self.n
            prev_e = [-1] * self.n
            in_q = [False] * self.n
            dist[s] = 0
            queue = [s]
            in_q[s] = True
            head = 0
            while head < len(queue):
                u = queue[head]
                head += 1
                in_q[u] = False
                for ei, e in enumerate(self.g[u]):
                    if e["cap"] <= 0 or (u, ei) in disabled:
                        continue
                    v = e["to"]
                    nd = dist[u] + e["cost"]
                    if nd < dist[v]:
                        dist[v] = nd
                        prev_v[v] = u
                        prev_e[v] = ei
                        if not in_q[v]:
                            queue.append(v)
                            in_q[v] = True
            if dist[t] == INF:
                break
            # 增广 1 单位（所有边容量为 1）
            v = t
            while v != s:
                u, ei = prev_v[v], prev_e[v]
                e = self.g[u][ei]
                e["cap"] -= 1
                self.g[v][e["rev"]]["cap"] += 1
                cost += e["cost"]
                v = u
            flow += 1
        return flow, cost

    def edge_key(self, u: int, v: int) -> tuple[int, int]:
        for ei, e in enumerate(self.g[u]):
            if e["to"] == v and e["cap"] >= 0:
                return u, ei
        raise KeyError((u, v))


def _eligible(schedule: Schedule, play: dict) -> bool:
    if play["certainty"] != "confirmed":
        return False
    if play["material_id"] != schedule.material_id:
        return False
    if not (schedule.win_start <= play["prog_start"] < schedule.win_end):
        return False
    # 连续播放时长：帧计数 * 帧移 = 连续匹配采样点数
    hop = play.get("evidence", {}).get("frame_hop_samples", 1)
    contiguous_samples = play["contiguous_frames"] * hop
    return contiguous_samples >= schedule.min_contiguous


def _build_graph(schedules: list[Schedule], plays: list[dict]):
    sched_nodes = {s.schedule_id: i + 1 for i, s in enumerate(schedules)}
    play_nodes = {p["play_id"]: 1 + len(schedules) + i for i, p in enumerate(plays)}
    sink = 1 + len(schedules) + len(plays)
    mcf = _MinCostFlow(sink + 1)
    source = 0
    edge_index: dict[tuple[str, str], tuple[int, int]] = {}
    for s in schedules:
        mcf.add_edge(source, sched_nodes[s.schedule_id], s.required_count, 0)
    for p in plays:
        mcf.add_edge(play_nodes[p["play_id"]], sink, 1, 0)
    for s in schedules:
        for p in plays:
            if _eligible(s, p):
                u, v = sched_nodes[s.schedule_id], play_nodes[p["play_id"]]
                mcf.add_edge(u, v, 1, s.doubled_distance(p["prog_start"]))
                edge_index[(s.schedule_id, p["play_id"])] = (u, len(mcf.g[u]) - 1)
    return mcf, source, sink, edge_index, sched_nodes, play_nodes


def solve(schedules: list[Schedule], plays: list[dict]) -> dict:
    """返回分配结果、未满足排期、剩余播放与求解诊断。"""
    # 输入校验
    ids: set[str] = set()
    for s in schedules:
        if s.schedule_id in ids:
            raise ValueError(f"schedule_id 重复: {s.schedule_id}")
        ids.add(s.schedule_id)
        if not (1 <= s.required_count <= 3):
            raise ValueError(f"{s.schedule_id}: required_count 必须在 1..3")
        if not (s.win_start < s.win_end):
            raise ValueError(f"{s.schedule_id}: 窗口必须为非空半开区间")
        if s.min_contiguous < 0:
            raise ValueError(f"{s.schedule_id}: min_contiguous 不能为负")
    pids: set[str] = set()
    for p in plays:
        if p["play_id"] in pids:
            raise ValueError(f"play_id 重复: {p['play_id']}")
        pids.add(p["play_id"])

    max_demand = sum(s.required_count for s in schedules)
    base, source, sink, edge_index, _, _ = _build_graph(schedules, plays)
    best_flow, best_cost = base.flow(source, sink, max_demand)
    sched_by_id = {s.schedule_id: s for s in schedules}
    play_by_id = {p["play_id"]: p for p in plays}

    def pair_cost(sid: str, pid: str) -> int:
        return sched_by_id[sid].doubled_distance(play_by_id[pid]["prog_start"])

    # 字典序决胜：按 (schedule_id, play_id) 排序候选配对，逐一强制判定。
    # 强制一对 = 保留该配对边、禁用与其争用同一播放或同一排期容量的其它边，
    # 再禁用此前已拒绝的边；若仍取得 (best_flow, best_cost) 则接受。
    candidate_pairs = sorted(edge_index.keys())
    forced: list[tuple[str, str]] = []
    rejected: set[tuple[str, str]] = set()
    req_count = {s.schedule_id: s.required_count for s in schedules}
    forced_plays: set[str] = set()
    forced_sched_count: dict[str, int] = {}
    for pair in candidate_pairs:
        sid, pid = pair
        # 内部冲突：该播放已被强制集合占用，或该排期容量已满 -> 直接拒绝，
        # 保证每次播放最多满足一条排期的一次要求、排期不超要求次数。
        if pid in forced_plays or forced_sched_count.get(sid, 0) >= req_count[sid]:
            rejected.add(pair)
            continue
        trial = forced + [pair]
        trial_sched_count = dict(forced_sched_count)
        trial_sched_count[sid] = trial_sched_count.get(sid, 0) + 1
        cap_left = {s2: req_count[s2] - trial_sched_count.get(s2, 0)
                    for s2 in req_count}
        used_play = forced_plays | {pid}
        mcf2, src, snk, idx2, _, _ = _build_graph(schedules, plays)
        block: set[tuple[int, int]] = set()
        for pair2, (u, ei) in idx2.items():
            sid2, pid2 = pair2
            if pair2 in trial:
                continue
            if pair2 in rejected or pid2 in used_play or cap_left[sid2] <= 0:
                block.add((u, ei))
        f2, c2 = mcf2.flow(src, snk, max_demand, frozenset(block))
        # 被强制的配对边在残余网络中 cap==0，其费用计入 c2，无需另加
        if f2 == best_flow and c2 == best_cost:
            forced = trial
            forced_plays.add(pid)
            forced_sched_count = trial_sched_count
        else:
            rejected.add(pair)

    # 最终解：在同样的禁用集合上再跑一次并回收被选用的配对。
    # 强制配对的边保持启用；仅禁用被拒绝的边，以及会让某排期/播放超过
    # 强制集合已占用容量的争用边（强制边自身始终可走）。
    forced_sched_count: dict[str, int] = {}
    for sid, _ in forced:
        forced_sched_count[sid] = forced_sched_count.get(sid, 0) + 1
    mcf3, src, snk, idx3, sched_nodes, _ = _build_graph(schedules, plays)
    cap_left = {sid: s.required_count - forced_sched_count.get(sid, 0)
                for s in schedules for sid in [s.schedule_id]}
    used_play = {p for _, p in forced}
    block3: set[tuple[int, int]] = set()
    for pair, (u, ei) in idx3.items():
        sid, pid = pair
        if pair in forced:
            continue
        if pair in rejected or pid in used_play or cap_left[sid] <= 0:
            block3.add((u, ei))
    f3, c3 = mcf3.flow(src, snk, max_demand, frozenset(block3))
    if not (f3 == best_flow and c3 == best_cost):
        # 费用与流量若有偏差说明决胜逻辑有误，直接失败而非静默产出次优解
        raise AssertionError(((f3, c3), (best_flow, best_cost)))

    allocations: list[Allocation] = []
    chosen: set[tuple[str, str]] = set(forced)
    for (sid, pid), (u, ei) in idx3.items():
        if mcf3.g[u][ei]["cap"] == 0:  # 正向配对边容量耗尽 = 被选用
            chosen.add((sid, pid))
    for sid, pid in sorted(chosen):
        s, p = sched_by_id[sid], play_by_id[pid]
        hop = p.get("evidence", {}).get("frame_hop_samples", 1)
        allocations.append(Allocation(
            schedule_id=sid, play_id=pid, material_id=s.material_id,
            prog_start=p["prog_start"], prog_end=p["prog_end"],
            contiguous_frames=p["contiguous_frames"],
            doubled_distance=s.doubled_distance(p["prog_start"]),
            basis={
                "window": [s.win_start, s.win_end],
                "window_half_open": True,
                "material_match": p["material_id"] == s.material_id,
                "contiguous_samples": p["contiguous_frames"] * hop,
                "min_contiguous_required": s.min_contiguous,
                "required_count": s.required_count,
                "certainty": p["certainty"],
                "doubled_distance_formula": "abs(2*prog_start-win_start-win_end)",
            }))
    allocations.sort(key=lambda a: (a.schedule_id, a.play_id))

    assigned_counts: dict[str, int] = {}
    used_plays: set[str] = set()
    for a in allocations:
        assigned_counts[a.schedule_id] = assigned_counts.get(a.schedule_id, 0) + 1
        used_plays.add(a.play_id)

    schedule_report = []
    for s in schedules:
        got = assigned_counts.get(s.schedule_id, 0)
        schedule_report.append({
            "schedule_id": s.schedule_id,
            "material_id": s.material_id,
            "required_count": s.required_count,
            "assigned_count": got,
            "status": "fulfilled" if got >= s.required_count else "partial",
            "shortfall": max(0, s.required_count - got),
            "window": [s.win_start, s.win_end],
        })
    leftovers = [{"play_id": p["play_id"], "material_id": p["material_id"],
                  "prog_start": p["prog_start"], "prog_end": p["prog_end"],
                  "reason": "no_eligible_schedule_or_outcompeted"}
                 for p in plays if p["play_id"] not in used_plays]
    return {
        "allocations": [asdict(a) for a in allocations],
        "schedule_report": schedule_report,
        "leftover_plays": leftovers,
        "diagnostics": {
            "n_confirmed_plays": len(plays),
            "n_schedules": len(schedules),
            "total_required": max_demand,
            "total_satisfied": best_flow,
            "total_doubled_distance": best_cost,
            "lex_forced_pairs": [list(x) for x in forced],
            "objectives": ["max_satisfied", "min_total_doubled_distance",
                           "lex_min_sorted_assignment_pairs"],
        },
    }
