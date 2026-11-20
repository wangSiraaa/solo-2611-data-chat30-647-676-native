"""统一命令行入口。

星表服务：
  catalog-init / catalog-import / catalog-replace / catalog-withdraw /
  catalog-show / catalog-releases / catalog-release / catalog-query / catalog-snapshot
匹配仓：
  wh-init / wh-import-exposure / wh-load-snapshot / wh-match / wh-runs /
  wh-show-run / wh-explain
验证：
  verify（小规模独立枚举 + 手算速度传播 + 场景回归 + 规模冒烟）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

import numpy as np

from . import __version__
from .catalog import CatalogService
from .matcher import (
    brute_force_optimal, build_candidates, prepare_detections, prepare_stars,
    solve_matching,
)
from .snapshot import snapshot_from_jsonable
from .vectors import normalize, propagate
from .warehouse import ExposureInput, MatchWarehouse


# --------------------------------------------------------------------- io utils
def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _star_lines(path: str) -> List[Dict[str, Any]]:
    payload = _read_json(path)
    if isinstance(payload, dict) and "stars" in payload:
        payload = payload["stars"]
    if not isinstance(payload, list):
        raise SystemExit(f"{path}: 星表 JSON 应为行数组或含 stars 字段的对象")
    return payload


# --------------------------------------------------------------------- catalog
def cmd_catalog_init(args: argparse.Namespace) -> None:
    svc = CatalogService(args.db)
    _print({"ok": True, "db": args.db, "schema": "catalog/v1", "version": __version__})
    svc.close()


def cmd_catalog_import(args: argparse.Namespace) -> None:
    with CatalogService(args.db) as svc:
        stats = svc.import_lines(_star_lines(args.file))
    _print({"ok": True, "imported": stats})


def cmd_catalog_replace(args: argparse.Namespace) -> None:
    payload = _read_json(args.file)
    with CatalogService(args.db) as svc:
        n = 0
        for line in payload if isinstance(payload, list) else payload["stars"]:
            svc.replace_star(line["star_id"], int(line["t0"]), line["r"], line["v"])
            n += 1
    _print({"ok": True, "replaced": n})


def cmd_catalog_withdraw(args: argparse.Namespace) -> None:
    with CatalogService(args.db) as svc:
        svc.withdraw_star(args.star_id)
    _print({"ok": True, "withdrawn": args.star_id})


def cmd_catalog_show(args: argparse.Namespace) -> None:
    with CatalogService(args.db) as svc:
        if args.star_id:
            rec = svc.get_star(args.star_id)
            if rec is None:
                raise SystemExit(f"star_id 不存在: {args.star_id}")
            out = {"star_id": rec.star_id, "t0": rec.t0, "status": rec.status,
                   "r": list(map(float, rec.r)), "v": list(map(float, rec.v))}
        else:
            out = [
                {"star_id": r.star_id, "t0": r.t0, "status": r.status,
                 "r": list(map(float, r.r)), "v": list(map(float, r.v))}
                for r in svc.list_active_stars()
            ]
    _print(out)


def cmd_catalog_release(args: argparse.Namespace) -> None:
    with CatalogService(args.db) as svc:
        info = svc.release(args.release_id, note=args.note)
    _print({"ok": True, **info})


def cmd_catalog_releases(args: argparse.Namespace) -> None:
    with CatalogService(args.db) as svc:
        _print(svc.list_releases())


def cmd_catalog_query(args: argparse.Namespace) -> None:
    """普通用户：不导入曝光也能取某历元完整方向表与来源版本。"""
    with CatalogService(args.db) as svc:
        table = svc.epoch_directions(args.epoch, release_id=args.release)
    payload = {
        "epoch": table["epoch"],
        "source_release": table["release_id"] or "working-set(未发布)",
        "n_available": len(table["available"]),
        "unavailable_star_ids": table["unavailable"],
        "directions": [
            {"star_id": row["star_id"], "direction": [float(x) for x in row["direction"]]}
            for row in table["available"]
        ],
    }
    if args.out:
        _write_json(args.out, payload)
        _print({"ok": True, "written": args.out, "n_available": len(table["available"]),
                "unavailable": table["unavailable"]})
    else:
        _print(payload)


def cmd_catalog_snapshot(args: argparse.Namespace) -> None:
    with CatalogService(args.db) as svc:
        snap, unavailable = svc.export_snapshot(args.epoch, args.release, unit_tol=args.unit_tol)
    if args.out:
        _write_json(args.out, snap.to_jsonable())
    _print({"ok": True, "snapshot_id": snap.snapshot_id, "epoch": snap.epoch,
            "source": snap.source, "release": snap.catalog_release,
            "n_stars": len(snap.entries), "unavailable": unavailable,
            "written": args.out})


# ------------------------------------------------------------------- warehouse
def cmd_wh_init(args: argparse.Namespace) -> None:
    wh = MatchWarehouse(args.db)
    _print({"ok": True, "db": args.db, "schema": "warehouse/v1"})
    wh.close()


def cmd_wh_import_exposure(args: argparse.Namespace) -> None:
    payload = _read_json(args.file)
    if "exposure_id" in payload:
        payload = [payload]
    with MatchWarehouse(args.db) as wh:
        n = 0
        for item in payload:
            wh.import_exposure(ExposureInput(
                exposure_id=item["exposure_id"], epoch=int(item["epoch"]),
                detections=item["detections"], unit_tol=float(item.get("unit_tol", 1e-9))))
            n += 1
    _print({"ok": True, "exposures_imported": n})


def cmd_wh_load_snapshot(args: argparse.Namespace) -> None:
    """消费外部同格式快照（不读星表库），落库封存并打印 snapshot_id。"""
    payload = _read_json(args.file)
    snap = snapshot_from_jsonable(payload)
    with MatchWarehouse(args.db) as wh:
        with wh.conn:
            wh._store_snapshot(snap)
    _print({"ok": True, "snapshot_id": snap.snapshot_id, "source": snap.source,
            "n_stars": len(snap.entries), "epoch": snap.epoch})


def _resolve_snapshot(wh: MatchWarehouse, args: argparse.Namespace):
    if args.snapshot_id:
        return wh.load_snapshot(args.snapshot_id)
    if args.snapshot_file:
        return snapshot_from_jsonable(_read_json(args.snapshot_file))
    if args.catalog_db and args.release:
        with CatalogService(args.catalog_db) as svc:
            epoch = wh._require_epoch(args.exposure_id)
            snap, unavailable = svc.export_snapshot(epoch, args.release)
        if unavailable:
            print(f"[warn] 快照剔除零向量不可用星: {unavailable}", file=sys.stderr)
        return snap
    raise SystemExit("需提供 --snapshot-id / --snapshot-file / (--catalog-db + --release) 之一")


def cmd_wh_match(args: argparse.Namespace) -> None:
    with MatchWarehouse(args.db) as wh:
        snap = _resolve_snapshot(wh, args)
        run = wh.run_matching(args.exposure_id, snap,
                              threshold=args.threshold, eps=args.eps)
    _print(_run_summary(run))


def _run_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": run["run_id"], "exposure_id": run["exposure_id"], "seq": run["seq"],
        "snapshot_id": run["snapshot_id"],
        "threshold": run["threshold"], "eps": run["eps"],
        "n_matches": run["n_matches"], "n_candidates": run["n_candidates"],
        "min_cost_C": run["min_cost"], "total_cost": run["total_cost"],
        "matches": run["matches"],
        "unmatched_detections": run["unmatched_detections"],
        "unmatched_stars": run["unmatched_stars"],
        "created_at": run["created_at"],
    }


def cmd_wh_runs(args: argparse.Namespace) -> None:
    with MatchWarehouse(args.db) as wh:
        rows = wh.list_runs(args.exposure_id)
    _print([{k: r[k] for k in
             ("run_id", "exposure_id", "seq", "snapshot_id", "n_matches",
              "min_cost", "total_cost", "created_at")} for r in rows])


def cmd_wh_show_run(args: argparse.Namespace) -> None:
    with MatchWarehouse(args.db) as wh:
        run = wh.get_run(args.run_id)
    out = _run_summary(run)
    out["candidates"] = run["candidates"]
    if args.full:
        out["trace"] = run["trace"]
    _print(out)


def cmd_wh_explain(args: argparse.Namespace) -> None:
    with MatchWarehouse(args.db) as wh:
        if args.star_id and args.detection_id:
            _print(wh.explain_match(args.run_id, args.star_id, args.detection_id))
        else:
            _print(wh.explain_run(args.run_id))


# ---------------------------------------------------------------------- verify
def _verify_brute_force() -> List[Dict[str, Any]]:
    """随机小例（<=5 星 5 点）：主求解器 vs 独立全枚举，逐例核对。"""
    from .synthetic import _rng
    rng = _rng(777)
    results = []
    for case in range(30):
        ns = int(rng.integers(1, 6))
        nd = int(rng.integers(1, 6))
        stars = [f"S{i}" for i in range(ns)]
        dets = [f"D{j}" for j in range(nd)]
        sdirs = rng.normal(size=(ns, 3))
        sdirs /= np.linalg.norm(sdirs, axis=1, keepdims=True)
        ddirs = rng.normal(size=(nd, 3))
        ddirs /= np.linalg.norm(ddirs, axis=1, keepdims=True)
        threshold = float(rng.uniform(0.5, 2.2))
        eps = float(rng.choice([0.0, 1e-12, 1e-9]))
        cands = build_candidates(stars, sdirs, dets, ddirs, threshold)
        outcome = solve_matching("case", stars, dets, cands, eps=eps, threshold=threshold)
        brute = brute_force_optimal(stars, dets, cands, eps=eps)
        ok_k = outcome.max_cardinality == brute["k"]
        ok_pairs = outcome.match_pairs == brute["pairs"]
        ok_cost = abs(outcome.total_cost - brute["cost"]) <= 1e-12
        results.append({"case": case, "n_stars": ns, "n_dets": nd, "eps": eps,
                        "K": brute["k"], "brute_force_match": True if (ok_k and ok_pairs and ok_cost)
                        else {"ok_k": ok_k, "ok_pairs": ok_pairs, "ok_cost": ok_cost}})
        assert ok_k and ok_pairs and ok_cost, (case, outcome.match_pairs, brute)
    return results


def _verify_hand_propagation() -> Dict[str, Any]:
    """手算一条速度传播并断言最近关系翻转（数值与手工推导对照）。"""
    r1 = [1.0, 0.0, 0.0]
    r2 = [1.0, 0.030, 0.0]
    v2 = [0.0, -0.060, 0.0]
    u1_t0 = propagate(np.array(r1), np.zeros(3), 0, 0)
    u2_t0 = propagate(np.array(r2), np.array(v2), 0, 0)
    u2_t1 = propagate(np.array(r2), np.array(v2), 1, 0)
    d = normalize(np.array([1.0, -0.020, 0.0]))
    from .vectors import angle_between
    a = {
        "t0_angle_S1_D": angle_between(u1_t0, d),
        "t0_angle_S2_D": angle_between(u2_t0, d),
        "t1_angle_S1_D": angle_between(u1_t0, d),
        "t1_angle_S2_D": angle_between(u2_t1, d),
        "s2_position_t1": [1.0, -0.030, 0.0],
        "s2_direction_t1": [float(x) for x in u2_t1],
    }
    a["t0_nearest"] = "S1" if a["t0_angle_S1_D"] < a["t0_angle_S2_D"] else "S2"
    a["t1_nearest"] = "S1" if a["t1_angle_S1_D"] < a["t1_angle_S2_D"] else "S2"
    a["flipped"] = a["t0_nearest"] != a["t1_nearest"]
    assert a["flipped"], a
    return a


def cmd_verify(args: argparse.Namespace) -> None:
    from . import synthetic as syn
    report: Dict[str, Any] = {"version": __version__}

    # 1) 独立枚举核对
    report["brute_force_cases"] = _verify_brute_force()

    # 2) 手算速度传播
    report["hand_propagation"] = _verify_hand_propagation()

    # 3) 场景回归（内存内直接跑引擎）
    scenario_results: Dict[str, Any] = {}
    # 相邻竞争：题面禁止的“逐点最近星”会把两个亮点重复判给同一颗星
    sc = syn.scenario_adjacent_competition()
    star_ids, star_dirs = prepare_stars(
        [{"star_id": s["star_id"], "direction":
          normalize(np.array(s["r"]) + 0 * np.array(s["v"]))} for s in sc["stars"]],
        unit_tol=1e-9)
    det_ids, det_dirs, _ = prepare_detections(sc["exposure"]["detections"], unit_tol=1e-9)
    cands = build_candidates(star_ids, star_dirs, det_ids, det_dirs, sc["threshold"])
    out = solve_matching(sc["exposure"]["exposure_id"], star_ids, det_ids, cands,
                         eps=sc["eps"], threshold=sc["threshold"])
    # 被禁止的朴素做法：每个亮点独立取最近星（不做一次性约束，允许重复占用）
    greedy_dup = _naive_per_detection_nearest(
        star_ids, star_dirs, det_ids, det_dirs, sc["threshold"])
    dup_stars = [s for s in {p[0] for p in greedy_dup}
                 if sum(1 for p in greedy_dup if p[0] == s) > 1]
    scenario_results["adjacent_competition"] = {
        "optimal_pairs": out.match_pairs, "K": out.max_cardinality,
        "per_detection_nearest_duplicates": greedy_dup,
        "duplicated_star_in_naive_greedy": dup_stars,
    }
    assert dup_stars == ["S1"]                      # 两亮点都被贪心判给 S1
    assert out.max_cardinality == 2
    assert out.match_pairs == [("S1", "D1"), ("S2", "D2")]

    # 单星多亮点
    sc = syn.scenario_one_star_many_detections()
    star_ids, star_dirs = prepare_stars(
        [{"star_id": "S1", "direction": normalize(np.array([1.0, 0.0, 0.0]))}], unit_tol=1e-9)
    det_ids, det_dirs, _ = prepare_detections(sc["exposure"]["detections"], unit_tol=1e-9)
    cands = build_candidates(star_ids, star_dirs, det_ids, det_dirs, sc["threshold"])
    out = solve_matching("E", star_ids, det_ids, cands, eps=sc["eps"], threshold=sc["threshold"])
    scenario_results["one_star_many_detections"] = {
        "pairs": out.match_pairs, "unmatched_detections": out.unmatched_detections}
    assert out.match_pairs == [("S1", "D1")] and out.unmatched_detections == ["D2"]

    # 无候选
    sc = syn.scenario_no_candidate()
    star_ids, star_dirs = prepare_stars(
        [{"star_id": "S1", "direction": [1.0, 0.0, 0.0]}], unit_tol=1e-9)
    det_ids, det_dirs, _ = prepare_detections(sc["exposure"]["detections"], unit_tol=1e-9)
    cands = build_candidates(star_ids, star_dirs, det_ids, det_dirs, sc["threshold"])
    out = solve_matching("E", star_ids, det_ids, cands, eps=sc["eps"], threshold=sc["threshold"])
    scenario_results["no_candidate"] = {"K": out.max_cardinality,
                                        "unmatched_detections": out.unmatched_detections}
    assert out.max_cardinality == 0

    # 零向量（历元方向表层面）
    sc = syn.scenario_zero_vector()
    with CatalogService(":memory:") as svc:
        for s in sc["stars"]:
            svc.add_star(s["star_id"], s["t0"], s["r"], s["v"])
        svc.release("R-ZERO")
        table = svc.epoch_directions(1, release_id="R-ZERO")
    scenario_results["zero_vector"] = {"unavailable": table["unavailable"]}
    assert table["unavailable"] == ["S2"]

    # eps 带字典序：构造等角代价的对称小例
    sdirs = np.array([[1.0, 0.0, 0.0],
                      normalize(np.array([1.0, 0.01, 0.0]))])
    ddirs = np.array([normalize(np.array([1.0, 0.0, 0.0])),
                      normalize(np.array([1.0, 0.01, 0.0]))])
    cands = build_candidates(["S1", "S2"], sdirs, ["D1", "D2"], ddirs, threshold=0.05)
    out0 = solve_matching("eps", ["S1", "S2"], ["D1", "D2"], cands, eps=0.0, threshold=0.05)
    brute0 = brute_force_optimal(["S1", "S2"], ["D1", "D2"], cands, eps=0.0)
    scenario_results["eps_lex_tie"] = {"pairs": out0.match_pairs,
                                       "brute_force_pairs": brute0["pairs"]}
    assert out0.match_pairs == brute0["pairs"]

    report["scenarios"] = scenario_results

    # 4) 规模冒烟（不建库，只跑引擎时间与结果健全性）
    import time
    rows = syn.build_scale_catalog()
    exposures = syn.build_scale_exposures(rows)
    t0 = time.perf_counter()
    n_matches_total = 0
    for exp in exposures[:3]:
        epoch = exp["epoch"]
        star_rows = []
        for s in rows:
            u = propagate(np.array(s["r"]), np.array(s["v"]), epoch, s["t0"])
            if u is not None:
                star_rows.append({"star_id": s["star_id"], "direction": u})
        sids, smat = prepare_stars(star_rows, unit_tol=1e-9)
        dids, dmat, _ = prepare_detections(exp["detections"], unit_tol=exp["unit_tol"])
        cands = build_candidates(sids, smat, dids, dmat, threshold=0.02)
        out = solve_matching(exp["exposure_id"], sids, dids, cands, eps=1e-12, threshold=0.02)
        n_matches_total += len(out.matches)
        assert len(set(out.match_pairs)) == len(out.match_pairs)
        used_s = {p[0] for p in out.match_pairs}
        used_d = {p[1] for p in out.match_pairs}
        assert len(used_s) == len(out.match_pairs) and len(used_d) == len(out.match_pairs)
        assert out.total_cost <= out.min_cost + 1e-12 + 1e-12
    report["scale_smoke"] = {
        "stars": syn.SCALE_STARS, "exposures": syn.SCALE_EXPOSURES,
        "detections_per_exposure": syn.DETECTIONS_PER_EXPOSURE,
        "exposures_run": 3, "matches_in_3_exposures": n_matches_total,
        "elapsed_sec": round(time.perf_counter() - t0, 3),
    }
    report["ok"] = True
    _print(report)


def _naive_per_detection_nearest(star_ids, star_dirs, det_ids, det_dirs, threshold):
    """题面禁止的朴素做法：每个亮点独立找最近星，允许同一颗星被重复选中。

    仅在 verify 中作反例对照，证明“逐点最近 + 删除重复”不能替代全局匹配。
    """
    from .vectors import angle_matrix
    ang = angle_matrix(star_dirs, det_dirs)
    pairs = []
    for j in range(len(det_ids)):
        i = int(np.argmin(ang[:, j]))
        if ang[i, j] <= threshold:
            pairs.append((star_ids[i], det_ids[j]))
    return sorted(pairs)


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="astromatch", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("catalog-init")
    sp.add_argument("--db", required=True)
    sp.set_defaults(func=cmd_catalog_init)

    sp = sub.add_parser("catalog-import")
    sp.add_argument("--db", required=True)
    sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_catalog_import)

    sp = sub.add_parser("catalog-replace")
    sp.add_argument("--db", required=True)
    sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_catalog_replace)

    sp = sub.add_parser("catalog-withdraw")
    sp.add_argument("--db", required=True)
    sp.add_argument("--star-id", required=True)
    sp.set_defaults(func=cmd_catalog_withdraw)

    sp = sub.add_parser("catalog-show")
    sp.add_argument("--db", required=True)
    sp.add_argument("--star-id")
    sp.set_defaults(func=cmd_catalog_show)

    sp = sub.add_parser("catalog-release")
    sp.add_argument("--db", required=True)
    sp.add_argument("--release-id", required=True)
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_catalog_release)

    sp = sub.add_parser("catalog-releases")
    sp.add_argument("--db", required=True)
    sp.set_defaults(func=cmd_catalog_releases)

    sp = sub.add_parser("catalog-query")
    sp.add_argument("--db", required=True)
    sp.add_argument("--epoch", type=int, required=True)
    sp.add_argument("--release")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_catalog_query)

    sp = sub.add_parser("catalog-snapshot")
    sp.add_argument("--db", required=True)
    sp.add_argument("--epoch", type=int, required=True)
    sp.add_argument("--release", required=True)
    sp.add_argument("--unit-tol", type=float, default=1e-9)
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_catalog_snapshot)

    sp = sub.add_parser("wh-init")
    sp.add_argument("--db", required=True)
    sp.set_defaults(func=cmd_wh_init)

    sp = sub.add_parser("wh-import-exposure")
    sp.add_argument("--db", required=True)
    sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_wh_import_exposure)

    sp = sub.add_parser("wh-load-snapshot")
    sp.add_argument("--db", required=True)
    sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_wh_load_snapshot)

    sp = sub.add_parser("wh-match")
    sp.add_argument("--db", required=True)
    sp.add_argument("--exposure-id", required=True)
    sp.add_argument("--threshold", type=float, required=True)
    sp.add_argument("--eps", type=float, required=True)
    sp.add_argument("--snapshot-id")
    sp.add_argument("--snapshot-file")
    sp.add_argument("--catalog-db")
    sp.add_argument("--release")
    sp.set_defaults(func=cmd_wh_match)

    sp = sub.add_parser("wh-runs")
    sp.add_argument("--db", required=True)
    sp.add_argument("--exposure-id")
    sp.set_defaults(func=cmd_wh_runs)

    sp = sub.add_parser("wh-show-run")
    sp.add_argument("--db", required=True)
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--full", action="store_true")
    sp.set_defaults(func=cmd_wh_show_run)

    sp = sub.add_parser("wh-explain")
    sp.add_argument("--db", required=True)
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--star-id")
    sp.add_argument("--detection-id")
    sp.set_defaults(func=cmd_wh_explain)

    sp = sub.add_parser("verify")
    sp.set_defaults(func=cmd_verify)
    return p


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except Exception as exc:  # 统一报错出口，事务回滚由 with 保证
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
