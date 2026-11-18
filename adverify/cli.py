"""命令行入口：建库 / 扫描 / 核对 / 证据导出 / 验证。

示例
----
python -m adverify.cli gen --out data
python -m adverify.cli build-db --db data/monitor.db --materials data/materials
python -m adverify.cli scan --db data/monitor.db --program data/program.wav
python -m adverify.cli make-schedules --gt data/ground_truth.json --out data/schedules.json
python -m adverify.cli check --monitor-db data/monitor.db --fulfill-db data/fulfill.db \
    --schedules data/schedules.json
python -m adverify.cli evidence --db data/monitor.db --scan-id <id> --out data/evidence
python -m adverify.cli validate --gt data/ground_truth.json --monitor-db data/monitor.db
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import features as F
from . import matcher as M
from .evidence import export_evidence
from .fulfill import FulfillStore, generate_schedule_file
from .generator import generate_all
from .monitor import IncompatibleIndexError, MonitorStore, validate_monitor_table
from .validator import evaluate_detection, verify_solver_optimality


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_gen(args):
    _print(generate_all(args.out, seed=args.seed))


def cmd_build_db(args):
    store = MonitorStore(args.db, F.FeatureConfig())
    wavs = sorted(f for f in os.listdir(args.materials) if f.endswith(".wav"))
    registered = []
    for f in wavs:
        mid = os.path.splitext(f)[0]
        rec = store.register(mid, os.path.join(args.materials, f),
                             overwrite=args.overwrite)
        registered.append(rec.material_id)
    # 统一重建索引（当前稀疏峰指纹表示无需全局白化，方法名保留历史命名）
    store.rebuild_whitening()
    _print({"db": args.db, "n_registered": len(registered),
            "feature_config_hash": store.fcfg.config_hash(),
            "index": "rebuilt_under_single_feature_version"})
    store.close()


def cmd_list_materials(args):
    store = MonitorStore(args.db)
    _print([m.__dict__ for m in store.list_materials(args.all)])
    store.close()


def cmd_delete(args):
    store = MonitorStore(args.db)
    store.delete(args.material_id)
    _print({"deleted": args.material_id,
            "note": "软删除；历史扫描批次与匹配证据保持不变"})
    store.close()


def cmd_reindex(args):
    store = MonitorStore(args.db)
    n = len(store.list_materials())
    for m in store.list_materials():
        store.register(m.material_id, m.path, overwrite=True)
    store.rebuild_whitening()
    _print({"reindexed": n, "feature_config_hash": store.fcfg.config_hash()})
    store.close()


def cmd_scan(args):
    store = MonitorStore(args.db)
    cfg = M.ScanConfig()
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            cfg = M.ScanConfig(**json.load(f))
    try:
        scan_id = store.scan(args.program, cfg, note=args.note)
    except IncompatibleIndexError as e:
        _print({"error": "incompatible_index", "message": str(e)})
        sys.exit(2)
    matches = store.get_matches(scan_id)
    summary = {"scan_id": scan_id,
               "n_matches": len(matches),
               "n_confirmed": sum(m["certainty"] == "confirmed" for m in matches),
               "n_review": sum(m["certainty"] == "review" for m in matches),
               "n_unmatched": len(store.get_unmatched(scan_id)),
               "scan_config_hash": store.get_scan(scan_id)["scan_config_hash"]}
    _print(summary)
    store.close()


def cmd_list_scans(args):
    store = MonitorStore(args.db)
    _print(store.list_scans())
    store.close()


def cmd_show_scan(args):
    store = MonitorStore(args.db)
    scan_id = args.scan_id or store.latest_scan_id()
    data = store.export_scan(scan_id)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _print({"exported": args.out, "scan_id": scan_id,
                "n_matches": len(data["matches"])})
    else:
        _print(data)
    store.close()


def cmd_correct(args):
    store = MonitorStore(args.db)
    overrides = json.loads(args.overrides_json)
    old_id = args.scan_id or store.latest_scan_id()
    new_id = store.correct_match(old_id, args.play_id, overrides, reason=args.reason)
    _print({"new_scan_id": new_id, "supersedes": old_id,
            "play_id": args.play_id, "overrides": overrides,
            "note": "旧批次不可变，履约端应对新批次整体重算"})
    store.close()


def cmd_make_schedules(args):
    with open(args.gt, encoding="utf-8") as f:
        gt = json.load(f)
    path = generate_schedule_file(gt["material_meta"], gt, args.out,
                                  n_schedules=args.n, sr=gt["sr"])
    _print({"schedules": path, "n": args.n})


def _load_monitor_table(args, store: MonitorStore) -> tuple[dict, str, str]:
    if args.monitor_table:
        with open(args.monitor_table, encoding="utf-8") as f:
            table = json.load(f)
        validate_monitor_table(table)
        return table, "external_file", args.monitor_table
    scan_id = args.scan_id or store.latest_scan_id()
    if scan_id is None:
        raise RuntimeError("监测库中没有扫描批次")
    return store.export_scan(scan_id), "monitor_db", scan_id


def cmd_check(args):
    store = MonitorStore(args.db) if args.db else None
    fstore = FulfillStore(args.fulfill_db)
    n_imp = fstore.import_schedules(args.schedules, replace=args.replace_schedules)
    if args.monitor_table:
        with open(args.monitor_table, encoding="utf-8") as f:
            table = json.load(f)
        kind, ref = "external_file", args.monitor_table
    else:
        if store is None:
            raise RuntimeError("需要 --monitor-db 或 --monitor-table")
        scan_id = args.scan_id or store.latest_scan_id()
        table = store.export_scan(scan_id)
        kind, ref = "monitor_db", scan_id
    run_id = fstore.run(table, source_kind=kind, source_ref=ref, note=args.note)
    run = fstore.get_run(run_id)
    if store:
        store.close()
    fstore.close()
    diag = run["result"]["diagnostics"]
    _print({"run_id": run_id, "schedules_imported": n_imp,
            "source": {"kind": kind, "ref": ref},
            "total_required": diag["total_required"],
            "total_satisfied": diag["total_satisfied"],
            "total_doubled_distance": diag["total_doubled_distance"],
            "n_fulfilled": sum(r["status"] == "fulfilled"
                               for r in run["result"]["schedule_report"]),
            "n_partial": sum(r["status"] == "partial"
                             for r in run["result"]["schedule_report"]),
            "n_leftover_plays": len(run["result"]["leftover_plays"]),
            "result_preview": run["result"] if args.verbose else
            {"allocations": run["result"]["allocations"][:10],
             "schedule_report": run["result"]["schedule_report"][:10],
             "leftover_plays": run["result"]["leftover_plays"][:10],
             "diagnostics": diag}})


def cmd_list_runs(args):
    fstore = FulfillStore(args.fulfill_db)
    _print(fstore.list_runs())
    fstore.close()


def cmd_show_run(args):
    fstore = FulfillStore(args.fulfill_db)
    run_id = args.run_id or fstore.latest_run_id()
    run = fstore.get_run(run_id)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(run, f, ensure_ascii=False, indent=2)
        _print({"exported": args.out, "run_id": run_id})
    else:
        _print(run)
    fstore.close()


def cmd_evidence(args):
    store = MonitorStore(args.db)
    scan_id = args.scan_id or store.latest_scan_id()
    path = export_evidence(store, scan_id, args.out)
    _print({"evidence_dir": path, "scan_id": scan_id})
    store.close()


def cmd_validate(args):
    store = MonitorStore(args.monitor_db)
    with open(args.gt, encoding="utf-8") as f:
        gt = json.load(f)
    scan_id = args.scan_id or store.latest_scan_id()
    matches = store.get_matches(scan_id)
    det = evaluate_detection(gt, matches)
    opt = verify_solver_optimality()
    report = {"scan_id": scan_id, "detection": det,
              "solver_bruteforce_check": opt}
    out = args.out or os.path.join(os.path.dirname(args.gt), "validation_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    store.close()
    _print({"report": out,
            "linked": det["n_linked"], "missed": len(det["missed_detections"]),
            "false_alarms": len(det["false_alarms"]),
            "start_err_mean_samples": det["start_error_samples"]["mean"],
            "solver_all_passed": opt["all_passed"]})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adverify",
                                description="本地广告播出核对工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("gen", help="生成合成素材、节目流与真值")
    sp.add_argument("--out", required=True)
    sp.add_argument("--seed", type=int, default=20261118)
    sp.set_defaults(func=cmd_gen)

    sp = sub.add_parser("build-db", help="登记素材并计算检索特征")
    sp.add_argument("--db", required=True)
    sp.add_argument("--materials", required=True)
    sp.add_argument("--overwrite", action="store_true")
    sp.set_defaults(func=cmd_build_db)

    sp = sub.add_parser("list-materials", help="查询素材")
    sp.add_argument("--db", required=True)
    sp.add_argument("--all", action="store_true", help="含已软删除素材")
    sp.set_defaults(func=cmd_list_materials)

    sp = sub.add_parser("delete-material", help="软删除素材（旧批次不变）")
    sp.add_argument("--db", required=True)
    sp.add_argument("--material-id", required=True)
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("reindex", help="按当前特征配置重建全部索引")
    sp.add_argument("--db", required=True)
    sp.set_defaults(func=cmd_reindex)

    sp = sub.add_parser("scan", help="扫描节目音频，产生不可变监测批次")
    sp.add_argument("--db", required=True)
    sp.add_argument("--program", required=True)
    sp.add_argument("--config", help="ScanConfig JSON 覆盖阈值")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("list-scans", help="列出监测批次")
    sp.add_argument("--db", required=True)
    sp.set_defaults(func=cmd_list_scans)

    sp = sub.add_parser("show-scan", help="导出/查看监测批次")
    sp.add_argument("--db", required=True)
    sp.add_argument("--scan-id")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_show_scan)

    sp = sub.add_parser("correct-match", help="更正一条匹配，产生新批次")
    sp.add_argument("--db", required=True)
    sp.add_argument("--scan-id")
    sp.add_argument("--play-id", required=True)
    sp.add_argument("--overrides-json", required=True,
                    help='如 {"certainty":"review","mat_end":12000}')
    sp.add_argument("--reason", default="manual_correction")
    sp.set_defaults(func=cmd_correct)

    sp = sub.add_parser("make-schedules", help="依据真值生成排期文件（造数/验证用）")
    sp.add_argument("--gt", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--n", type=int, default=100)
    sp.set_defaults(func=cmd_make_schedules)

    sp = sub.add_parser("check", help="履约核对（监测库批次或外部监测表）")
    sp.add_argument("--monitor-db", dest="db")
    sp.add_argument("--scan-id")
    sp.add_argument("--monitor-table", help="合法外部监测表 JSON，无需音频/监测库")
    sp.add_argument("--fulfill-db", required=True)
    sp.add_argument("--schedules", required=True)
    sp.add_argument("--replace-schedules", action="store_true")
    sp.add_argument("--note", default="")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("list-runs")
    sp.add_argument("--fulfill-db", required=True)
    sp.set_defaults(func=cmd_list_runs)

    sp = sub.add_parser("show-run")
    sp.add_argument("--fulfill-db", required=True)
    sp.add_argument("--run-id")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_show_run)

    sp = sub.add_parser("evidence", help="导出可复查对齐证据与 WAV 片段")
    sp.add_argument("--db", required=True)
    sp.add_argument("--scan-id")
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=cmd_evidence)

    sp = sub.add_parser("validate", help="真值比对 + 穷举最优性验证")
    sp.add_argument("--gt", required=True)
    sp.add_argument("--monitor-db", required=True)
    sp.add_argument("--scan-id")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_validate)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
