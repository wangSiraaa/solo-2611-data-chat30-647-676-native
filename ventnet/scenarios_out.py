"""Generate example catalog/experiment JSON files for the CLI.

Usage::

    python -m ventnet.scenarios_out examples/
"""

from __future__ import annotations

import json
import os
import sys

from .examples import SCENARIOS, build


def main(outdir: str = "examples") -> int:
    os.makedirs(outdir, exist_ok=True)
    manifest = {}
    for name in SCENARIOS:
        built = build(name)
        if name == "legit_flow_correction":
            cat, exp, revision = built
            rev_payload = {f"{s}->{d}": f for (s, d), f in revision.items()}
        else:
            cat, exp = built
            rev_payload = None
        cat_path = os.path.join(outdir, f"{name}.catalog.json")
        exp_path = os.path.join(outdir, f"{name}.experiment.json")
        with open(cat_path, "w", encoding="utf-8") as fh:
            json.dump(cat.to_dict(), fh, indent=2, sort_keys=True)
        with open(exp_path, "w", encoding="utf-8") as fh:
            json.dump(exp, fh, indent=2, sort_keys=True)
        manifest[name] = {
            "catalog": cat_path,
            "experiment": exp_path,
            "catalog_identity": cat.identity,
        }
        if rev_payload:
            manifest[name]["balanced_revision"] = rev_payload
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    print(f"wrote {len(SCENARIOS)} scenarios to {outdir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
