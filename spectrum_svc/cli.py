"""Command line interface: calibration editing/publishing, spectrum import,
quantification and verification.

Two SQLite files keep the services separate::

    catalog-<name>.db   private calibration points + published products
    repo-<name>.db      public products, spectra and cached results

Rational arguments are written exactly, e.g. ``1/3``, ``2.5``, ``7``.
"""

from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from typing import List, Tuple

import numpy as np

from .calibration import CalibrationError
from .catalog import CalibrationCatalog
from .limits import MAX_WINDOWS
from .rationals import as_rational
from .repository import CountRepository, RepositoryError


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _parse_points(items: List[str]) -> List[Tuple[Fraction, Fraction]]:
    points = []
    for item in items:
        parts = item.split(",")
        if len(parts) != 2:
            raise argparse.ArgumentTypeError(
                f"point {item!r} must be channel,energy")
        points.append((as_rational(parts[0].strip()),
                       as_rational(parts[1].strip())))
    return points


def _parse_windows(items: List[str]) -> List[Tuple[Fraction, Fraction]]:
    windows = []
    for item in items:
        parts = item.split(",")
        if len(parts) != 2:
            raise argparse.ArgumentTypeError(
                f"window {item!r} must be e_lo,e_hi")
        lo, hi = as_rational(parts[0].strip()), as_rational(parts[1].strip())
        if hi <= lo:
            raise argparse.ArgumentTypeError(f"empty window {item!r}")
        windows.append((lo, hi))
    if len(windows) > MAX_WINDOWS:
        raise argparse.ArgumentTypeError(
            f"at most {MAX_WINDOWS} windows per call")
    return windows


def _read_counts(spec: str) -> List[int]:
    if spec.startswith("@"):
        path = spec[1:]
        if path.endswith(".npy"):
            arr = np.load(path)
            return [int(x) for x in arr.reshape(-1)]
        with open(path, encoding="utf-8") as fh:
            spec = fh.read()
    values = [int(x) for x in spec.replace("\n", ",").split(",") if x.strip()]
    if any(x < 0 for x in values):
        raise argparse.ArgumentTypeError("counts must be non-negative")
    return values


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spectrum-svc",
        description="Synthetic spectrum calibration service and counting "
                    "repository (no device access).")
    p.add_argument("--catalog", default="data/catalog.db",
                   help="catalog SQLite path (private calibration points)")
    p.add_argument("--repo", default="data/repo.db",
                   help="repository SQLite path (public products/spectra)")
    sub = p.add_subparsers(dest="command", required=True)

    cal_put = sub.add_parser("calib-put",
                             help="replace a device's calibration points")
    cal_put.add_argument("device")
    cal_put.add_argument("points", nargs="+",
                         help="channel,energy pairs, exact rationals")

    sub.add_parser("calib-list", help="list devices and head versions")

    cal_get = sub.add_parser("calib-get", help="show stored points")
    cal_get.add_argument("device")
    cal_get.add_argument("--version", type=int)

    pub = sub.add_parser("calib-publish", help="publish an immutable product")
    pub.add_argument("device")
    pub.add_argument("--version", type=int)

    plist = sub.add_parser("product-list", help="list products in a service")
    plist.add_argument("--service", choices=("catalog", "repo"),
                       default="catalog")

    pexp = sub.add_parser("product-export", help="export product as JSON")
    pexp.add_argument("product_id")

    pimp = sub.add_parser("product-import",
                          help="import an external same-format product JSON")
    pimp.add_argument("json_file")

    pproj = sub.add_parser("project", help="interval projection checks")
    pproj.add_argument("product_id")
    pproj.add_argument("--channel", nargs=2, metavar=("C_LO", "C_HI"))
    pproj.add_argument("--energy", nargs=2, metavar=("E_LO", "E_HI"))

    simp = sub.add_parser("spectrum-import", help="import one spectrum")
    simp.add_argument("spectrum_id")
    simp.add_argument("device")
    simp.add_argument("product_id")
    simp.add_argument("start_sec", type=int)
    simp.add_argument("T")
    simp.add_argument("L")
    simp.add_argument("counts", help="comma list or @file (.npy/.txt)")

    sub.add_parser("spectrum-list", help="list imported spectra")

    q = sub.add_parser("quantify", help="quantify energy windows")
    q.add_argument("spectrum_id")
    q.add_argument("windows", nargs="+", help="e_lo,e_hi pairs")
    q.add_argument("--eff", required=True)
    q.add_argument("--lambda", dest="lam", type=float, default=0.0)
    q.add_argument("--t-ref", type=int, required=True)

    net = sub.add_parser("quantify-net",
                         help="sample minus background at common t_ref")
    net.add_argument("sample_id")
    net.add_argument("background_id")
    net.add_argument("window", help="e_lo,e_hi")
    net.add_argument("--eff", required=True)
    net.add_argument("--lambda", dest="lam", type=float, default=0.0)
    net.add_argument("--t-ref", type=int, required=True)

    ver = sub.add_parser("verify", help="recompute and check a stored result")
    ver.add_argument("result_id")

    sub.add_parser("result-list", help="list cached results")
    return p


