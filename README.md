# Offline range-fetch assembler with a local object source

A Python 3.11 implementation of two cooperating components:

1. **`src/object_source.py`** — a real HTTP object source with a stable
   `object_id`, an immutable *current* version, a strong ETag that is exactly
   `"<sha256 of that version's body>"`, `HEAD`/`GET`, single and multi byte
   ranges, suffix ranges and `If-Match`. It supports fault injection and stores
   a queryable request log.
2. **`src/assembler.py`** — a concurrent, ETag-pinned, resumable file
   assembler. It pins `object_id + total length + strong ETag` first, then
   fetches the body over **real HTTP** with conditionally-guarded range
   requests, verifies every fragment, tracks coverage, and publishes a target
   file **only** when coverage is complete and the whole-file SHA256 equals the
   pinned ETag.

The two share small, side-effect-free helpers in **`src/rangeutil.py`**
(range parsing/normalisation, interval algebra, ETag and Content-Range
parsing). No third-party packages are required — Python 3.11 standard library
only.

Run everything:

```bash
python3.11 src/demo.py --workdir demo_out --size-mib 20   # full 20 MiB demo
python3.11 -m unittest discover -s tests -v               # 29 tests
# or: python3.11 -m pytest tests -q
```

---

## 1. Source protocol

| Request | Behaviour |
|---|---|
| `HEAD /objects/{id}` | `200` with `Content-Length`, `ETag`, `Accept-Ranges: bytes`. Never `206`; a `Range` header is ignored. |
| `GET /objects/{id}` (no `Range`) | `200`, full body. |
| `GET` + one satisfiable range | `206` + exact `Content-Range: bytes s-e/total`. |
| `GET` + several ranges | `206 multipart/byteranges`, one part per served range, each with its own `Content-Range`. Part order is not guaranteed. |
| Overlapping / adjacent ranges | **Merged** server-side. A client must never assume one requested range = one response part. |
| `bytes=-N` | Last `N` bytes (clamped to the body). |
| `bytes=s-` | From `s` to the end. |
| `If-Match: "<digest>"` | Strong comparison. Mismatch → `412`. `W/"..."` never matches. |
| Unknown object | `404`. |
| Malformed `Range` | `400`. |
| No satisfiable range | `416` + `Content-Range: bytes */total`, empty body. |

Additional guarantees:

* **Per-response version snapshot.** The handler resolves the current version
  *before* writing headers and streams that immutable `bytes` object. An
  `update` during the response cannot alter it.
* **Different requests may see different versions** unless they pin the same
  strong ETag. Old versions are never resolvable again — updating an object
  makes the previous version unreadable.
* **Distinct failure semantics.** `404`, `412`, `416`, `400` and a truncated
  transfer are all distinguishable: status codes, `Content-Range`, and the
  request-log `truncated` flag. None is treated as "empty body".

### Fault injection (`Faults`)

| Facility | Purpose |
|---|---|
| `drop_next(after_bytes, count=, object_id=, etag=)` | Write only `after_bytes` then close the socket mid-body (real truncation). |
| `reorder_parts = True` | Deliver `multipart/byteranges` parts in a shuffled order. |
| `chunk_size` / `chunk_delay` | Throttle the body to keep responses in flight (e.g. for version swaps). |
| `hold(etag)` / `release(etag)` | Gate a version's responses so the object can be replaced while a request is held. |
| `store.update(id, body)` | Install a new current version (the old one becomes unreadable). |

Over HTTP the same controls are available under `/__control/`:

```
GET    /__control/log[?object_id=..&since_seq=..]   request log + summary
GET    /__control/summary[?object_id=..]            status histogram
GET    /__control/objects                           current versions
DELETE /__control/log                               clear the log
POST   /__control/update  {"object_id","body_hex"|"body_text"}
POST   /__control/faults  {"action":"drop_next"|"reorder_parts"|"chunk"|"hold"|"release"|"reset", ...}
```

Each log entry records method, path, object id, requested `If-Match` and
`Range`, mode (`full`/`single`/`multipart`), status, response ETag, version,
served ranges, declared length, bytes actually written, truncation flag,
error and duration.

