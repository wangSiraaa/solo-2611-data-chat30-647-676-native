#!/usr/bin/env bash
# Reproduce the full deliverable: tests + 20 MiB end-to-end demo.
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3.11}

echo "== test suite =="
"$PY" -m unittest discover -s tests -v

echo
echo "== end-to-end demo (20 MiB, max 4 connections, <=100 pieces) =="
"$PY" src/demo.py --workdir demo_out --size-mib 20

echo
echo "Artifacts:"
echo "  demo_out/original.bin      fixed-version original (20 MiB)"
echo "  demo_out/original.json     its size / SHA256 / strong ETag"
echo "  demo_out/*/state.json      pinned plan + coverage per scenario"
echo "  demo_out/*/confirmed.jsonl verified-fragment journal"
echo "  demo_out/*/target.bin      published targets"