CATALOG_COMMANDS = frozenset({
    "calib-put", "calib-list", "calib-get", "calib-publish",
    "product-export", "project",
})


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command
    use_catalog = cmd in CATALOG_COMMANDS or (
        cmd == "product-list" and args.service == "catalog")
    try:
        if use_catalog:
            with CalibrationCatalog(args.catalog) as catalog:
                return _run_catalog(catalog, args)
        with CountRepository(args.repo) as repo:
            return _run_repo(repo, args)
    except (CalibrationError, RepositoryError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run_catalog(catalog: CalibrationCatalog, args) -> int:
    cmd = args.command
    if cmd == "calib-put":
        version = catalog.replace_points(args.device, _parse_points(args.points))
        _print({"device_id": args.device, "new_version": version,
                "note": "previous state retained on validation failure"})
    elif cmd == "calib-list":
        _print(catalog.list_devices())
    elif cmd == "calib-get":
        points = catalog.get_points(args.device, args.version)
        _print({"device_id": args.device,
                "points": [[str(c), str(e)] for c, e in points]})
    elif cmd == "calib-publish":
        product = catalog.publish(args.device, args.version)
        _print({"product_id": product.product_id,
                "device_id": product.device_id,
                "version": product.version,
                "energy_range": [str(x) for x in product.energy_range]})
    elif cmd == "product-list":
        _print(catalog.list_products())
    elif cmd == "product-export":
        print(catalog.export_product(args.product_id))
    elif cmd == "project":
        product = catalog.get_product(args.product_id)
        out = {"product_id": product.product_id,
               "channel_range": [str(x) for x in product.channel_range],
               "energy_range": [str(x) for x in product.energy_range]}
        if args.channel:
            lo, hi = map(as_rational, args.channel)
            out["channel_to_energy"] = [
                str(x) for x in product.channel_interval_to_energy(lo, hi)]
        if args.energy:
            lo, hi = map(as_rational, args.energy)
            out["energy_to_channel"] = [
                str(x) for x in product.energy_interval_to_channel(lo, hi)]
        _print(out)
    return 0


def _run_repo(repo: CountRepository, args) -> int:
    cmd = args.command
    if cmd == "product-import":
        with open(args.json_file, encoding="utf-8") as fh:
            pid = repo.import_product(fh.read())
        _print({"imported_product_id": pid})
    elif cmd == "product-list":
        _print(repo.list_products())
    elif cmd == "spectrum-import":
        repo.import_spectrum(
            args.spectrum_id, args.device, args.product_id,
            args.start_sec, args.T, args.L, _read_counts(args.counts))
        _print({"imported": args.spectrum_id})
    elif cmd == "spectrum-list":
        _print(repo.list_spectra())
    elif cmd == "quantify":
        windows = _parse_windows(args.windows)
        _print(repo.quantify_windows(args.spectrum_id, windows, args.eff,
                                     args.lam, args.t_ref))
    elif cmd == "quantify-net":
        lo, hi = _parse_windows([args.window])[0]
        _print(repo.quantify_net(args.sample_id, args.background_id, lo, hi,
                                 args.eff, args.lam, args.t_ref))
    elif cmd == "verify":
        _print(repo.verify_result(args.result_id))
    elif cmd == "result-list":
        _print(repo.list_results())
    return 0
