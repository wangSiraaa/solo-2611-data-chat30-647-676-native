"""``tape-restore`` command line.

Subcommands:

    gen       create synthetic media + catalog + independent truth
    catalog   validate/list a catalog
    inspect   audit volumes / drives / scratch / journal
    plan      show both strategies and their predicted costs
    restore   run a restore (both strategies selectable)
    resume    continue an interrupted run
    verify    independently compare output vs truth bytes + digests
    device    drive a raw sequential read of a volume (no restore logic)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

from . import media
from .device import DeviceManager, MediaLibrary
from .errors import TapeRestoreError
from .journal import Journal
from .model import Catalog, load_catalog, parse_catalog
from .planner import build_plan, pareto_report
from .restorer import RestoreEngine, Interrupted, resume
from .scenario import generate_scenario, load_scenario
from .staging import ScratchLedger, Workspace


def _load_catalog_for(args) -> tuple[Catalog, MediaLibrary]:
    lib = MediaLibrary(Path(args.media))
    if args.scenario:
        sc = load_scenario(Path(args.scenario))
        return sc.catalog, MediaLibrary(Path(sc.root) / "media")
    if not args.catalog:
        raise TapeRestoreError("--catalog or --scenario is required")
    raw = json.loads(Path(args.catalog).read_text())
    # load real volumes so validation is structural, not decorative
    volumes = {}
    for vid in raw.get("volumes", {}):
        p = lib.path_for(vid)
        volumes[vid] = media.read_volume(p)
    return parse_catalog(raw, volumes=volumes), lib


def _emit(obj, args) -> None:
    text = json.dumps(obj, indent=2, sort_keys=True)
    if getattr(args, "out", None):
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n")
    print(text)


def cmd_gen(args) -> int:
    sc = generate_scenario(Path(args.dest), force=args.force)
    _emit({"generated": str(Path(args.dest).resolve()), **sc.as_summary()}, args)
    return 0


def cmd_catalog(args) -> int:
    sc = load_scenario(Path(args.scenario))
    if args.file:
        if args.file not in sc.catalog.files:
            raise TapeRestoreError("no such file_id", file_id=args.file)
        _emit(sc.catalog.files[args.file].as_dict(), args)
        return 0
    _emit({
        "valid": True,
        "warnings": list(sc.catalog.warnings),
        "block_size": sc.catalog.block_size,
        "volumes": sc.catalog.volume_block_counts,
        "files": {
            fid: {
                "path": fe.path, "total_length": fe.total_length,
                "sha256": fe.sha256, "fragments": len(fe.fragments),
                "fragment_ids": [f.fragment_id for f in fe.fragments],
            } for fid, fe in sc.catalog.files.items()
        },
    }, args)
    return 0


def cmd_inspect(args) -> int:
    sc = load_scenario(Path(args.scenario))
    result: dict = {"scenario": sc.as_summary()}
    if args.audit_media:
        audits = {}
        for vid, p in sorted(sc.media_paths.items()):
            try:
                audits[vid] = media.audit_volume(p)
            except TapeRestoreError as exc:
                audits[vid] = {"error": exc.as_dict()}
        # keep output small: drop per-block detail unless asked
        for vid, a in audits.items():
            if isinstance(a, dict) and "blocks" in a and not args.blocks:
                a["blocks"] = f"<{len(a['blocks'])} blocks; use --blocks>"
        result["media_audit"] = audits
    if args.media_dir:
        lib = MediaLibrary(Path(args.media_dir))
        result["media_library"] = {"volumes": lib.known_volumes()}
    if args.state:
        state_dir = Path(args.state)
        cp = Journal(state_dir, "?").read_checkpoint()
        if cp is None:
            result["checkpoint"] = None
        else:
            result["checkpoint"] = {
                "run_id": cp.get("run_id"),
                "strategy": cp.get("strategy"),
                "confirmed_total": cp.get("confirmed_total"),
                "scratch_current": cp.get("scratch_current"),
                "scratch_peak": cp.get("scratch_peak"),
                "journal_seq": cp.get("journal_seq"),
                "drives": cp.get("device", {}).get("drives"),
                "files": {
                    fid: {"status": st.get("status"), "lease": st.get("lease"),
                          "confirmed": len(st.get("confirmed", {}))}
                    for fid, st in cp.get("files", {}).items()
                },
            }
        jr = Journal(state_dir, "?")
        if jr.path.exists():
            result["journal"] = jr.run_summary()
            result["last_events"] = jr.events()[-8:]
    _emit(result, args)
    return 0


def cmd_plan(args) -> int:
    sc = load_scenario(Path(args.scenario))
    lib = MediaLibrary(Path(sc.root) / "media")
    quota = args.quota if args.quota is not None else sc.quota_bytes
    ids = args.files.split(",") if args.files else None
    plans = [build_plan(sc.catalog, lib, strategy=s, file_ids=ids, quota=quota)
             for s in ("volume", "file")]
    _emit(pareto_report(plans), args)
    return 0


def _workspace(args, sc) -> Workspace:
    root = Path(args.root) if args.root else (Path(sc.root) / "run")
    return Workspace(root)


def cmd_restore(args) -> int:
    sc = load_scenario(Path(args.scenario))
    lib = MediaLibrary(Path(sc.root) / "media")
    quota = args.quota if args.quota is not None else sc.quota_bytes
    ws = _workspace(args, sc)
    if args.clean:
        shutil.rmtree(ws.root, ignore_errors=True)
        ws = Workspace(ws.root)
    ids = args.files.split(",") if args.files else None
    engine = RestoreEngine(
        sc.catalog, lib, ws, quota=quota, strategy=args.strategy,
        run_id=args.run_id, block_size=sc.catalog.block_size,
        interrupt_after=args.interrupt_after,
    )
    try:
        result = engine.restore(ids, allow_fallback=args.fallback)
    except Interrupted as exc:
        cp = engine.journal.read_checkpoint()
        _emit({
            "status": "interrupted",
            "run_id": engine.run_id,
            "error": exc.as_dict(),
            "checkpoint": {
                "confirmed_total": (cp or {}).get("confirmed_total"),
                "scratch_current": (cp or {}).get("scratch_current"),
                "scratch_peak": (cp or {}).get("scratch_peak"),
                "drives": (cp or {}).get("device", {}).get("drives"),
            },
            "resume_hint": f"--scenario {args.scenario} resume --run-id {engine.run_id} --root {ws.root}",
            "device": {"summary": engine.device.log.summary()},
        }, args)
        return 3
    _emit(result, args)
    return 0


def cmd_resume(args) -> int:
    sc = load_scenario(Path(args.scenario))
    lib = MediaLibrary(Path(sc.root) / "media")
    quota = args.quota if args.quota is not None else sc.quota_bytes
    ws = _workspace(args, sc)
    result = resume(sc.catalog, lib, ws, quota=quota, run_id=args.run_id,
                    interrupt_after=args.interrupt_after)
    _emit(result, args)
    return 0


def cmd_verify(args) -> int:
    sc = load_scenario(Path(args.scenario))
    ws = _workspace(args, sc)
    ids = args.files.split(",") if args.files else sc.catalog.file_ids()
    checks = []
    for fid in ids:
        fe = sc.catalog.files[fid]
        dest = ws.output_dir / fe.path
        truth = sc.truth[fid]
        exists = dest.exists()
        actual = dest.read_bytes() if exists else b""
        actual_sha = __import__("hashlib").sha256(actual).hexdigest() if exists else None
        truth_sha = __import__("hashlib").sha256(truth).hexdigest()
        checks.append({
            "file_id": fid,
            "path": fe.path,
            "published": exists,
            "expected_length": len(truth),
            "actual_length": len(actual) if exists else None,
            "bytes_match_truth": exists and actual == truth,
            "sha_match_catalog": exists and actual_sha == fe.sha256,
            "sha_match_truth": exists and actual_sha == truth_sha,
        })
    present = [c for c in checks if c["published"]]
    # correctness is judged on files that ARE published: each must match truth
    # byte-for-byte.  Missing files are listed separately; a missing file never
    # counts as a match, and a published-but-wrong file is always a failure.
    all_published_correct = all(c["bytes_match_truth"] and c["sha_match_catalog"]
                                for c in present)
    _emit({
        "output_dir": str(ws.output_dir),
        "checked": len(checks),
        "published_count": len(present),
        "missing": [c["file_id"] for c in checks if not c["published"]],
        "all_published_match_truth": all_published_correct,
        "published": [c["file_id"] for c in present],
        "checks": checks,
    }, args)
    return 0


def cmd_device(args) -> int:
    sc = load_scenario(Path(args.scenario)) if args.scenario else None
    media_dir = Path(args.media) if args.media else Path(sc.root) / "media"
    lib = MediaLibrary(media_dir)
    dm = DeviceManager(lib)
    out = dm.scan(args.volume)
    _emit(out, args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tape-restore", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--scenario", help="scenario root (media/catalog/truth)")
    common.add_argument("--catalog", help="catalog JSON path")
    common.add_argument("--media", help="media directory")
    common.add_argument("--out", help="write JSON result here as well")

    g = sub.add_parser("gen", parents=[common])
    g.add_argument("--dest", default="results/scenario")
    g.add_argument("--force", action="store_true")
    g.set_defaults(func=cmd_gen)

    c = sub.add_parser("catalog", parents=[common])
    c.add_argument("--file")
    c.set_defaults(func=cmd_catalog)

    i = sub.add_parser("inspect", parents=[common])
    i.add_argument("--audit-media", action="store_true")
    i.add_argument("--blocks", action="store_true")
    i.add_argument("--media-dir")
    i.add_argument("--state")
    i.set_defaults(func=cmd_inspect)

    pl = sub.add_parser("plan", parents=[common])
    pl.add_argument("--quota", type=int)
    pl.add_argument("--files")
    pl.set_defaults(func=cmd_plan)

    r = sub.add_parser("restore", parents=[common])
    r.add_argument("--strategy", choices=("file", "volume"), default="file")
    r.add_argument("--quota", type=int)
    r.add_argument("--files")
    r.add_argument("--root")
    r.add_argument("--run-id")
    r.add_argument("--interrupt-after", type=int)
    r.add_argument("--fallback", action="store_true")
    r.add_argument("--clean", action="store_true")
    r.set_defaults(func=cmd_restore)

    rs = sub.add_parser("resume", parents=[common])
    rs.add_argument("--quota", type=int)
    rs.add_argument("--root")
    rs.add_argument("--run-id")
    rs.add_argument("--interrupt-after", type=int)
    rs.set_defaults(func=cmd_resume)

    v = sub.add_parser("verify", parents=[common])
    v.add_argument("--files")
    v.add_argument("--root")
    v.set_defaults(func=cmd_verify)

    d = sub.add_parser("device", parents=[common])
    d.add_argument("--volume", required=True)
    d.set_defaults(func=cmd_device)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TapeRestoreError as exc:
        print(json.dumps({"error": exc.as_dict()}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
