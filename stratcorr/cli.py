"""Command line interface for the horizon catalogue and borehole repository.

Two independent sub-application groups::

    stratcorr catalog ...   # horizon partial order, products, provenance
    stratcorr repo ...      # products, boreholes/samples, solving, validation

All commands accept ``--json`` for machine-readable output.  Errors exit with
status 2; a persisted correlation contradiction also exits 2 without removing
any constraint.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import synthetic
from .catalog_service import CatalogError, CatalogService
from .geometry import GeometryError
from .models import BoreholeInput, SampleInput
from .poset import PosetError
from .rational import RationalError
from .repo_db import BoreholeRepository, RepoError
from .storage import StorageError


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def _emit(data, *, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(text if text is not None else json.dumps(
            data, ensure_ascii=False, indent=2, default=str
        ))


def _read_json(path: str) -> dict | list:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    except FileNotFoundError as exc:
        raise ValueError(f"file not found: {path}") from exc


# --------------------------------------------------------------------------- #
# Catalogue commands
# --------------------------------------------------------------------------- #


def cmd_catalog_init(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        service.init()
    finally:
        service.close()
    _emit({"db": args.db, "initialised": True}, as_json=args.json,
          text=f"catalogue initialised at {args.db}")


def cmd_horizon_add(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        service.add_horizon(
            args.horizon_id, args.name, args.lith or [], args.source
        )
    finally:
        service.close()
    _emit({"horizon_id": args.horizon_id}, as_json=args.json,
          text=f"added horizon {args.horizon_id}")


def cmd_horizon_list(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        rows = service.list_horizons()
    finally:
        service.close()
    text = "\n".join(
        f"{r['horizon_id']:6s} {r['name']:16s} lith={','.join(r['acceptable_lithology'])}"
        f"  source={r['source']}" for r in rows
    ) or "(no horizons)"
    _emit(rows, as_json=args.json, text=text)


def cmd_lith_add(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        service.add_lithology(args.horizon_id, args.lith_code, args.source)
    finally:
        service.close()
    _emit({"horizon_id": args.horizon_id, "lith_code": args.lith_code},
          as_json=args.json,
          text=f"added lithology {args.lith_code!r} to {args.horizon_id}")


def cmd_lith_remove(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        service.remove_lithology(args.horizon_id, args.lith_code)
    finally:
        service.close()
    _emit({"horizon_id": args.horizon_id, "removed": args.lith_code},
          as_json=args.json,
          text=f"removed lithology {args.lith_code!r} from {args.horizon_id}")


def cmd_edge_add(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        service.add_edge(args.parent, args.child, args.source)
    finally:
        service.close()
    _emit({"parent": args.parent, "child": args.child}, as_json=args.json,
          text=f"added edge {args.parent} -> {args.child}")


def cmd_edge_remove(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        service.remove_edge(args.parent, args.child)
    finally:
        service.close()
    _emit({"parent": args.parent, "child": args.child, "removed": True},
          as_json=args.json, text=f"removed edge {args.parent} -> {args.child}")


def cmd_edge_list(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        rows = service.list_edges()
    finally:
        service.close()
    text = "\n".join(
        f"{r['parent_id']} -> {r['child_id']}   source={r['source']}" for r in rows
    ) or "(no edges)"
    _emit(rows, as_json=args.json, text=text)


def cmd_catalog_validate(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        report = service.validate_draft()
    finally:
        service.close()
    text = ("VALID" if report["ok"] else "INVALID")
    for kind in ("errors", "warnings"):
        for item in report[kind]:
            text += f"\n{kind[:-1].upper()}: {item}"
    _emit(report, as_json=args.json, text=text)
    if not report["ok"]:
        raise SystemExit(2)


def cmd_catalog_publish(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        product = service.publish(args.note)
    finally:
        service.close()
    _emit({"product_id": product.product_id, "version": product.version},
          as_json=args.json,
          text=f"published {product.product_id} (version {product.version})")


def cmd_product_list(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        rows = service.list_products()
    finally:
        service.close()
    text = "\n".join(
        f"{r['product_id']} v{r['version']} [{r['kind']}] {r['note']}" for r in rows
    ) or "(no products)"
    _emit(rows, as_json=args.json, text=text)


def cmd_catalog_export(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        payload = service.export_product(args.product_id)
    finally:
        service.close()
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        _emit({"product_id": args.product_id, "path": args.out}, as_json=args.json,
              text=f"exported {args.product_id} to {args.out}")
    else:
        print(payload)


def cmd_relation(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        data = service.query_relation(
            args.shallower, args.deeper, args.product
        )
    finally:
        service.close()
    if data["shallower_than"]:
        text = (f"{args.shallower} shallower_than {args.deeper}: "
                f"path {' -> '.join(data['witness_path'])}")
    else:
        text = f"no shallower_than path from {args.shallower} to {args.deeper}"
    _emit(data, as_json=args.json, text=text)


def cmd_evidence(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        data = service.query_evidence(args.left, args.right, args.product)
    finally:
        service.close()
    _emit(data, as_json=args.json, text=data["explanation"])


def cmd_sequence(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        data = service.validate_sequence(args.horizons, args.product)
    finally:
        service.close()
    text = "VALID SEQUENCE" if data["valid"] else "INVALID SEQUENCE"
    for violation in data["violations"]:
        text += f"\n- {violation}"
    _emit(data, as_json=args.json, text=text)
    if not data["valid"]:
        raise SystemExit(2)


def cmd_provenance(args: argparse.Namespace) -> None:
    service = CatalogService(args.db)
    try:
        data = service.provenance(args.product_id)
    finally:
        service.close()
    _emit(data, as_json=args.json)


# --------------------------------------------------------------------------- #
# Repository commands
# --------------------------------------------------------------------------- #


def cmd_repo_init(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        repo.init()
    finally:
        repo.close()
    _emit({"db": args.db, "initialised": True}, as_json=args.json,
          text=f"repository initialised at {args.db}")


def cmd_repo_import_catalog_product(args: argparse.Namespace) -> None:
    catalog = CatalogService(args.catalog_db)
    try:
        product = catalog.get_product(args.product_id)
    finally:
        catalog.close()
    repo = BoreholeRepository(args.db)
    try:
        repo.import_product(product, origin=f"catalog:{args.catalog_db}")
    finally:
        repo.close()
    _emit({"product_id": product.product_id}, as_json=args.json,
          text=f"imported local product {product.product_id}")


def cmd_repo_import_external(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        product = repo.import_product_file(args.path)
    finally:
        repo.close()
    _emit({"product_id": product.product_id, "kind": "external"},
          as_json=args.json,
          text=f"imported external product {product.product_id}")


def cmd_repo_products(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        rows = repo.list_products()
    finally:
        repo.close()
    text = "\n".join(
        f"{r['product_id']} v{r['version']} [{r['kind']}] origin={r['origin']}"
        for r in rows
    ) or "(no products)"
    _emit(rows, as_json=args.json, text=text)


def cmd_repo_import_borehole(args: argparse.Namespace) -> None:
    data = _read_json(args.path)
    items = data if isinstance(data, list) else [data]
    repo = BoreholeRepository(args.db)
    try:
        imported = []
        for item in items:
            borehole = BoreholeInput.from_dict(item)
            repo.import_borehole(borehole)
            imported.append(borehole.borehole_id)
    finally:
        repo.close()
    _emit({"imported": imported}, as_json=args.json,
          text=f"imported boreholes: {', '.join(imported)}")


def cmd_repo_import_sample(args: argparse.Namespace) -> None:
    data = _read_json(args.path)
    items = data if isinstance(data, list) else [data]
    repo = BoreholeRepository(args.db)
    try:
        imported = []
        for item in items:
            sample = SampleInput.from_dict(item)
            repo.import_sample(sample)
            imported.append(sample.sample_id)
    finally:
        repo.close()
    _emit({"imported": imported}, as_json=args.json,
          text=f"imported samples: {', '.join(imported)}")


def cmd_repo_boreholes(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        rows = repo.list_boreholes()
    finally:
        repo.close()
    text = "\n".join(
        f"{r['borehole_id']:6s} segments={r['segment_count']:2d} gaps={r['gap_count']}"
        for r in rows
    ) or "(no boreholes)"
    _emit(rows, as_json=args.json, text=text)


def cmd_repo_solve(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        result = repo.solve(args.product_id, note=args.note)
    finally:
        repo.close()
    if result["status"] == "contradiction":
        text = f"CONTRADICTION, stored as {result['scheme_id']}"
        for item in result["contradictions"]:
            text += f"\n- [{item['borehole_id']}/{item['kind']}] {item['message']}"
        _emit(result, as_json=args.json, text=text)
        raise SystemExit(2)
    text = (
        f"scheme {result['scheme_id']} selected "
        f"(UNKNOWN weight={result['total_unknown_weight']}, "
        f"switches={result['total_switches']})"
    )
    _emit(result, as_json=args.json, text=text)


def cmd_repo_schemes(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        rows = repo.list_schemes()
    finally:
        repo.close()
    text = "\n".join(
        f"{r['scheme_id']} product={r['product_id']} status={r['status']} "
        f"unknown={r['total_unknown_weight']} switches={r['total_switches']}"
        for r in rows
    ) or "(no schemes)"
    _emit(rows, as_json=args.json, text=text)


def cmd_repo_assignments(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        rows = repo.get_assignments(args.scheme_id)
    finally:
        repo.close()
    text = "\n".join(
        f"{r['borehole_id']}/{r['segment_id']:8s} -> "
        f"{'UNKNOWN' if r['is_unknown'] else r['horizon_id']}"
        for r in rows
    )
    _emit(rows, as_json=args.json, text=text)


def cmd_repo_stats(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        rows = repo.get_stats(args.scheme_id)
    finally:
        repo.close()
    text = "\n".join(
        f"{r['borehole_id']}/{r['horizon_id']} top={r['top_depth']} "
        f"bottom={r['bottom_depth']} cored={r['cored_thickness']} "
        f"gap={r['gap_thickness']}"
        for r in rows
    ) or "(no statistics)"
    _emit(rows, as_json=args.json, text=text)


def cmd_repo_gaps(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        rows = repo.get_gap_attribution(args.scheme_id)
    finally:
        repo.close()
    text = "\n".join(
        f"{r['borehole_id']}/{r['gap_id']} [{r['start_depth']},{r['end_depth']}) "
        f"-> {r['horizon_id'] or 'UNDETERMINED'} ({r['reason']})"
        for r in rows
    )
    _emit(rows, as_json=args.json, text=text)


def cmd_repo_samples(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        rows = repo.get_sample_membership(args.scheme_id)
    finally:
        repo.close()
    text = "\n".join(
        f"{r['sample_id']:6s} depth={r['depth']:6s} {r['status']:11s} "
        f"horizon={r['horizon_id'] or '-'} segment={r['segment_id'] or '-'} "
        f"gap={r['gap_id'] or '-'}"
        for r in rows
    )
    _emit(rows, as_json=args.json, text=text)


def cmd_repo_explain(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        data = repo.explain(args.scheme_id)
    finally:
        repo.close()
    _emit(data, as_json=args.json)


def cmd_repo_validate(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        report = repo.validate(args.scheme_id)
    finally:
        repo.close()
    text = "SCHEME VALID" if report["ok"] else "SCHEME INVALID"
    for error in report["errors"]:
        text += f"\n- {error}"
    _emit(report, as_json=args.json, text=text)
    if not report["ok"]:
        raise SystemExit(2)


def cmd_repo_provenance(args: argparse.Namespace) -> None:
    repo = BoreholeRepository(args.db)
    try:
        data = repo.product_provenance(args.product_id)
    finally:
        repo.close()
    _emit(data, as_json=args.json)


def cmd_demo(args: argparse.Namespace) -> None:
    """End-to-end synthetic run inside a fresh temporary directory."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="stratcorr-demo-"))
    catalog_db, repo_db = tmp / "catalog.db", tmp / "repo.db"

    catalog = CatalogService(catalog_db)
    catalog.init()
    product = synthetic.build_product()
    for horizon in product.horizons:
        catalog.add_horizon(
            horizon.horizon_id,
            horizon.name,
            sorted(horizon.acceptable_lithology),
            horizon.source,
        )
    for edge in product.edges:
        catalog.add_edge(edge.parent_id, edge.child_id, edge.source)
    published = catalog.publish("initial synthetic product")
    catalog.close()

    repo = BoreholeRepository(repo_db)
    repo.init()
    local = CatalogService(catalog_db).get_product(published.product_id)
    repo.import_product(local, origin=f"catalog:{catalog_db}")
    boreholes = synthetic.build_boreholes()
    feasible = [b for b in boreholes if b.borehole_id != "B4"]
    for borehole in feasible:
        repo.import_borehole(borehole)
    for sample in synthetic.build_samples():
        repo.import_sample(sample)
    first = repo.solve(published.product_id)

    # A real correction: remove the H5 -> H6 edge and re-publish.  H6 becomes
    # unreachable, so LIMESTONE segments genuinely flip to UNKNOWN and their
    # sample membership changes; no hard fixation references H6.
    catalog = CatalogService(catalog_db)
    catalog.remove_edge("H5", "H6")
    corrected = catalog.publish("removed H5->H6 after source correction")
    catalog.close()
    repo.import_product(corrected, origin=f"catalog:{catalog_db}")
    second = repo.solve(corrected.product_id)

    # A cyclic correction must be refused, never partially published.
    catalog = CatalogService(catalog_db)
    cyclic_note = ""
    try:
        catalog.add_edge("H5", "H2", "synthetic:bad-correction")
    except CatalogError as exc:
        cyclic_note = str(exc)
    catalog.close()

    # Contradiction record with the fixed-order borehole included.
    repo.import_borehole(
        next(b for b in boreholes if b.borehole_id == "B4")
    )
    contradiction = repo.solve(corrected.product_id)
    report = {
        "workdir": str(tmp),
        "first_scheme": first,
        "second_scheme": second,
        "cyclic_correction_refused": cyclic_note,
        "contradiction_scheme": contradiction,
        "old_scheme_still_available": [s["scheme_id"] for s in repo.list_schemes()],
    }
    repo.close()
    _emit(report, as_json=args.json)


