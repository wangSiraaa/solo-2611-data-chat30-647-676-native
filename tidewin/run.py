"""Run all passage requests from data/scenarios.json against the catalog."""
from __future__ import annotations

import json
import os

from .catalog import TideCatalog
from .io import channel_from_dict, request_from_dict
from .vault import solve_request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
RESULTS = os.path.join(HERE, "results")


def load_catalog(path=None):
    path = path or os.path.join(DATA, "catalog.json")
    with open(path) as f:
        return TideCatalog.from_dict(json.load(f))


def load_scenarios(path=None):
    path = path or os.path.join(DATA, "scenarios.json")
    with open(path) as f:
        return json.load(f)


def run_all(catalog=None, scenarios=None):
    catalog = catalog or load_catalog()
    scenarios = scenarios or load_scenarios()
    channels = {c["name"]: channel_from_dict(c) for c in scenarios["channels"]}
    results = []
    for rd in scenarios["requests"]:
        req = request_from_dict(rd)
        ch = channels[req["channel"]]
        res = solve_request(catalog, ch, req["vessel"], req["S"], req["E"])
        res["id"] = req["id"]
        res["note"] = req["note"]
        results.append(res)
    return results


def format_result(res: dict) -> str:
    lines = [f"[{res['id']}] channel={res['channel']}  "
             f"range=[{res['S']:.6g},{res['E']:.6g}]  {res['status']}"]
    if res["note"]:
        lines.append(f"    note: {res['note']}")
    for w in res["windows"]:
        tag = "ISOLATED-POINT" if w["isolated"] else "INTERVAL"
        lines.append(f"    {tag} s in [{w['start']:.9f}, {w['end']:.9f}] "
                     f"(width {w['width']:.9f} h)")
        for lbl, b in (("start", w["start_boundary"]),
                       ("end", w["end_boundary"])):
            if not b:
                continue
            if b["kind"] == "tide":
                lines.append(
                    f"        {lbl} boundary: tide constraint, segment "
                    f"{b['segment_index']} (port {b['port']} rev {b['revision']}), "
                    f"binding tide_time={b['tide_time']:.9f} h, "
                    f"residual={b['residual']:.3e}")
            else:
                lines.append(
                    f"        {lbl} boundary: range limit (request S/E), "
                    f"residual={b['residual']:.3e}")
    return "\n".join(lines)


def main():
    results = run_all()
    text = []
    for res in results:
        block = format_result(res)
        print(block)
        print()
        text.append(block)
    feas = sum(1 for r in results if r["status"] == "FEASIBLE")
    summary = ("=" * 70 + "\n" +
               f"{feas}/{len(results)} requests feasible; "
               f"{sum(r['n_isolated'] for r in results)} isolated points total\n")
    print(summary)
    text.append(summary)

    # persist machine-readable + human-readable artifacts
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "passage_windows.json"), "w") as f:
        json.dump(results, f, indent=2)
    with open(os.path.join(RESULTS, "passage_windows.txt"), "w") as f:
        f.write("\n\n".join(text))


if __name__ == "__main__":
    main()
