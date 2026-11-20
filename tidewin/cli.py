"""Unified entry points (CLI).

Subcommands map to the required deliverables:

  publish   edit / publish a tide-model revision to the catalog
  revisions list revisions and the current one for a port
  level     query the water level at an instant, with per-term breakdown
  extrema   interval extrema / minimum for a port model
  windows   solve passage windows for a channel + vessel + departure range
  explain   per-boundary explanation (segment, tide instant, residual)
  run-all   run all 30 requests in the scenarios file
  verify    run the independent verification suite

Run ``python -m tidewin.cli <subcommand> ...``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .catalog import TideCatalog
from .io import channel_from_dict, request_from_dict
from .rational import F
from .run import DATA, format_result, load_catalog, load_scenarios
from .vault import solve_request

CATALOG_PATH = os.path.join(DATA, "catalog.json")


def _save_catalog(cat: TideCatalog):
    with open(CATALOG_PATH, "w") as f:
        json.dump(cat.to_dict(), f, indent=2)


def _parse_series(text, name):
    """Accept ``[a1,a2,a3,a4]`` (bare fractions or JSON) or ``k:v,k:v``."""
    if text is None:
        return None
    text = text.strip()
    if text.startswith("["):
        inner = text.strip()[1:-1].strip()
        if not inner:
            return []
        parts = [p.strip() for p in inner.split(",")]
        # bare fractions like 3/2 are not valid JSON, so parse each as rational
        return [p for p in parts]
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        k, v = part.split(":")
        out[int(k.strip())] = v.strip()
    return out


def cmd_publish(args):
    cat = load_catalog()
    a = _parse_series(args.a, "a")
    c = _parse_series(args.c, "c")
    model = cat.publish(args.port, F(args.b), a or [0, 0, 0, 0],
                        c or [0, 0, 0, 0], revision=args.revision,
                        note=args.note)
    _save_catalog(cat)
    print(f"published port={model.port} revision={model.revision} "
          f"b={args.b}")


def cmd_revisions(args):
    cat = load_catalog()
    for port in ([args.port] if args.port else cat.ports()):
        revs = cat.revisions(port)
        cur = cat.current_revision(port)
        print(f"{port}: revisions={revs} current={cur}")
        for r in revs:
            print(f"    rev {r}: {cat.get_model(port, r).to_dict()}")


def cmd_level(args):
    cat = load_catalog()
    print(json.dumps(cat.level_at(args.port, F(args.time),
                                  revision=args.revision), indent=2))


def cmd_extrema(args):
    cat = load_catalog()
    print(json.dumps(cat.extrema(args.port, F(args.lo), F(args.hi),
                                 revision=args.revision), indent=2))


def _load_scenarios():
    return load_scenarios()


def cmd_windows(args):
    cat = load_catalog()
    sc = _load_scenarios()
    if args.request:
        rd = next(r for r in sc["requests"] if r["id"] == args.request)
        req = request_from_dict(rd)
        chname = req["channel"]
        ch = next(channel_from_dict(c) for c in sc["channels"]
                  if c["name"] == chname)
        res = solve_request(cat, ch, req["vessel"], req["S"], req["E"])
        res["id"] = req["id"]
        res["note"] = req["note"]
    else:
        ch = next(channel_from_dict(c) for c in sc["channels"]
                  if c["name"] == args.channel)
        v = request_from_dict({"channel": args.channel, "draft": args.draft,
                               "margin": args.margin, "S": args.S, "E": args.E,
                               "id": "manual"})["vessel"]
        res = solve_request(cat, ch, v, F(args.S), F(args.E))
    print(format_result(res))
    if args.json:
        print(json.dumps(res, indent=2))


def cmd_explain(args):
    cat = load_catalog()
    sc = _load_scenarios()
    rd = next(r for r in sc["requests"] if r["id"] == args.request)
    req = request_from_dict(rd)
    ch = next(channel_from_dict(c) for c in sc["channels"]
              if c["name"] == req["channel"])
    res = solve_request(cat, ch, req["vessel"], req["S"], req["E"])
    res["id"] = req["id"]
    res["note"] = req["note"]
    print(format_result(res))
    print("\nFull boundary records:")
    for w in res["windows"]:
        print(json.dumps({"window": [w["start"], w["end"]],
                          "start_boundary": w["start_boundary"],
                          "end_boundary": w["end_boundary"]}, indent=2))


def cmd_run_all(args):
    from .run import main as run_main
    run_main()


def cmd_verify(args):
    from . import verify
    argv = []
    if args.falsify:
        argv.append("--falsify")
    argv.append(f"--falsify-step={args.falsify_step}")
    return verify.main(argv)


def build_parser():
    p = argparse.ArgumentParser(prog="tidewin", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("publish", help="publish a model revision")
    sp.add_argument("port")
    sp.add_argument("--b", required=True)
    sp.add_argument("--a", default=None, help='"[a1,a2,a3,a4]" or "1:1/2,3:1/3"')
    sp.add_argument("--c", default=None)
    sp.add_argument("--revision", type=int, default=None)
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("revisions", help="list revisions")
    sp.add_argument("port", nargs="?", default=None)
    sp.set_defaults(func=cmd_revisions)

    sp = sub.add_parser("level", help="level at an instant")
    sp.add_argument("port")
    sp.add_argument("time")
    sp.add_argument("--revision", type=int, default=None)
    sp.set_defaults(func=cmd_level)

    sp = sub.add_parser("extrema", help="interval extrema")
    sp.add_argument("port")
    sp.add_argument("lo")
    sp.add_argument("hi")
    sp.add_argument("--revision", type=int, default=None)
    sp.set_defaults(func=cmd_extrema)

    sp = sub.add_parser("windows", help="passage windows")
    sp.add_argument("--channel", default=None)
    sp.add_argument("--draft", default=None)
    sp.add_argument("--margin", default=None)
    sp.add_argument("--S", default=None)
    sp.add_argument("--E", default=None)
    sp.add_argument("--request", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_windows)

    sp = sub.add_parser("explain", help="boundary explanation for a request")
    sp.add_argument("request")
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("run-all", help="run all scenarios")
    sp.set_defaults(func=cmd_run_all)

    sp = sub.add_parser("verify", help="run verification suite")
    sp.add_argument("--falsify", action="store_true",
                    help="also run the whole-dataset dense grid falsifier")
    sp.add_argument("--falsify-step", type=float, default=0.01,
                    help="grid step (hours) for the falsifier")
    sp.set_defaults(func=cmd_verify)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
