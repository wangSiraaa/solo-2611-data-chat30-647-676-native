# Explainable metro gate-to-train interval flows

An offline Python data warehouse that turns anonymous gate swipes plus an
actual timetable into **traceable trip explanations** and **train interval
passenger bounds that are honest about ambiguity**.

A gate record tells you *where* a passenger entered and exited, not *which
train* they took. This tool enumerates every ride plan that is feasible under
explicit, auditable rules, keeps the ambiguity, and reports per-interval
minimum/maximum passenger counts **with a complete witness selection** for
each bound — never a single "reconstructed load".

No fares, no forecasting, no web UI.

---

## 1. Data model and rules

### Gate events
Fields: `event_id`, `card_id`, `station`, integer-second `ts`, `direction`
(`IN`/`OUT`).

Pairing is per card, ordered by `(ts, event_id)`:
- `IN` opens a pending entry; the next `OUT` closes it into a trip.
- A new `IN` while one is pending marks the **old entry unclosed** (preserved,
  not deleted) and takes over the pending slot.
- An `OUT` with no pending entry is preserved as an **orphan exit**.
- Same `event_id` with identical content → de-duplicated; same `event_id`
  with different content → the **entire batch is rejected**. No anomalous
  record is dropped to force a pairing.

### Timetable
Each train has a stop sequence of `(train_id, seq, station, arr_ts, dep_ts)`.
- `seq` (stop identity) is authoritative; **never** collapsed to `station`.
  A train may visit the same station many times (loop lines).
- Per stop `arr <= dep`; between adjacent stops `dep(prev) <= arr(next)`.
- Sequences must be contiguous from 1.
- Any invalid import/correction rejects the whole batch.
- Stations carry `entry_walk`, `exit_walk`, `min_transfer`.

The timetable module works with **no gate data**: import, correct, freeze
snapshots, and query station times.

### Trip explanation
For each paired trip `(entry station a @ t_in) -> (exit station b @ t_out)`:
- board departure `>= t_in + entry_walk(a)`
- final alight arrival `+ exit_walk(b) <= t_out`
- one transfer at most, on **two different trains**
- transfer: `depart(train2) - arrive(train1) >= min_transfer(m)`, same station `m`
- every leg boards strictly before it alights (`board_seq < alight_seq`)
- no out-of-station walking is ever invented (entry/exit/transfer stations
  must match exactly)
- plans de-duplicated by `(train, board_seq, alight_seq)` per leg

The full feasible set is stored. **No nearest-train pick, no proportional
allocation.** Empty set → trip marked `UNEXPLAINABLE` with diagnostics, kept
and reported, never dropped.

### Interval bounds (`min`/`max`)
For every adjacent interval `(train, seq -> seq+1)`:

- `max` = number of explainable trips having **at least one** plan crossing it
- `min` = number of explainable trips having **every** plan crossing it

These are extrema over **independent per-trip choices**. Different intervals
(and min vs max) may come from different explanations; they are not stitched
into one physical load. Each bound stores a deterministic witness rule plus
override count and digest; the CLI can replay it against the persisted plans.

---

## 2. Layout

```
metro/
  db.py         SQLite schema + helpers
  model.py      shared dataclasses
  catalog.py    timetable registry: import/correct/snapshot/query   (source module 1)
  gate.py       gate import + anomaly-preserving pairing
  analysis.py   candidate enumeration + interval bounds/witnesses  (source module 2)
  queries.py    read-side: batches, explanations, intervals, witness replay, audit
  synth.py      synthetic data generator (full + small edge-case sets)
  cli.py        command line
tools/verify.py independent brute-force cross-check (no production imports)
demo/run_demo.sh end-to-end demonstration
tests/test_metro.py unit tests
```

The call chain for analysis is
`load_snapshot -> SnapshotIndex -> pairing -> explain_trips -> persist_analysis`.