Run the source standalone for use by any ordinary HTTP client:

```bash
python3.11 src/object_source.py --port 8000 --object-id demo --body-file original.bin
curl -sI http://127.0.0.1:8000/objects/demo
curl -s -H 'Range: bytes=0-9,20-29' http://127.0.0.1:8000/objects/demo | xxd
curl -s -H 'Range: bytes=-16' http://127.0.0.1:8000/objects/demo
```

---

## 2. Assembler design

### 2.1 Pinning

`plan` issues a `HEAD`, then records `object_id`, `total`, strong `ETag`, and
`sha256` (extracted from the ETag) in `state.json`. `--etag`/`--total` let a
caller demand a specific version; a mismatch aborts the plan. Chunk size is
chosen so the object needs at most `--max-chunks` (default 100) plan pieces:
`chunk_size = max(requested, ceil(total / max_chunks))`. Every subsequent `GET`
sends `If-Match: "<pinned etag>"`, and `fetch`/`resume` do a conditional
preflight `HEAD` before any body byte.

### 2.2 Per-response validation (not just "did I receive bytes?")

For each response the assembler checks:

* ETag is present, strong, and **equals the pinned ETag** (otherwise → abort).
* Single range: `Content-Range` parseable, its `total` equals the pinned total,
  the range lies inside what was requested, the body is not longer than the
  range, and a complete body equals the range length.
* Multipart: every part has a `Content-Range` inside the requested union, part
  lengths match their `Content-Range`, parts may arrive in any order, and
  overlapping/merged parts are reconciled byte-for-byte. A trailing
  unterminated part is salvaged only as a **verified prefix** (a partial
  delimiter prefix is trimmed).
* A `200` fallback (server ignoring `Range`) is accepted only if its length
  equals the total *and* its SHA256 equals the pinned ETag.
* Received bytes must **cover** every requested hole; a response that confirms
  nothing is retried with a smaller span and eventually fails loudly.

Fragment acceptance reconciles any byte already known from another overlapping
fragment: a differing overlap is a fatal protocol violation, never a silent
overwrite.

### 2.3 Fragments and coverage

Confirmed bytes live in a sparse temporary body (`body.part`) plus an
append-only journal (`confirmed.jsonl`) of
`{offset, length, sha256, etag, source, ts}` records (fsync'd before a fragment
counts as confirmed). Coverage is the union of confirmed intervals. A truncated
response only confirms the bytes actually received and verified — never the
whole requested range. A failed response marks nothing complete.

### 2.4 Splitting, retries, and their cost

Each hole is covered by work regions built from the plan chunk size. Within a
region a worker:

* splits the still-missing intervals into `--fanout` disjoint request ranges,
  leaving gaps that are fetched in a later round — **gaps are never zero-filled**;
* optionally expands each range by `--overlap` bytes on both sides, forcing
  server-side merging and overlap reconciliation;
* caps a range's span and halves that cap after a truncation, so retries shrink
  toward the failure point;
* retries transport errors and truncated responses with backoff, re-requesting
  only the remaining holes.

The granularity/strategy trades off **duplicate transmission**, **temp space**,
and **coverage-proof granularity**:

| Strategy / option | Effect |
|---|---|
| `chunks` + `--coalesce N` | `N` adjacent plan pieces per request; fewer requests, coarser retry unit. |
| `coalesced` | Grouping/merging mode for neighbouring ranges. |
| `--fanout N` + `--fanout-gap G` | Multiple disjoint ranges per request; exercises `multipart/byteranges` and split/retry. |
| `--overlap N` | Deliberate duplicate bytes to force merging and overlap checks. |
| `--chunk-size` / `--max-chunks` | Smaller chunks = finer retry/resume granularity but more requests and journal records; larger chunks = less duplication and a smaller journal, but a bigger unit to redo after a disconnect. |

### 2.5 Version change → terminal abort

