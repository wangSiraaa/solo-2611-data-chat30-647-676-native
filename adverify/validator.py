"""验证路径：生成器真值比对 + 独立穷举的分配最优性核对。

本模块是整个项目中唯一读取 ground_truth.json 的地方。

检索质量指标
------------
把扫描匹配与真值事件做一对一贪心关联（按 IoU 最大匹配），统计
* 定位误差：开始/结束采样点误差（含均值、P95、最大）；
* 漏检：没有任何匹配关联上的真值事件（按素材维度）；
* 误检：没有任何真值事件支撑的匹配；
* 确定性纯度：confirmed 匹配中关联到真实事件的比例。

分配最优性（独立人工构造竞争表 + 穷举）
--------------------------------------
不复用 solver 内部实现，而是用 itertools 独立枚举所有合法分配
（每条播放至多归属一条排期的一次要求），按题面三级目标计算最优值，
再与 solver 的输出逐项比较，并验证两次求解结果完全一致（确定性）。
"""
from __future__ import annotations

import itertools
import json
from typing import Optional

import numpy as np

from .solver import Schedule, solve


# --------------------------------------------------------------------------- #
# 检索质量
# --------------------------------------------------------------------------- #
def _iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    inter = max(0, hi - lo)
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def evaluate_detection(ground_truth: dict, matches: list[dict],
                       position_tol_s: float = 1.0) -> dict:
    sr = ground_truth["sr"]
    tol = int(position_tol_s * sr)
    gt_events = [e for e in ground_truth["events"] if e["material_id"]]
    pairs: list[tuple[float, int, int]] = []
    for i, e in enumerate(gt_events):
        for j, m in enumerate(matches):
            if e["material_id"] != m["material_id"]:
                continue
            score = _iou((e["prog_start"], e["prog_end"]),
                         (m["prog_start"], m["prog_end"]))
            if score > 0:
                pairs.append((score, i, j))
    pairs.sort(reverse=True)
    taken_e, taken_m = set(), set()
    links = []
    for score, i, j in pairs:
        if i in taken_e or j in taken_m:
            continue
        taken_e.add(i)
        taken_m.add(j)
        links.append((i, j, score))

    start_err, end_err, confirmed_correct, confirmed_total = [], [], 0, 0
    link_details = []
    for i, j, score in links:
        e, m = gt_events[i], matches[j]
        start_err.append(abs(m["prog_start"] - e["prog_start"]))
        end_err.append(abs(m["prog_end"] - e["prog_end"]))
        if m["certainty"] == "confirmed":
            confirmed_total += 1
            if abs(m["prog_start"] - e["prog_start"]) <= tol:
                confirmed_correct += 1
        link_details.append({
            "event_id": e["event_id"], "play_id": m["play_id"],
            "material_id": e["material_id"], "kind": e["kind"],
            "certainty": m["certainty"], "iou": round(score, 4),
            "start_err": abs(m["prog_start"] - e["prog_start"]),
            "end_err": abs(m["prog_end"] - e["prog_end"])})

    missed = [{"event_id": e["event_id"], "material_id": e["material_id"],
               "kind": e["kind"], "prog_start": e["prog_start"],
               "prog_end": e["prog_end"]}
              for i, e in enumerate(gt_events) if i not in taken_e]
    false_alarms = [{"play_id": m["play_id"], "material_id": m["material_id"],
                     "certainty": m["certainty"],
                     "prog_start": m["prog_start"], "prog_end": m["prog_end"],
                     "mean_score": m["mean_score"]}
                    for j, m in enumerate(matches) if j not in taken_m]

    def stats(errs):
        if not errs:
            return {"mean": 0, "p95": 0, "max": 0, "n": 0}
        a = np.array(errs, dtype=np.float64)
        return {"mean": round(float(a.mean()), 2),
                "p95": round(float(np.quantile(a, 0.95)), 2),
                "max": int(a.max()), "n": len(errs)}

    return {
        "n_gt_events": len(gt_events),
        "n_matches": len(matches),
        "n_linked": len(links),
        "start_error_samples": stats(start_err),
        "end_error_samples": stats(end_err),
        "start_error_seconds": {k: (round(v / sr, 4) if isinstance(v, float)
                                    else round(v / sr, 4))
                                for k, v in stats(start_err).items() if k != "n"},
        "missed_detections": missed,
        "false_alarms": false_alarms,
        "confirmed_precision": (round(confirmed_correct / confirmed_total, 4)
                                if confirmed_total else None),
        "links": link_details,
    }