### Persistence and correction cost
Everything is batch-scoped in SQLite: raw events, snapshots (immutable),
pairing results, candidate plans, per-plan interval uses, interval bounds and
witness definitions. Reopen the DB and every query answers identically.

A timetable correction validates the whole batch, writes a **new immutable
snapshot** (linked to its base), and analysis creates a **new batch**. Old
batches keep pointing at their old snapshot and remain queryable — corrections
genuinely change later candidates/bounds without rewriting history.

---

## 3. Commands

`--db PATH` is global. Inputs are JSON files (or `-` for stdin). Outputs go to
stdout or `--out FILE`.

```bash
# schema
python3 -m metro --db m.db init --reset

# stations and timetable (freeze snapshot v1)
python3 -m metro --db m.db import-stations stations.json
python3 -m metro --db m.db import-timetable timetable.json --name v1

# gate events (whole-batch validation, dedup, anomaly preservation)
python3 -m metro --db m.db import-gates gates.json --name day1

# pair + explain + materialise bounds  -> batch_id
python3 -m metro --db m.db analyze --snapshot 1 --import-id 1 --name run-v1

# timetable queries that need no gate data
python3 -m metro --db m.db snapshots
python3 -m metro --db m.db station-times --snapshot 1 --station S05
python3 -m metro --db m.db train-stops --snapshot 1 --train A01

# inspect / explain
python3 -m metro --db m.db batches
python3 -m metro --db m.db summary   --batch 1          # counts + anomalies
python3 -m metro --db m.db trips     --batch 1 --status UNEXPLAINABLE
python3 -m metro --db m.db explain   --batch 1 --trip TRIP-E…-E…

# interval bounds + witness replay
python3 -m metro --db m.db interval --batch 1 --train A01            # all intervals
python3 -m metro --db m.db interval --batch 1 --train A01 --from-seq 1
python3 -m metro --db m.db witness  --batch 1 --train A01 --from-seq 1 --bound MAX
python3 -m metro --db m.db audit    --batch 1                        # replay every bound

# correction -> new snapshot -> re-analyze
python3 -m metro --db m.db correct --snapshot 1 --train A07 correction.json --name v2
python3 -m metro --db m.db analyze --snapshot 2 --import-id 1 --name run-v2
```

---

## 4. Synthetic inputs and verification

Generate from an empty project:

```bash
python3 -m metro.synth --out demo_data/full --small-out demo_data/small
```

- `full`: **20 stations, 100 trains, 30 stops each (3000 stops), 1000 trips**,
  plus showcase/anomaly cards: direct, transfer, loop repeated-station,
  missed-last-train, duplicate swipes, stale entry, orphan exit, missing exit.
- `small`: tiny network for exhaustive checking of every plan/bound/witness.

Independent cross-check (re-implements pairing, plan feasibility, bounds and
witness replay without importing the production analysis code):

```bash
# ordinary batch
python3 tools/verify.py check --input demo_data/full --db m.db --batch 1

# corrected batch, verified against the corrected timetable
python3 tools/verify.py correction --input demo_data/full --db m.db \
    --base-batch 1 --new-batch 2 --train A07 --changes demo_data/full/correction.json
```

Both exit non-zero on any mismatch. The same tool works on `demo_data/small`
for exhaustive confirmation of the full feasible-set enumeration.

---

## 5. One-command demo

```bash
./demo/run_demo.sh                      # writes /tmp/metro-demo
SKIP_FULL_VERIFY=1 ./demo/run_demo.sh   # skip the 1000-trip brute force
```

---

## 6. Typical timings (reference machine)

| step | data | time |
|------|------|------|
| generate full | 3000 stops / 1006 trips | < 0.1 s |
| analyze full | ~109k candidate plans | ~ 5 s |
| audit full (replay all 5800 bounds) | | ~ 1 s |
| independent verify full | | ~ 2 s |

Plan counts are data-dependent: tight gate windows keep the feasible set
small; wide windows legitimately admit many explanations, which is exactly
what the min/max bounds are designed to expose instead of hiding.
