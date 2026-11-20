"""端到端可复跑演示：规模集 + 全部题面场景 + 持久化/复读/旧run不变验证。

用法：python -m astromatch.demo --data-dir data
全程本地合成、无外部网络；结尾会删除原始输入 JSON 后再从 SQLite 复读结果。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from typing import Any, Dict, List

import numpy as np

from . import synthetic as syn
from .catalog import CatalogService
from .matcher import build_candidates, prepare_detections, prepare_stars, solve_matching
from .snapshot import snapshot_from_jsonable
from .vectors import angle_between, normalize, propagate
from .warehouse import ExposureInput, MatchWarehouse

THRESHOLD_SCALE = 0.02
EPS = 1e-12


def _w(title: str) -> None:
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def _dump(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def main(data_dir: str = "data") -> Dict[str, Any]:
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    inputs = os.path.join(data_dir, "inputs")
    os.makedirs(inputs, exist_ok=True)
    report: Dict[str, Any] = {"checks": []}

    def check(name: str, cond: bool, detail: Any = None) -> None:
        if not cond:
            raise AssertionError(f"检查失败: {name} :: {detail}")
        report["checks"].append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail is not None else ""))

    # ============================================================ 1. 规模集
    _w("1. 规模集：200 星 / 20 曝光 / 每曝光 100 亮点")
    cat_rows = syn.build_scale_catalog()
    exposures = syn.build_scale_exposures(cat_rows)
    _dump(os.path.join(inputs, "catalog_200.json"), cat_rows)
    _dump(os.path.join(inputs, "exposures_20.json"), exposures)

    cat_db = os.path.join(data_dir, "catalog.sqlite")
    wh_db = os.path.join(data_dir, "warehouse.sqlite")
    with CatalogService(cat_db) as cat:
        cat.import_lines(cat_rows)
        rel = cat.release("R-2026A", note="200星初始固定版本")
        print(f"  发布 {rel['release_id']}，条目数 {rel['n_entries']}")
        check("scale catalog release 200", rel["n_entries"] == 200, rel["n_entries"])

    run_summaries: List[Dict[str, Any]] = []
    with MatchWarehouse(wh_db) as wh:
        for exp in exposures:
            wh.import_exposure(ExposureInput(
                exposure_id=exp["exposure_id"], epoch=exp["epoch"],
                detections=exp["detections"], unit_tol=exp["unit_tol"]))
        with CatalogService(cat_db) as cat:
            for exp in exposures:
                snap, unavailable = cat.export_snapshot(exp["epoch"], "R-2026A")
                assert not unavailable
                run = wh.run_matching(exp["exposure_id"], snap,
                                      threshold=THRESHOLD_SCALE, eps=EPS)
                run_summaries.append({
                    "run_id": run["run_id"], "exposure_id": run["exposure_id"],
                    "n_matches": run["n_matches"], "n_candidates": run["n_candidates"],
                    "total_cost": round(run["total_cost"], 10)})
    total_matches = sum(r["n_matches"] for r in run_summaries)
    print("  前 3 个 run:", json.dumps(run_summaries[:3], ensure_ascii=False))
    print(f"  20 张曝光合计匹配 {total_matches} 条；每曝光候选边 "
          f"{min(r['n_candidates'] for r in run_summaries)}.."
          f"{max(r['n_candidates'] for r in run_summaries)}")
    check("scale all 20 runs created", len(run_summaries) == 20)
    check("scale matches within detection budget",
          all(r["n_matches"] <= 100 for r in run_summaries))

    # ======================================== 2. 手算速度传播 + 星表更新翻转
    _w("2. 手算速度传播与真实星表更新改变匹配（旧 run 不变）")
    flip = syn.scenario_velocity_flip()
    small_cat = os.path.join(data_dir, "catalog_small.sqlite")
    small_wh = os.path.join(data_dir, "warehouse_small.sqlite")
    with CatalogService(small_cat) as cat:
        for s in flip["stars"]:
            cat.add_star(s["star_id"], s["t0"], s["r"], s["v"])
        cat.release("R0", note="翻转前固定版本")

    # 手工传播数值（与 synthetic.scenario_velocity_flip 同模型，独立算一遍）
    r1, r2 = np.array([1.0, 0.0, 0.0]), np.array([1.0, 0.030, 0.0])
    v2 = np.array([0.0, -0.060, 0.0])
    d = normalize(np.array([1.0, -0.020, 0.0]))
    u2_t0, u2_t1 = propagate(r2, v2, 0, 0), propagate(r2, v2, 1, 0)
    print(f"  S2 t=0 位置={r2} 方向={np.round(u2_t0, 6)}")
    print(f"  S2 t=1 位置=r+1*v={r2 + v2} 方向={np.round(u2_t1, 6)}")
    print(f"  夹角 t=0: S1-D={angle_between(r1, d):.6f}  S2-D={angle_between(u2_t0, d):.6f}")
    print(f"  夹角 t=1: S1-D={angle_between(r1, d):.6f}  S2-D={angle_between(u2_t1, d):.6f}")
    check("hand propagation S2 t1 position",
          np.allclose(r2 + v2, [1.0, -0.030, 0.0]))
    check("hand propagation nearest flips",
          angle_between(r1, d) < angle_between(u2_t0, d)
          and angle_between(u2_t1, d) < angle_between(r1, d))

    exp0 = flip["exposure_before"]
    exp1 = flip["exposure_after"]
    _dump(os.path.join(inputs, "exp_flip_t0.json"), exp0)
    _dump(os.path.join(inputs, "exp_flip_t1.json"), exp1)
    with MatchWarehouse(small_wh) as wh:
        wh.import_exposure(ExposureInput(**{k: exp0[k] for k in
                                  ("exposure_id", "epoch", "detections", "unit_tol")}))
        with CatalogService(small_cat) as cat:
            snap0, _ = cat.export_snapshot(0, "R0")
            run_t0 = wh.run_matching("E-FLIP", snap0, threshold=0.05, eps=EPS)
        t0_snapshot = run_t0["snapshot_id"]
        print(f"  t=0 run={run_t0['run_id']} 匹配={run_t0['matches']}")
        check("flip t0 matches S1", run_t0["matches"][0]["star_id"] == "S1")

        # 同曝光在 t=1 用同一发布（纯传播导致翻转）
        # E-FLIP 已按 t=0 入库；t=1 的重跑用同一 exposure_id 但需要新历元，
        # 因此另建一条同几何、不同历元的曝光记录 E-FLIP-T1。
        wh.import_exposure(ExposureInput(
            exposure_id="E-FLIP-T1", epoch=1,
            detections=exp1["detections"], unit_tol=exp1["unit_tol"]))
        with CatalogService(small_cat) as cat:
            snap1, _ = cat.export_snapshot(1, "R0")
            run_t1 = wh.run_matching("E-FLIP-T1", snap1, threshold=0.05, eps=EPS)
        t1_snapshot = run_t1["snapshot_id"]
        print(f"  t=1 run={run_t1['run_id']} 匹配={run_t1['matches']}（传播翻转）")
        check("flip t1 matches S2 via propagation",
              run_t1["matches"][0]["star_id"] == "S2")

        # 真实星表更新：替换 S2 速度后发布 R1，重跑 t=1，产生新 run
        with CatalogService(small_cat) as cat:
            cat.replace_star("S2", 0, [1.0, 0.030, 0.0], [0.0, -0.090, 0.0])
            cat.release("R1", note="S2 速度更正后固定版本")
            snap1b, _ = cat.export_snapshot(1, "R1")
            run_t1b = wh.run_matching("E-FLIP-T1", snap1b, threshold=0.05, eps=EPS)
        print(f"  t=1 更正后 run={run_t1b['run_id']} 匹配={run_t1b['matches']}，"
              f"代价 {run_t1b['total_cost']:.8f} vs 旧 {run_t1['total_cost']:.8f}")
        check("catalog update creates new run", run_t1b["run_id"] != run_t1["run_id"])
        check("new run binds new snapshot",
              run_t1b["snapshot_id"] != run_t1["snapshot_id"])

        # 旧 run 复读：必须仍绑定旧快照、旧分配
        old_again = wh.get_run(run_t1["run_id"])
        check("old run allocation unchanged after catalog correction",
              [(m["star_id"], m["detection_id"]) for m in old_again["matches"]]
              == [(m["star_id"], m["detection_id"]) for m in run_t1["matches"]]
              and old_again["snapshot_id"] == t1_snapshot)
        t0_again = wh.get_run(run_t0["run_id"])
        check("t0 run unchanged",
              [(m["star_id"], m["detection_id"]) for m in t0_again["matches"]]
              == [(m["star_id"], m["detection_id"]) for m in run_t0["matches"]]
              and t0_again["snapshot_id"] == t0_snapshot)
        report["flip_run_ids"] = [run_t0["run_id"], run_t1["run_id"], run_t1b["run_id"]]

    # ===================================================== 3. 撤回 + 版本冻结
    _w("3. 撤回场景：旧发布仍含 S2，新发布剔除，旧 run 不变")
    wd = syn.scenario_withdrawal()
    with CatalogService(small_cat) as cat:
        cat.withdraw_star(wd["withdraw"])
        rel_r2 = cat.release("R2", note="撤回 S2 后固定版本")
        r1_entries = [e.star_id for e in cat.get_release_entries("R1")]
        r2_entries = [e.star_id for e in cat.get_release_entries("R2")]
        check("withdrawn star absent from new release", "S2" not in r2_entries, r2_entries)
        check("old release still contains withdrawn star", "S2" in r1_entries, r1_entries)
    with MatchWarehouse(small_wh) as wh:
        wh.import_exposure(ExposureInput(**{k: wd["exposure"][k] for k in
                                  ("exposure_id", "epoch", "detections", "unit_tol")}))
        with CatalogService(small_cat) as cat:
            snap_old, _ = cat.export_snapshot(0, "R1")
            snap_new, _ = cat.export_snapshot(0, "R2")
            run_wd_old = wh.run_matching("E-WD", snap_old, threshold=0.05, eps=EPS)
            run_wd_new = wh.run_matching("E-WD", snap_new, threshold=0.05, eps=EPS)
        print(f"  旧发布 run={run_wd_old['run_id']} {run_wd_old['matches']}")
        print(f"  新发布 run={run_wd_new['run_id']} {run_wd_new['matches']} "
              f"未匹配星={run_wd_new['unmatched_stars']}")
        check("withdrawal changes new run",
              any(m["star_id"] == "S2" for m in run_wd_old["matches"])
              and all(m["star_id"] != "S2" for m in run_wd_new["matches"]))
        check("old withdrawal run frozen",
              [(m["star_id"], m["detection_id"]) for m in
               wh.get_run(run_wd_old["run_id"])["matches"]]
              == [(m["star_id"], m["detection_id"]) for m in run_wd_old["matches"]])

    # ======================================== 4. 零向量 / 无候选 / 相邻竞争 / 单星多点
    _w("4. 其余小场景：零向量、无候选、贪心反例、单星多亮点、亮度无关")
    zv = syn.scenario_zero_vector()
    with CatalogService(":memory:") as cat:
        for s in zv["stars"]:
            cat.add_star(s["star_id"], s["t0"], s["r"], s["v"])
        cat.release("RZ")
        snap_zv, unavailable = cat.export_snapshot(1, "RZ")
        check("zero vector excluded from snapshot", unavailable == ["S2"], unavailable)
        check("zero vector not in snapshot entries",
              all(e.star_id != "S2" for e in snap_zv.entries))

    nc = syn.scenario_no_candidate()
    sids, smat = prepare_stars(
        [{"star_id": "S1", "direction": [1.0, 0.0, 0.0]}], unit_tol=1e-9)
    dids, dmat, _ = prepare_detections(nc["exposure"]["detections"], unit_tol=1e-9)
    out_nc = solve_matching("E", sids, dids,
                            build_candidates(sids, smat, dids, dmat, nc["threshold"]),
                            eps=EPS, threshold=nc["threshold"])
    check("no candidate => K=0", out_nc.max_cardinality == 0
          and out_nc.unmatched_detections == ["D1"])

    adj = syn.scenario_adjacent_competition()
    sids, smat = prepare_stars(
        [{"star_id": s["star_id"], "direction": normalize(np.array(s["r"]))}
         for s in adj["stars"]], unit_tol=1e-9)
    dids, dmat, _ = prepare_detections(adj["exposure"]["detections"], unit_tol=1e-9)
    cands = build_candidates(sids, smat, dids, dmat, adj["threshold"])
    out_adj = solve_matching("E", sids, dids, cands, eps=EPS, threshold=adj["threshold"])
    check("adjacent competition global optimum K=2",
          out_adj.match_pairs == [("S1", "D1"), ("S2", "D2")], out_adj.match_pairs)

    osm = syn.scenario_one_star_many_detections()
    s1_dir = np.array([[1.0, 0.0, 0.0]])
    dids2, dmat2, _ = prepare_detections(osm["exposure"]["detections"], unit_tol=1e-9)
    cands2 = build_candidates(["S1"], s1_dir, dids2, dmat2, osm["threshold"])
    out_osm = solve_matching("E", ["S1"], dids2, cands2,
                             eps=EPS, threshold=osm["threshold"])
    check("one star many detections picks closer one",
          out_osm.match_pairs == [("S1", "D1")] and out_osm.unmatched_detections == ["D2"])

    # 亮度无关：换亮度分布重算，几何结果必须一致
    bi = syn.scenario_brightness_invariance()
    dids_a, dmat_a, _ = prepare_detections(bi["base"]["exposure"]["detections"], unit_tol=1e-9)
    dids_b, dmat_b, _ = prepare_detections(bi["exposure_alt"]["detections"], unit_tol=1e-9)
    out_a = solve_matching("A", sids, dids_a,
                           build_candidates(sids, smat, dids_a, dmat_a, adj["threshold"]),
                           eps=EPS, threshold=adj["threshold"])
    out_b = solve_matching("B", sids, dids_b,
                           build_candidates(sids, smat, dids_b, dmat_b, adj["threshold"]),
                           eps=EPS, threshold=adj["threshold"])
    check("brightness does not affect allocation",
          out_a.match_pairs == out_b.match_pairs, out_b.match_pairs)

    # ================================================= 5. 外部快照（不碰速度表）
    _w("5. 外部同格式快照：匹配仓独立消费，来源 external")
    ext_path = os.path.join(inputs, "external_snapshot.json")
    _dump(ext_path, syn.external_snapshot_payload())
    ext_snap = snapshot_from_jsonable(json.load(open(ext_path, encoding="utf-8")))
    with MatchWarehouse(small_wh) as wh:
        wh.import_exposure(ExposureInput(
            exposure_id="E-EXT", epoch=0, unit_tol=1e-9,
            detections=[{"detection_id": "DX1",
                         "direction": [float(x) for x in normalize(np.array([1.0, 0.001, 0.0]))],
                         "brightness": 3}]))
        run_ext = wh.run_matching("E-EXT", ext_snap, threshold=0.02, eps=EPS)
        stored = wh.load_snapshot(run_ext["snapshot_id"])
        check("external snapshot consumed without catalog velocity access",
              stored.source == "external" and stored.catalog_release is None
              and run_ext["matches"][0]["star_id"] == "X1",
              run_ext["matches"])
        explain = wh.explain_match(run_ext["run_id"], "X2", "DX1")
        check("explain rejected edge available",
              explain["status"] == "rejected" and "budget" in explain,
              explain["trace_event"]["reason"] if explain["trace_event"] else None)

    # ======================================== 6. 删除原始输入文件后复读 + 解释
    _w("6. 删除原始输入 JSON，仅靠 SQLite 复读全部 run 与解释")
    shutil.rmtree(inputs)
    check("input files removed", not os.path.exists(inputs))
    with MatchWarehouse(small_wh) as wh:
        runs = wh.list_runs()
        reread = wh.get_run(run_t1["run_id"])
        check("reread run after input deletion",
              [(m["star_id"], m["detection_id"]) for m in reread["matches"]]
              == [(m["star_id"], m["detection_id"]) for m in run_t1["matches"]]
              and reread["candidates"] and reread["trace"])
        narrative = wh.explain_run(run_t1b["run_id"])
        check("explain narrative survives deletion",
              len(narrative["selected"]) >= 1 and "min_cost" in narrative)
        why = wh.explain_match(run_ext["run_id"], "X1", "DX1")
        check("selected-edge explanation survives deletion",
              why["status"] == "selected" and why["trace_event"]["decision"] == "include")
        report["small_warehouse_runs"] = [
            {k: r[k] for k in ("run_id", "exposure_id", "snapshot_id",
                               "n_matches", "total_cost")} for r in runs]
    with MatchWarehouse(wh_db) as wh:
        big = wh.get_run(wh.latest_run_id(run_summaries[-1]["exposure_id"]))
        check("scale warehouse reread after deletion",
              big["n_matches"] == run_summaries[-1]["n_matches"])

    report["ok"] = True
    report["n_checks"] = len(report["checks"])
    _w(f"全部 {len(report['checks'])} 项检查通过")
    print(json.dumps({k: v for k, v in report.items() if k != "checks"},
                     ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()
    main(args.data_dir)