# --------------------------------------------------------------------------- #
# Argument parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stratcorr",
        description="Horizon catalogue and borehole correlation repository",
    )
    parser.add_argument(
        "--json", action="store_true", default=False,
        help="emit machine-readable JSON instead of text tables",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    # ---- catalogue ----------------------------------------------------- #
    cat = sub.add_parser("catalog", help="horizon catalogue service")
    cat_sub = cat.add_subparsers(dest="command", required=True)

    p = cat_sub.add_parser("init", help="initialise a catalogue database")
    p.add_argument("db")
    p.set_defaults(func=cmd_catalog_init)

    p = cat_sub.add_parser("horizon-add", help="add a horizon")
    p.add_argument("db")
    p.add_argument("horizon_id")
    p.add_argument("name")
    p.add_argument("--lith", action="append", help="acceptable lithology code")
    p.add_argument("--source", required=True)
    p.set_defaults(func=cmd_horizon_add)

    p = cat_sub.add_parser("horizon-list", help="list horizons")
    p.add_argument("db")
    p.set_defaults(func=cmd_horizon_list)

    p = cat_sub.add_parser("lith-add", help="add an accepted lithology code")
    p.add_argument("db")
    p.add_argument("horizon_id")
    p.add_argument("lith_code")
    p.add_argument("--source", required=True)
    p.set_defaults(func=cmd_lith_add)

    p = cat_sub.add_parser("lith-remove", help="remove an accepted lithology code")
    p.add_argument("db")
    p.add_argument("horizon_id")
    p.add_argument("lith_code")
    p.set_defaults(func=cmd_lith_remove)

    p = cat_sub.add_parser("edge-add", help="add a shallower_than edge")
    p.add_argument("db")
    p.add_argument("parent")
    p.add_argument("child")
    p.add_argument("--source", required=True)
    p.set_defaults(func=cmd_edge_add)

    p = cat_sub.add_parser("edge-remove", help="remove a shallower_than edge")
    p.add_argument("db")
    p.add_argument("parent")
    p.add_argument("child")
    p.set_defaults(func=cmd_edge_remove)

    p = cat_sub.add_parser("edge-list", help="list direct edges")
    p.add_argument("db")
    p.set_defaults(func=cmd_edge_list)

    p = cat_sub.add_parser("validate", help="validate the draft catalogue")
    p.add_argument("db")
    p.set_defaults(func=cmd_catalog_validate)

    p = cat_sub.add_parser("publish", help="publish an immutable product")
    p.add_argument("db")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_catalog_publish)

    p = cat_sub.add_parser("product-list", help="list published products")
    p.add_argument("db")
    p.set_defaults(func=cmd_product_list)

    p = cat_sub.add_parser("export", help="export a product as JSON")
    p.add_argument("db")
    p.add_argument("product_id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_catalog_export)

    p = cat_sub.add_parser("relation", help="transitive shallower_than query")
    p.add_argument("db")
    p.add_argument("shallower")
    p.add_argument("deeper")
    p.add_argument("--product", default=None)
    p.set_defaults(func=cmd_relation)

    p = cat_sub.add_parser("evidence", help="order/incomparability evidence")
    p.add_argument("db")
    p.add_argument("left")
    p.add_argument("right")
    p.add_argument("--product", default=None)
    p.set_defaults(func=cmd_evidence)

    p = cat_sub.add_parser("sequence", help="validate a horizon sequence")
    p.add_argument("db")
    p.add_argument("horizons", nargs="+")
    p.add_argument("--product", default=None)
    p.set_defaults(func=cmd_sequence)

    p = cat_sub.add_parser("provenance", help="show product relation sources")
    p.add_argument("db")
    p.add_argument("product_id")
    p.set_defaults(func=cmd_provenance)

    # ---- repository ---------------------------------------------------- #
    rps = sub.add_parser("repo", help="borehole analysis repository")
    repo_sub = rps.add_subparsers(dest="command", required=True)

    p = repo_sub.add_parser("init", help="initialise a repository database")
    p.add_argument("db")
    p.set_defaults(func=cmd_repo_init)

    p = repo_sub.add_parser("import-catalog-product",
                            help="consume a local catalogue product")
    p.add_argument("db")
    p.add_argument("catalog_db")
    p.add_argument("product_id")
    p.set_defaults(func=cmd_repo_import_catalog_product)

    p = repo_sub.add_parser("import-external",
                            help="import an external product JSON file")
    p.add_argument("db")
    p.add_argument("path")
    p.set_defaults(func=cmd_repo_import_external)

    p = repo_sub.add_parser("products", help="list consumed products")
    p.add_argument("db")
    p.set_defaults(func=cmd_repo_products)

    p = repo_sub.add_parser("import-borehole", help="import borehole JSON")
    p.add_argument("db")
    p.add_argument("path")
    p.set_defaults(func=cmd_repo_import_borehole)

    p = repo_sub.add_parser("import-sample", help="import sample JSON")
    p.add_argument("db")
    p.add_argument("path")
    p.set_defaults(func=cmd_repo_import_sample)

    p = repo_sub.add_parser("boreholes", help="list boreholes")
    p.add_argument("db")
    p.set_defaults(func=cmd_repo_boreholes)

    p = repo_sub.add_parser("solve", help="run the exact correlation solver")
    p.add_argument("db")
    p.add_argument("product_id")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_repo_solve)

    p = repo_sub.add_parser("schemes", help="list stored schemes")
    p.add_argument("db")
    p.set_defaults(func=cmd_repo_schemes)

    p = repo_sub.add_parser("assignments", help="show segment assignments")
    p.add_argument("db")
    p.add_argument("scheme_id")
    p.set_defaults(func=cmd_repo_assignments)

    p = repo_sub.add_parser("stats", help="show per-horizon geometry stats")
    p.add_argument("db")
    p.add_argument("scheme_id")
    p.set_defaults(func=cmd_repo_stats)

    p = repo_sub.add_parser("gaps", help="show gap attribution")
    p.add_argument("db")
    p.add_argument("scheme_id")
    p.set_defaults(func=cmd_repo_gaps)

    p = repo_sub.add_parser("samples", help="show sample membership")
    p.add_argument("db")
    p.add_argument("scheme_id")
    p.set_defaults(func=cmd_repo_samples)

    p = repo_sub.add_parser("explain", help="explain a stored scheme")
    p.add_argument("db")
    p.add_argument("scheme_id")
    p.set_defaults(func=cmd_repo_explain)

    p = repo_sub.add_parser("validate", help="independently re-validate a scheme")
    p.add_argument("db")
    p.add_argument("scheme_id")
    p.set_defaults(func=cmd_repo_validate)

    p = repo_sub.add_parser("provenance", help="show consumed product provenance")
    p.add_argument("db")
    p.add_argument("product_id")
    p.set_defaults(func=cmd_repo_provenance)

    # ---- synthetic demo ------------------------------------------------ #
    p = sub.add_parser("demo", help="run the synthetic end-to-end demonstration")
    p.set_defaults(func=cmd_demo)

    # Accept --json either before or after the subcommand.  Nested parsers use
    # SUPPRESS as their default so an omitted flag never overwrites the value
    # the top-level parser may already have parsed.
    def _add_json(parser: argparse.ArgumentParser, *, top: bool = True) -> None:
        if not top:
            parser.add_argument(
                "--json", action="store_true", default=argparse.SUPPRESS,
                help="emit machine-readable JSON instead of text tables",
            )
        for action in parser._actions:
            choices = getattr(action, "choices", None)
            if not choices:
                continue
            for nested in choices.values():
                if isinstance(nested, argparse.ArgumentParser):
                    _add_json(nested, top=False)

    _add_json(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "json", False):
        args.json = False
    try:
        args.func(args)
    except (
        CatalogError,
        RepoError,
        GeometryError,
        PosetError,
        RationalError,
        StorageError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
