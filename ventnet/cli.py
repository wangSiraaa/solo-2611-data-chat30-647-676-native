"""Command-line interface for VentNet.

Commands
--------
* ``catalog-validate``  validate a catalog product file and print its identity
* ``catalog-publish``   publish a catalog product file (balance check + identity)
* ``import``            persist an experiment (inputs only) into the store
* ``run``               integrate an experiment, write result and mass check
* ``explain``           show per-integral segment/room-state explanations
* ``verify``            run all scenarios + analytic cross-checks
* ``demo``              run every synthetic scenario end to end
"""

from __future__ import annotations

import argparse
import json
import sys

from .catalog import Catalog
from .db import Store
from .examples import SCENARIOS, build


def _load_json(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _dump(obj):
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def cmd_catalog_validate(args):
    product = _load_json(args.path)
    cat = Catalog.from_product(product)
    report = cat.validate()
    report["identity"] = cat.identity
    _dump(report)
    return 0 if report["balanced"] else 1


def cmd_catalog_publish(args):
    product = _load_json(args.path)
    cat = Catalog.from_product(product)  # from_product already publishes/validates
    out = {
        "identity": cat.identity,
        "revision": cat.revision,
        "product": cat.to_dict(),
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
        print(f"published catalog {cat.identity} -> {args.out}")
    else:
        _dump(out)
    return 0


def cmd_import(args):
    store = Store(args.db)
    product = _load_json(args.catalog)
    cat = Catalog.from_product(product)
    store.save_catalog(cat)
    spec = _load_json(args.experiment)
    exp = store.import_experiment(
        catalog=cat,
        initial_concentration=spec["initial_concentration"],
        source_events=spec.get("source_events", []),
        window=spec["window"],
        visits=spec.get("visits", []),
    )
    _dump({"experiment_identity": exp.identity, "catalog_identity": cat.identity})
    return 0


def cmd_run(args):
    store = Store(args.db)
    payload = store.run(args.experiment_identity)
    _dump(
        {
            "experiment_identity": payload["identity"],
            "catalog_identity": payload["catalog_identity"],
            "mass_balance": payload["mass_balance"],
            "person_totals": payload["person_totals"],
        }
    )
    return 0 if payload["mass_balance"]["ok"] else 2


def cmd_explain(args):
    store = Store(args.db)
    payload = store.get_result(args.experiment_identity)
    _dump(payload["explanations"])
    return 0


def cmd_demo(args):
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(f"{tmp}/demo.db")
        summary = {}
        for name in SCENARIOS:
            if name == "legit_flow_correction":
                cat, exp, revision = build(name)
            else:
                cat, exp = build(name)
                revision = None
            store.save_catalog(cat)
            e = store.import_experiment(
                catalog=cat,
                initial_concentration=exp["initial_concentration"],
                source_events=exp["source_events"],
                window=exp["window"],
                visits=exp["visits"],
            )
            result = store.run(e.identity)
            entry = {
                "catalog_identity": cat.identity,
                "experiment_identity": e.identity,
                "mass_ok": result["mass_balance"]["ok"],
                "relative_residual": result["mass_balance"]["relative_residual"],
                "max_abs_error_estimate": result["max_abs_error_estimate"],
            }
            if revision:
                rev = store.revise_flows(cat.identity, revision)
                e2 = store.import_experiment(
                    catalog=rev,
                    initial_concentration=exp["initial_concentration"],
                    source_events=exp["source_events"],
                    window=exp["window"],
                    visits=exp["visits"],
                )
                result2 = store.run(e2.identity)
                entry["revision_identity"] = rev.identity
                entry["neighbor_curve_changed"] = _curve_changed(result, result2, "B")
                old = store.get_result(e.identity)
                entry["old_result_kept_old_basis"] = (
                    old["catalog_identity"] == cat.identity
                )
            summary[name] = entry
        _dump(summary)
        store.close()
    return 0


def _curve_changed(a, b, room):
    ca = a["concentrations"][room]
    cb = b["concentrations"][room]
    return max(abs(x - y) for x, y in zip(ca, cb)) > 1e-6


def cmd_verify(args):
    """Run scenarios, analytic cross-checks, and invariant checks."""
    from .verify import verify_all

    results = verify_all()
    _dump(results)
    return 0 if results["ok"] else 1


def build_parser():
    p = argparse.ArgumentParser(prog="ventnet", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser("catalog-validate", help="validate a catalog product file")
    pv.add_argument("path")
    pv.set_defaults(func=cmd_catalog_validate)

    pp = sub.add_parser("catalog-publish", help="publish a catalog product file")
    pp.add_argument("path")
    pp.add_argument("--out", default=None)
    pp.set_defaults(func=cmd_catalog_publish)

    pi = sub.add_parser("import", help="persist an experiment into the store")
    pi.add_argument("--db", required=True)
    pi.add_argument("--catalog", required=True)
    pi.add_argument("--experiment", required=True)
    pi.set_defaults(func=cmd_import)

    pr = sub.add_parser("run", help="integrate a stored experiment")
    pr.add_argument("--db", required=True)
    pr.add_argument("--experiment-identity", required=True)
    pr.set_defaults(func=cmd_run)

    pe = sub.add_parser("explain", help="show per-integral explanations")
    pe.add_argument("--db", required=True)
    pe.add_argument("--experiment-identity", required=True)
    pe.set_defaults(func=cmd_explain)

    pd = sub.add_parser("demo", help="run all synthetic scenarios")
    pd.set_defaults(func=cmd_demo)

    pver = sub.add_parser("verify", help="run scenarios and analytic cross-checks")
    pver.set_defaults(func=cmd_verify)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # surface cleanly for CLI use
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
