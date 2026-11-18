#!/usr/bin/env python3
"""Reproducible end-to-end demonstration.

Runs every scenario the brief asks for and writes a machine-readable report to
``results/report.json`` (plus a human summary on stdout).  Every "published"
file is compared byte-for-byte against independently generated truth; device
calls and per-step scratch usage are re-checked, never just counted.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tape_restore.device import DeviceManager, MediaLibrary
from tape_restore.errors import TapeRestoreError
from tape_restore.media import read_volume, write_volume
from tape_restore.model import parse_catalog
from tape_restore.planner import build_plan, pareto_report
from tape_restore.restorer import (
    COMPLETE, FAILED_FRAGMENT, FAILED_MEDIA, SKIPPED_QUOTA,
    Interrupted, RestoreEngine, resume,
)
from tape_restore.scenario import generate_scenario, load_scenario
from tape_restore.staging import Workspace

RESULTS = ROOT / "results"
SCEN = RESULTS / "scenario"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def audit_outputs(sc, ws: Workspace, result: dict) -> dict:
    """Independent byte comparison + device-call sanity for one run."""
    per_file = {}
    for fid, fe in sc.catalog.files.items():
        dest = ws.output_dir / fe.path
        exists = dest.exists()
        data = dest.read_bytes() if exists else None
        req = result["files"].get(fid)
        per_file[fid] = {
            "status": req["status"] if req else "not_requested",
            "published": exists,
            "bytes_match_truth": exists and data == sc.truth[fid],
            "sha_match_catalog": exists and sha(data) == fe.sha256,
        }
    published = [f for f, v in per_file.items() if v["published"]]
    wrong_published = [f for f in published if not per_file[f]["bytes_match_truth"]]
    false_complete = [
        f for f, v in per_file.items()
        if v["status"] == COMPLETE and not v["bytes_match_truth"]
    ]
    all_expected_absent = [
        f for f, v in per_file.items()
        if v["status"] in (FAILED_FRAGMENT, FAILED_MEDIA, SKIPPED_QUOTA) and v["published"]
    ]
    dev = result["device"]
    scratch = result["scratch"]
    skip_forward_only = all(
        c.get("after", 0) >= c.get("before", 0)
        for c in dev["calls"] if c["op"] == "skip" and c.get("ok")
    )
    peak_ok = scratch["peak_bytes"] <= result["quota_bytes"] and all(
        s["current_bytes"] <= result["quota_bytes"] for s in scratch["steps"]
    )
    return {
        "published_count": len(published),
        "published": sorted(published),
        "wrong_published": wrong_published,
        "complete_but_wrong": false_complete,
        "failed_but_published": all_expected_absent,
        "scratch_peak_bytes": scratch["peak_bytes"],
        "quota_bytes": result["quota_bytes"],
        "scratch_within_quota": peak_ok,
        "skip_forward_only": skip_forward_only,
        "device": dev["summary"],
        "ok": not wrong_published and not false_complete and not all_expected_absent
              and peak_ok and skip_forward_only,
        "per_file": per_file,
    }


def case_all_file_major(sc, lib) -> dict:
    root = RESULTS / "run_file_major"
    shutil.rmtree(root, ignore_errors=True)
    ws = Workspace(root)
    eng = RestoreEngine(sc.catalog, lib, ws, quota=sc.quota_bytes, strategy="file")
    res = eng.restore(None, allow_fallback=True)
    return {"run": res, "audit": audit_outputs(sc, ws, res), "root": str(root)}


def case_volume_major(sc, lib) -> dict:
    ids = ["F01", "F02", "F03", "F04", "F08", "F13", "F17"]
    root = RESULTS / "run_volume_major"
    shutil.rmtree(root, ignore_errors=True)
    ws = Workspace(root)
    eng = RestoreEngine(sc.catalog, lib, ws, quota=sc.quota_bytes, strategy="volume")
    res = eng.restore(ids, allow_fallback=False)
    audit = audit_outputs(sc, ws, res)
    return {"run": res, "audit": audit, "requested": ids, "root": str(root)}


def case_quota_infeasible(sc, lib) -> dict:
    root = RESULTS / "run_quota"
    shutil.rmtree(root, ignore_errors=True)
    ws = Workspace(root)
    eng = RestoreEngine(sc.catalog, lib, ws, quota=sc.quota_bytes, strategy="file")
    res = eng.restore(["F10", "F09", "F01"], allow_fallback=False)
    audit = audit_outputs(sc, ws, res)
    return {"run": res, "audit": audit, "root": str(root)}


def case_interrupt_resume(sc, lib) -> dict:
    root = RESULTS / "run_resume"
    shutil.rmtree(root, ignore_errors=True)
    ws = Workspace(root)
    eng = RestoreEngine(sc.catalog, lib, ws, quota=sc.quota_bytes, strategy="file",
                        run_id="DEMO-RESUME", interrupt_after=5)
    try:
        eng.restore(["F07", "F12", "F01"], allow_fallback=True)
        interrupted = False
    except Interrupted:
        interrupted = True
    cp = eng.journal.read_checkpoint()
    resumed = resume(sc.catalog, lib, ws, quota=sc.quota_bytes, run_id="DEMO-RESUME")
    audit = audit_outputs(sc, ws, resumed)
    return {
        "interrupted": interrupted,
        "checkpoint": {
            "confirmed_total": cp["confirmed_total"],
            "scratch_current": cp["scratch_current"],
            "scratch_peak": cp["scratch_peak"],
            "drives": cp["device"]["drives"],
        },
        "invalidation": resumed["invalidation"],
        "run": resumed,
        "audit": audit,
        "root": str(root),
    }


def case_bad_and_truncation(sc, lib) -> dict:
    # (a) bad fragment: F14 / F18 are already corrupted in the generated media
    root_bad = RESULTS / "run_bad_fragment"
    shutil.rmtree(root_bad, ignore_errors=True)
    ws = Workspace(root_bad)
    eng = RestoreEngine(sc.catalog, lib, ws, quota=sc.quota_bytes, strategy="file")
    bad = eng.restore(["F14", "F18", "F01", "F03"], allow_fallback=True)
    bad_audit = audit_outputs(sc, ws, bad)

    # (b) truncation: copy media, truncate V4, try F09 (all on V4) + F01 (not on V4)
    root_tr = RESULTS / "run_truncation"
    media = root_tr / "media"
    shutil.rmtree(root_tr, ignore_errors=True)
    media.mkdir(parents=True)
    for vid in sc.volumes:
        shutil.copy(SCEN / "media" / f"{vid}.tape", media / f"{vid}.tape")
    p = media / "V4.tape"
    p.write_bytes(p.read_bytes()[:600])
    tlib = MediaLibrary(media)
    ws2 = Workspace(root_tr / "ws")
    eng2 = RestoreEngine(sc.catalog, tlib, ws2, quota=sc.quota_bytes, strategy="file")
    tr = eng2.restore(["F09", "F01"], allow_fallback=True)
    tr_audit = audit_outputs(sc, ws2, tr)
    return {
        "bad_fragment": {"run": bad, "audit": bad_audit, "root": str(root_bad)},
        "truncation": {"run": tr, "audit": tr_audit, "root": str(root_tr / "ws")},
    }


def case_catalog_change(sc, lib) -> dict:
    root = RESULTS / "run_catalog_change"
    shutil.rmtree(root, ignore_errors=True)
    ws = Workspace(root)
    eng = RestoreEngine(sc.catalog, lib, ws, quota=sc.quota_bytes, strategy="file",
                        run_id="DEMO-CAT", interrupt_after=2)
    try:
        eng.restore(["F01", "F02"], allow_fallback=True)
    except Interrupted:
        pass
    raw = json.loads((SCEN / "catalog.json").read_text())
    f = next(f for f in raw["files"] if f["file_id"] == "F01")
    f["fragments"][0]["sha256"] = "f" * 64
    new_cat = parse_catalog(raw, volumes=sc.volumes)
    res = resume(new_cat, lib, ws, quota=sc.quota_bytes, run_id="DEMO-CAT")
    audit = audit_outputs(sc, ws, res)
    return {"run": res, "audit": audit,
            "catalog_changed": res["invalidation"]["catalog_changed"],
            "root": str(root)}


def case_shared_volume(sc, lib) -> dict:
    # V3 is shared by F05, F06, F14, F19: restore all non-corrupt ones from it.
    ids = ["F05", "F06", "F19"]
    root = RESULTS / "run_shared_volume"
    shutil.rmtree(root, ignore_errors=True)
    ws = Workspace(root)
    eng = RestoreEngine(sc.catalog, lib, ws, quota=sc.quota_bytes, strategy="volume")
    res = eng.restore(ids, allow_fallback=False)
    audit = audit_outputs(sc, ws, res)
    return {"run": res, "audit": audit, "requested": ids, "root": str(root)}


def case_device_client(sc, lib) -> dict:
    dm = DeviceManager(lib)
    scan = dm.scan("V3")
    return {
        "volume": "V3",
        "position_after": scan["position"],
        "data_blocks_seen": sum(1 for b in scan["blocks"] if b["kind"] == "data"),
        "filemarks_seen": scan["filemarks"],
        "expected_data_blocks": sc.volumes["V3"].data_blocks(),
        "log": scan["log"],
        "independent_of_restore": True,
    }


def case_plan(sc, lib) -> dict:
    v = build_plan(sc.catalog, lib, "volume", None, quota=sc.quota_bytes)
    f = build_plan(sc.catalog, lib, "file", None, quota=sc.quota_bytes)
    return pareto_report([v, f])


def main() -> int:
    RESULTS.mkdir(exist_ok=True)
    print("generating scenario ...")
    sc = generate_scenario(SCEN, force=True)
    lib = MediaLibrary(SCEN / "media")
    print(f"  {len(sc.catalog.files)} files, {len(sc.volumes)} volumes, "
          f"quota {sc.quota_bytes} B")

    report: dict = {
        "scenario": sc.as_summary(),
        "plan": case_plan(sc, lib),
        "cases": {},
    }
    cases = {
        "plan_tradeoff": lambda: report["plan"],
        "file_major_all": lambda: case_all_file_major(sc, lib),
        "volume_major_subset": lambda: case_volume_major(sc, lib),
        "shared_volume": lambda: case_shared_volume(sc, lib),
        "quota_infeasible": lambda: case_quota_infeasible(sc, lib),
        "interrupt_resume": lambda: case_interrupt_resume(sc, lib),
        "bad_fragment_and_truncation": lambda: case_bad_and_truncation(sc, lib),
        "catalog_change": lambda: case_catalog_change(sc, lib),
        "device_client": lambda: case_device_client(sc, lib),
    }
    for name, fn in cases.items():
        print(f"running {name} ...")
        try:
            report["cases"][name] = fn()
        except TapeRestoreError as exc:
            report["cases"][name] = {"error": exc.as_dict()}

    out = RESULTS / "report.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    print(f"wrote {out}")

    # ---- human summary -------------------------------------------------
    def show_audit(label, a):
        print(f"  [{label}] ok={a['ok']} published={a['published_count']} "
              f"peak={a['scratch_peak_bytes']}/{a['quota_bytes']} "
              f"wrong={a['wrong_published']} false_complete={a['complete_but_wrong']} "
              f"failed_but_published={a['failed_but_published']}")

    c = report["cases"]
    print("\n=== summary ===")
    show_audit("file-major all", c["file_major_all"]["audit"])
    show_audit("volume-major subset", c["volume_major_subset"]["audit"])
    show_audit("shared volume", c["shared_volume"]["audit"])
    show_audit("quota infeasible", c["quota_infeasible"]["audit"])
    show_audit("interrupt+resume", c["interrupt_resume"]["audit"])
    show_audit("bad fragment", c["bad_fragment_and_truncation"]["bad_fragment"]["audit"])
    show_audit("truncation", c["bad_fragment_and_truncation"]["truncation"]["audit"])
    show_audit("catalog change", c["catalog_change"]["audit"])

    plans = report["plan"]["strategies"]
    print(f"\n  plan: volume mounts={plans['volume']['predicted']['mounts']} "
          f"peak={plans['volume']['predicted']['scratch_peak_bytes']}  |  "
          f"file mounts={plans['file']['predicted']['mounts']} "
          f"peak={plans['file']['predicted']['scratch_peak_bytes']}")
    dc = c["device_client"]
    print(f"  device client: V3 read {dc['data_blocks_seen']}/{dc['expected_data_blocks']} "
          f"data blocks, {dc['filemarks_seen']} filemarks, position={dc['position_after']}")
    ir = c["interrupt_resume"]
    print(f"  resume: checkpoint=5 frags, restored={len(ir['invalidation']['restored_fragments'])}, "
          f"dropped={len(ir['invalidation']['dropped_fragments'])}, "
          f"positions_ok={all(p['ok'] for p in ir['invalidation']['position_checks'])}")

    ok = all(
        c[name]["audit"]["ok"] for name in
        ("file_major_all", "volume_major_subset", "shared_volume", "quota_infeasible",
         "interrupt_resume", "catalog_change")
    ) and c["bad_fragment_and_truncation"]["bad_fragment"]["audit"]["ok"] \
      and c["bad_fragment_and_truncation"]["truncation"]["audit"]["ok"]
    print(f"\nOVERALL: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