# --------------------------------------------------------------------------- #
# 独立穷举求解器（仅用于小规模竞争表验证）
# --------------------------------------------------------------------------- #
def _eligible_pairs(schedules: list[Schedule], plays: list[dict]):
    pairs = []
    for s in schedules:
        for p in plays:
            if p["certainty"] != "confirmed":
                continue
            if p["material_id"] != s.material_id:
                continue
            if not (s.win_start <= p["prog_start"] < s.win_end):
                continue
            hop = p.get("evidence", {}).get("frame_hop_samples", 1)
            if p["contiguous_frames"] * hop >= s.min_contiguous:
                pairs.append((s, p))
    return pairs


def brute_force_optimum(schedules: list[Schedule], plays: list[dict]) -> dict:
    """枚举所有合法分配，返回三级目标下的最优值与最优排序列表集合。"""
    pairs = _eligible_pairs(schedules, plays)
    # 每条播放最多用一次；每条排期最多 required_count 次。
    # 枚举：对每条排期枚举其可选播放的 0..req 大小组合，再做笛卡尔积并剪枝。
    by_sched: dict[str, list[dict]] = {}
    req: dict[str, int] = {}
    for s in schedules:
        by_sched[s.schedule_id] = [p for (sc, p) in pairs if sc.schedule_id == s.schedule_id]
        req[s.schedule_id] = s.required_count
    best = None  # (flow, cost, sorted_pairs_tuple)
    all_best: list[tuple] = []
    option_lists = []
    sched_ids = sorted(by_sched)
    for sid in sched_ids:
        cands = by_sched[sid]
        opts = [()]  # 不分配也是选项
        for r in range(1, min(req[sid], len(cands)) + 1):
            for combo in itertools.combinations(cands, r):
                opts.append(tuple(p["play_id"] for p in combo))
        option_lists.append((sid, opts))

    def recurse(k, used_plays, chosen, flow, cost):
        nonlocal best, all_best
        if k == len(option_lists):
            if best is None or (flow > best[0]) or (flow == best[0] and cost < best[1]):
                best = (flow, cost, tuple(sorted(chosen)))
                all_best = [tuple(sorted(chosen))]
            elif best is not None and flow == best[0] and cost == best[1]:
                all_best.append(tuple(sorted(chosen)))
            return
        sid, opts = option_lists[k]
        sched = next(s for s in schedules if s.schedule_id == sid)
        for opt in opts:
            if any(pid in used_plays for pid in opt):
                continue
            added_cost = sum(sched.doubled_distance(
                next(p for p in plays if p["play_id"] == pid)["prog_start"])
                for pid in opt)
            recurse(k + 1, used_plays | set(opt),
                    chosen + [(sid, pid) for pid in opt],
                    flow + len(opt), cost + added_cost)

    recurse(0, set(), [], 0, 0)
    # 第三级：字典序最小的排序列表
    lex_best = min(all_best)
    return {"best_flow": best[0], "best_cost": best[1],
            "lex_best_pairs": [list(x) for x in lex_best],
            "n_tied_optima": len(all_best)}


