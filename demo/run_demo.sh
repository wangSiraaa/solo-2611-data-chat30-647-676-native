#!/usr/bin/env bash
# End-to-end demonstration of the explainable flow warehouse.
#
# Usage: ./demo/run_demo.sh [WORKDIR]
#
# Generates synthetic input from an empty database, runs the full call chain,
# shows explanations, interval bounds and witness replays, then applies a
# timetable correction and re-runs analysis against the new snapshot.

set -euo pipefail
WORK="${1:-/tmp/metro-demo}"
DB="$WORK/metro.db"
rm -rf "$WORK"
mkdir -p "$WORK"

run() { echo; echo "### $*"; "$@"; }
M() { python3 -m metro --db "$DB" "$@"; }

echo "== 1. generate synthetic input =="
python3 -m metro.synth --out "$WORK/full" --small-out "$WORK/small"

echo; echo "== 2. small exhaustive case: import + analyze =="
DB_S="$WORK/small.db"
MS() { python3 -m metro --db "$DB_S" "$@"; }
MS init --reset >/dev/null
MS import-stations "$WORK/small/stations.json"
MS import-timetable "$WORK/small/timetable.json" --name small-v1
MS import-gates "$WORK/small/gates.json" --name small-gates
MS analyze --snapshot 1 --import-id 1 --name small-run

echo; echo "== 3. small: independent brute-force verification =="
python3 tools/verify.py check --input "$WORK/small" --db "$DB_S" --batch 1

echo; echo "== 4. small: audit every stored bound/witness =="
MS audit --batch 1 | python3 -c "import json,sys;d=json.load(sys.stdin);print('audit ok =',d['ok'],'checked',d['n_checked'])"

echo; echo "== 5. full-scale 20 stations / 100 trains / 3000 stops / 1000 trips =="
M init --reset >/dev/null
M import-stations "$WORK/full/stations.json"
M import-timetable "$WORK/full/timetable.json" --name full-v1
M import-gates "$WORK/full/gates.json" --name full-gates
M analyze --snapshot 1 --import-id 1 --name full-run-v1

echo; echo "== 6. anomaly inventory (nothing dropped) =="
M summary --batch 1 | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('trips',d['n_trips'],'explainable',d['n_explainable'],'unexplainable',d['n_unexplainable'])
print('unclosed',len(d['unclosed_entries']),'orphan exits',len(d['orphan_exits']))
"

echo; echo "== 7. explain an anomaly trip (transfer showcase) =="
M trips --batch 1 --out - | python3 -c "
import json,sys
for t in json.load(sys.stdin):
    if t['card_id']=='CARD-TRANSFER':
        print(t['trip_id'])
" > "$WORK/tid.txt"
M explain --batch 1 --trip "$(cat "$WORK/tid.txt")" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('status',d['status'],'plans',len(d['plans']))
for p in d['plans'][:3]:
    print(' ',p['kind'],[f\"{l['train']}[{l['board_seq']}->{l['alight_seq']}]\" for l in p['legs']],
          'transfer' if p['transfer'] else '')
"

echo; echo "== 8. interval bounds + witness for one train =="
M interval --batch 1 --train A01 | python3 -c "
import json,sys
for r in json.load(sys.stdin): print(r['from_seq'],r['station_from'],'->',r['station_to'],'min',r['min_count'],'max',r['max_count'])
" | head -6
M witness --batch 1 --train A01 --from-seq 1 --bound MAX | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('MAX replay ok=',d['ok'],'count',d['replayed_count'],'selection trips',len(d['selection']))
"

echo; echo "== 9. full independent brute-force verification (can take a while) =="
if [ "${SKIP_FULL_VERIFY:-0}" != "1" ]; then
  python3 tools/verify.py check --input "$WORK/full" --db "$DB" --batch 1
else
  echo "skipped (SKIP_FULL_VERIFY=1)"
fi

echo; echo "== 10. timetable correction -> new snapshot -> re-analyze =="
M correct --snapshot 1 --train A07 "$WORK/full/correction.json" --name full-v2 --note "delay A07"
M analyze --snapshot 2 --import-id 1 --name full-run-v2
echo "-- old batch still bound to snapshot 1:"
M summary --batch 1 | python3 -c "import json,sys;d=json.load(sys.stdin);print('batch1 snapshot',d['snapshot_id'],'plans',d['n_plans'])"
M summary --batch 2 | python3 -c "import json,sys;d=json.load(sys.stdin);print('batch2 snapshot',d['snapshot_id'],'plans',d['n_plans'])"

echo; echo "== 11. corrected-run verification against corrected timetable =="
if [ "${SKIP_FULL_VERIFY:-0}" != "1" ]; then
  python3 tools/verify.py correction --input "$WORK/full" --db "$DB" \
      --base-batch 1 --new-batch 2 --train A07 --changes "$WORK/full/correction.json"
fi

echo; echo "== done. database: $DB =="