If a `GET` returns `412`, a different ETag, or a different total, or the
preflight `HEAD` disagrees with the pinned version, the task is marked
`aborted` with a reason, **no target is published**, and `fetch`/`resume` raise
`SourceVersionChanged`. The assembler never silently switches to the new ETag to
fetch the remainder. A `404` after the object vanished is also terminal;
`416`/`400` indicate a protocol error and are fatal rather than "empty".

### 2.6 Persistence and resume

```
state.json             pinned object_id/total/etag/sha256/status/coverage/counters
body.part              sparse temp body (confirmed bytes only)
confirmed.jsonl        append-only verified-fragment journal
client_requests.jsonl  client-side per-request log (ranges, status, retries, confirmations)
resume_report.json     fragments rejected by the last resume validation
target.bin             published target (only after full verification)
metadata.json          publish metadata (coverage, sha256, counters)
```

On `resume` (or `fetch` after a crash) the journal is replayed and **every**
record's bytes in `body.part` are re-hashed and checked against its recorded
hash, ETag and bounds. Invalid records are dropped, reported, and refetched;
confirmed fragments are not downloaded again; gaps are fetched normally.

Publishing requires (a) coverage exactly `[0, total)` (or total `0`) and
(b) whole-file SHA256 equal to the pinned ETag digest. Only then is `body.part`
atomically renamed to `target.bin`.

### 2.7 `inspect` and `verify`

`inspect` reports pinned state, coverage, holes, journal validity, target
existence/size/hash, and optionally the source's current ETag.

`verify` re-proves success without trusting status codes:

* target size equals the pinned total and target SHA256 equals the pinned ETag;
* every confirmed interval is re-hashed against the **fixed-version original**
  (with `--original`) and against the target;
* with `--check-server-log`, it pulls the source's request log and asserts every
  `200`/`206` body response carried the pinned ETag, that real body responses
  occurred, and that retries/truncations are visible;
* an `aborted` task verifies as a **terminal** outcome (no target, reason
  recorded, `412` still visible in the server log).

### 2.8 CLI

```bash
python3.11 src/assembler.py plan   --base-url http://127.0.0.1:8000 \
    --object-id demo --state-dir state --etag '"<sha256>"' --chunk-size 2097152
python3.11 src/assembler.py fetch  --base-url http://127.0.0.1:8000 --state-dir state
python3.11 src/assembler.py resume --base-url http://127.0.0.1:8000 --state-dir state
python3.11 src/assembler.py inspect --base-url http://127.0.0.1:8000 --state-dir state --check-source
python3.11 src/assembler.py verify --base-url http://127.0.0.1:8000 --state-dir state \
    --original original.bin --check-server-log
```

---

## 3. Scenarios covered by `demo.py`

| Scenario | What it proves |
|---|---|
| source protocol | independent `urllib` client: HEAD/GET, merge, suffix, multipart, 416/412/404 |
| baseline | 20 MiB, 4 connections, ≤100 plan pieces, byte-exact target, ETag/SHA256 |
| overlap/fan-out | overlapping and split ranges, server merging, multipart parts |
| reorder | multipart parts delivered out of order, still assembled correctly |
| empty object | zero-byte object publishes an empty target |
| disconnect | mid-body truncation, verified prefix salvage, retry, final exactness |
| version swap | same-length replacement mid-fetch → terminal abort, no target, 412 in server log |
| resume | crash between fragments, re-hash of temp body, no refetch of confirmed bytes |
| tamper | a corrupted temp-body fragment is rejected and refetched on resume |

The demo writes `original.bin`, `original.json` (size / SHA256 / ETag) and the
per-scenario state directories under `--workdir`, and prints a JSON summary.
All checks verify actual bytes, fragment hashes, coverage and the request log;
HTTP 200/206 alone is never considered success.

---

## 4. Files

```
src/rangeutil.py       shared range / ETag / interval helpers
src/object_source.py   HTTP object source (store, faults, request log, control API, CLI)
src/assembler.py       concurrent ETag-pinned resumable assembler + CLI
src/demo.py            20 MiB end-to-end demo with all scenarios
tests/test_range_assembly.py  29 unittest cases (real HTTP)
```