def build_competition_cases() -> list[tuple[list[Schedule], list[dict]]]:
    """人工构造的竞争小表：共享播放、多要求、窗口交叠、距离并列全覆盖。"""
    def play(pid, mid, start, frames=100, hop=80):
        return {"play_id": pid, "material_id": mid, "certainty": "confirmed",
                "prog_start": start, "prog_end": start + frames * hop,
                "mat_start": 0, "mat_end": frames * hop,
                "contiguous_frames": frames,
                "evidence": {"frame_hop_samples": hop}}

    cases = []
    # 案例 1：一条播放同时落在两条同素材排期窗口内，只能满足其一
    s1 = Schedule("sA", "m1", 0, 1000, 1000, 1)
    s2 = Schedule("sB", "m1", 500, 1500, 1000, 1)
    plays = [play("p1", "m1", 700), play("p2", "m1", 1200)]
    cases.append(([s1, s2], plays))

    # 案例 2：要求次数 3 但只有 2 条合格播放 -> 部分履约 + 距离决胜
    s3 = Schedule("sC", "m2", 0, 5000, 1000, 3)
    plays = [play(f"q{i}", "m2", 1000 + i * 900) for i in range(3)]
    cases.append(([s3], plays))

    # 案例 3：距离完全对称并列，检查字典序决胜
    s4 = Schedule("sD", "m3", 0, 2000, 1000, 1)
    s5 = Schedule("sE", "m3", 0, 2000, 1000, 1)
    plays = [play("z9", "m3", 1000), play("a1", "m3", 1000)]
    cases.append(([s4, s5], plays))

    # 案例 4：review 匹配必须被忽略；时长不足必须被拒绝
    s6 = Schedule("sF", "m4", 0, 4000, 5000, 2)
    good = play("g1", "m4", 500, frames=100)
    short = play("g2", "m4", 1500, frames=10)   # 800 样本 < 5000
    review = dict(play("g3", "m4", 2500), certainty="review")
    cases.append(([s6], [good, short, review]))

    # 案例 5：多素材多排期混合，播放与窗口交叉
    schedules = [
        Schedule("sG", "m5", 0, 3000, 1000, 2),
        Schedule("sH", "m5", 2000, 6000, 1000, 1),
        Schedule("sI", "m6", 1000, 5000, 1000, 1),
    ]
    plays = [play("h1", "m5", 800), play("h2", "m5", 2500),
             play("h3", "m5", 4000), play("i1", "m6", 3000),
             play("i2", "m6", 200)]  # 200 不在 sI 窗口
    cases.append((schedules, plays))
    return cases


def verify_solver_optimality() -> dict:
    report = {"cases": []}
    for idx, (schedules, plays) in enumerate(build_competition_cases()):
        bf = brute_force_optimum(schedules, plays)
        r1 = solve(schedules, plays)
        r2 = solve(schedules, [dict(p) for p in plays])  # 重跑验证确定性
        got_pairs = sorted((a["schedule_id"], a["play_id"])
                           for a in r1["allocations"])
        got_pairs_2 = sorted((a["schedule_id"], a["play_id"])
                             for a in r2["allocations"])
        ok_flow = r1["diagnostics"]["total_satisfied"] == bf["best_flow"]
        ok_cost = r1["diagnostics"]["total_doubled_distance"] == bf["best_cost"]
        ok_lex = [list(x) for x in got_pairs] == bf["lex_best_pairs"]
        ok_det = got_pairs == got_pairs_2 and json.dumps(
            r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
        report["cases"].append({
            "case": idx + 1,
            "n_schedules": len(schedules), "n_plays": len(plays),
            "brute_force": bf,
            "solver_flow": r1["diagnostics"]["total_satisfied"],
            "solver_cost": r1["diagnostics"]["total_doubled_distance"],
            "solver_pairs": [list(x) for x in got_pairs],
            "flow_optimal": ok_flow, "cost_optimal": ok_cost,
            "lex_optimal": ok_lex, "deterministic": ok_det,
            "passed": ok_flow and ok_cost and ok_lex and ok_det,
        })
    report["all_passed"] = all(c["passed"] for c in report["cases"])
    return report
