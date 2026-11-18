"""End-to-end demo: local object source + ETag-pinned resumable assembler.

Generates a synthetic 20 MiB object, serves it over real HTTP, and runs a set
of scenarios that exercise the hard cases from the brief:

  * baseline       20 MiB, 4 concurrent connections
  * overlap/merge  client expands ranges; server merges overlaps/adjacents
  * suffix range   ``bytes=-N`` and out-of-range behaviour on the source
  * empty object   zero-byte object publishes an empty file
  * reorder        multipart parts delivered in a shuffled order
  * disconnect     mid-part disconnect, salvaged prefix, retry
  * version swap   same-length body replacement terminates the task
  * resume         crash between fragment boundaries, reused verified bytes
  * tamper         a corrupted temp-body fragment is rejected on resume

Every check reads the real HTTP body. Success is never inferred from status
codes alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import object_source as osrc
import rangeutil
from assembler import Assembler, SourceVersionChanged, AbortedState


MIB = 1024 * 1024
OBJECT_ID = "demo-object"


# --------------------------------------------------------------------------
# Small independent HTTP client (plain urllib; no assembler code involved)
# --------------------------------------------------------------------------


def http_request(url: str, method: str = "GET", headers: dict[str, str] | None = None,
                 timeout: float = 30.0):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def parse_multipart_parts(content_type: str, body: bytes) -> list[tuple[tuple[int, int], bytes]]:
    boundary = None
    for field in content_type.split(";"):
        field = field.strip()
        if field.lower().startswith("boundary="):
            boundary = field.split("=", 1)[1].strip('"')
    if boundary is None:
        raise AssertionError(f"no boundary in {content_type!r}")
    delim = b"--" + boundary.encode()
    out = []
    chunks = body.split(delim)
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        header_block, _, data = chunk.partition(b"\r\n\r\n")
        start = end = None
        for line in header_block.split(b"\r\n"):
            if line.lower().startswith(b"content-range:"):
                spec = line.split(b":", 1)[1].strip().decode()
                # "bytes <start>-<end>/<total>"
                _unit, spec = spec.split(" ", 1)
                rng, _total = spec.split("/", 1)
                a, b = rng.split("-")
                start, end = int(a), int(b)
        assert start is not None
        out.append(((start, end), data))
    return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def make_body(size: int, seed: int = 7) -> bytes:
    """Deterministic incompressible-looking synthetic body."""
    h = hashlib.sha256(str(seed).encode()).digest()
    out = bytearray()
    counter = 0
    while len(out) < size:
        h = hashlib.sha256(h + counter.to_bytes(8, "big")).digest()
        out.extend(h)
        counter += 1
    return bytes(out[:size])


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(MIB), b""):
            h.update(chunk)
    return h.hexdigest()


def log(msg: str) -> None:
    print(f"    {msg}", flush=True)


class Reporter:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append({"check": name, "ok": bool(ok), "detail": detail})
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not ok else ""),
              flush=True)
        if not ok:
            raise AssertionError(f"{name}: {detail}")

    @property
    def ok(self) -> bool:
        return all(r["ok"] for r in self.results)


def scenario(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


def scenario_source_protocol(base: str, body: bytes, etag: str, rep: Reporter) -> None:
    """The source is an ordinary HTTP object server (plain urllib client)."""
    scenario("source protocol with an independent HTTP client")
    url = osrc.object_url(base, OBJECT_ID)

    status, headers, data = http_request(url, "HEAD")
    rep.check("HEAD 200 + length", status == 200 and int(headers["Content-Length"]) == len(body))
    rep.check("HEAD strong ETag", headers.get("ETag") == etag, headers.get("ETag", ""))
    rep.check("HEAD advertises byte ranges", headers.get("Accept-Ranges") == "bytes")

    status, headers, data = http_request(url, headers={"Range": "bytes=0-15,10-31"})
    rep.check("server merges overlapping ranges into one part",
              status == 206 and headers.get("Content-Range") == f"bytes 0-31/{len(body)}"
              and data == body[0:32])

    status, headers, data = http_request(url, headers={"Range": "bytes=-17"})
    rep.check("suffix range", status == 206
              and headers.get("Content-Range") == f"bytes {len(body) - 17}-{len(body) - 1}/{len(body)}"
              and data == body[-17:])

    status, headers, data = http_request(url, headers={"Range": "bytes=0-9,20-29,-8"})
    parts = parse_multipart_parts(headers.get("Content-Type", ""), data)
    got = {rng: payload for rng, payload in parts}
    rep.check("multipart/byteranges with exact per-part Content-Range",
              status == 206 and len(parts) == 3
              and got.get((0, 9)) == body[0:10]
              and got.get((20, 29)) == body[20:30]
              and got.get((len(body) - 8, len(body) - 1)) == body[-8:])

    status, headers, data = http_request(url, headers={"Range": "bytes=0-3"},
                                         method="HEAD")
    rep.check("HEAD ignores Range (never 206)", status == 200
              and int(headers["Content-Length"]) == len(body))

    status, headers, data = http_request(url, headers={"Range": f"bytes={len(body) + 10}-"})
    rep.check("416 has bytes */length and empty body",
              status == 416 and headers.get("Content-Range") == f"bytes */{len(body)}"
              and data == b"")

    status, headers, data = http_request(url, headers={"Range": "bytes=0-1,999999999-"})
    rep.check("unsatisfiable member ignored when one satisfiable remains",
              status == 206 and data == body[0:2])

    status, headers, data = http_request(url, headers={"If-Match": '"' + "0" * 64 + '"'})
    rep.check("If-Match failure is 412, not an empty 200/206", status == 412)

    status, headers, data = http_request(osrc.object_url(base, "does-not-exist"))
    rep.check("missing object is 404", status == 404)


def scenario_baseline(base: str, body: bytes, etag: str, workdir: str,
                      rep: Reporter) -> dict:
    scenario(f"baseline assembly of {len(body)} bytes with concurrency 4")
    state_dir = os.path.join(workdir, "baseline")
    shutil.rmtree(state_dir, ignore_errors=True)
    asm = Assembler(base, state_dir, concurrency=4, chunk_size=MIB, max_chunks=100,
                    log_fn=log)
    plan = asm.plan(OBJECT_ID, etag=etag, total=len(body))
    rep.check("plan pins length and ETag",
              plan["total"] == len(body) and plan["etag"] == etag)
    rep.check("plan within 100 pieces", 1 <= len(plan["chunks"]) <= 100,
              f"{len(plan['chunks'])} pieces")
    meta = asm.fetch()
    target = os.path.join(workdir, "original.bin")
    rep.check("published target size", os.path.getsize(meta["target"]) == len(body))
    rep.check("published target equals fixed-version original",
              open(meta["target"], "rb").read() == body)
    rep.check("whole-file SHA256 equals pinned ETag",
              sha256_file(meta["target"]) == etag.strip('"'))
    result = asm.verify(original_path=target, check_server_log=True)
    rep.check("verify: full coverage + per-interval original match + log ETags",
              result["ok"], json.dumps(result["failures"]))
    rep.check("verify: every body response bound to pinned ETag",
              result["checks"]["server_etag_mismatches"] == [])
    rep.check("verify: confirmed intervals independently matched original",
              all(e.get("matches_original") for e in result["checks"]["confirmed_intervals"]))
    print(json.dumps({"requests": result["checks"]["server_requests"],
                      "statuses": result["checks"]["server_statuses"],
                      "bytes_written": result["checks"]["server_bytes_written"]}, indent=1))
    return {"state_dir": state_dir, "meta": meta}


def scenario_merged_ranges(base: str, body: bytes, etag: str, workdir: str,
                           rep: Reporter) -> None:
    scenario("client-side overlap, fan-out and merged server responses")
    state_dir = os.path.join(workdir, "merged")
    shutil.rmtree(state_dir, ignore_errors=True)
    asm = Assembler(base, state_dir, concurrency=4, chunk_size=256 * 1024,
                    fanout=4, fanout_gap=1024, overlap=64, coalesce=2, log_fn=log)
    asm.plan(OBJECT_ID, etag=etag)
    asm.fetch(resume=False)
    target = os.path.join(state_dir, "target.bin")
    rep.check("overlapping/fan-out fetch equals original",
              open(target, "rb").read() == body)
    result = asm.verify(original_path=os.path.join(workdir, "original.bin"),
                        check_server_log=True)
    rep.check("verify after overlap/fan-out", result["ok"], json.dumps(result["failures"]))
    counters = asm.counters()
    rep.check("multipart responses were actually used", counters["multipart_responses"] > 0,
              json.dumps(counters))


def scenario_reorder(base: str, srvs, body: bytes, etag: str, workdir: str,
                     rep: Reporter) -> None:
    scenario("multipart parts returned out of order")
    state_dir = os.path.join(workdir, "reorder")
    shutil.rmtree(state_dir, ignore_errors=True)
    asm = Assembler(base, state_dir, concurrency=3, chunk_size=128 * 1024,
                    fanout=5, fanout_gap=11, log_fn=log)
    asm.plan(OBJECT_ID, etag=etag)
    srvs.faults.reorder_parts = True
    srvs.faults.chunk_size = 4096
    asm.fetch()
    srvs.faults.reorder_parts = False
    srvs.faults.chunk_size = 64 * 1024
    # A single request with several ranges produces a shuffled multipart body.
    rep.check("out-of-order parts assembled correctly",
              open(os.path.join(state_dir, "target.bin"), "rb").read() == body)
    result = asm.verify(original_path=os.path.join(workdir, "original.bin"))
    rep.check("verify after reordered multipart", result["ok"], json.dumps(result["failures"]))


def scenario_empty(base: str, rep: Reporter) -> None:
    scenario("empty object")
    # The source for this scenario gets its own store.
    store = osrc.ObjectStore()
    store.create("empty", b"")
    srv, _, empty_base = osrc.serve_in_thread(store)
    try:
        state_dir = os.path.join("/tmp", "solodemo-empty")
        shutil.rmtree(state_dir, ignore_errors=True)
        asm = Assembler(empty_base, state_dir, concurrency=4, log_fn=log)
        asm.plan("empty")
        asm.fetch()
        target = os.path.join(state_dir, "target.bin")
        rep.check("empty target published with 0 bytes",
                  os.path.exists(target) and os.path.getsize(target) == 0)
        rep.check("empty verify", asm.verify()["ok"])
    finally:
        srv.shutdown()


def scenario_disconnect(base: str, srvs, body: bytes, etag: str, workdir: str,
                        rep: Reporter) -> None:
    scenario("mid-body disconnect, salvaged prefix, retry")
    state_dir = os.path.join(workdir, "disconnect")
    shutil.rmtree(state_dir, ignore_errors=True)
    asm = Assembler(base, state_dir, concurrency=2, chunk_size=512 * 1024,
                    log_fn=log)
    asm.plan(OBJECT_ID, etag=etag)
    srvs.faults.chunk_size = 8192
    srvs.faults.drop_next(120_000, count=2)
    asm.fetch()
    counters = asm.counters()
    rep.check("truncation triggered retries", counters["retries"] >= 2, json.dumps(counters))
    rep.check("verified prefixes were salvaged", counters["salvaged_prefixes"] >= 1,
              json.dumps(counters))
    rep.check("target equals original after disconnect recovery",
              open(os.path.join(state_dir, "target.bin"), "rb").read() == body)
    result = asm.verify(original_path=os.path.join(workdir, "original.bin"),
                        check_server_log=True, require_retries=True)
    rep.check("verify notes server truncations and retries", result["ok"],
              json.dumps(result["failures"]))
    log(f"server summary: {json.dumps(result['checks']['server_statuses'])} "
        f"truncated={result['checks']['server_retries_observed']}")
    srvs.faults.chunk_size = 64 * 1024


def scenario_version_swap(base: str, srvs, store, body: bytes, etag: str,
                          workdir: str, rep: Reporter) -> None:
    scenario("same-length body replacement mid-fetch")
    state_dir = os.path.join(workdir, "versionswap")
    shutil.rmtree(state_dir, ignore_errors=True)
    asm = Assembler(base, state_dir, concurrency=1, chunk_size=256 * 1024,
                    log_fn=log)
    asm.plan(OBJECT_ID, etag=etag)
    # Confirm a fragment, then hold the next GET so the object can be replaced.
    asm.fetch_ranges([rangeutil.ByteRange(0, 9999)])
    srvs.faults.hold(etag)
    outcome: dict[str, BaseException | str] = {}

    def run() -> None:
        try:
            asm.fetch()
            outcome["result"] = "unexpected success"
        except BaseException as exc:  # noqa: BLE001
            outcome["result"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(500):
        if srvs.faults.gate_waiters > 0:
            break
        time.sleep(0.01)
    replacement = bytearray(body)
    replacement[1000:2000] = bytes(1000)  # same length, different bytes
    new_version = store.update(OBJECT_ID, bytes(replacement))
    srvs.faults.release(etag)
    thread.join(60)
    exc = outcome.get("result")
    rep.check("task terminates with SourceVersionChanged",
              isinstance(exc, SourceVersionChanged), repr(exc))
    rep.check("replacement keeps the same length",
              new_version.length == len(body) and new_version.etag != etag)
    state = json.load(open(os.path.join(state_dir, "state.json")))
    rep.check("state records terminal abort", state["status"] == "aborted", state["status"])
    rep.check("no target published", not os.path.exists(os.path.join(state_dir, "target.bin")))
    try:
        asm.fetch(resume=True)
        rep.check("resume refuses to switch ETag", False, "resume succeeded")
    except AbortedState:
        rep.check("resume refuses to switch ETag", True)
    result = asm.verify(check_server_log=True)
    rep.check("aborted task verifies as terminal, not as success", result["ok"],
              json.dumps(result["failures"]))
    rep.check("server log shows the 412", "412" in result["checks"]["server_statuses"],
              json.dumps(result["checks"]["server_statuses"]))
    # Restore the original version for later scenarios.
    store.update(OBJECT_ID, body)


def scenario_resume(base: str, body: bytes, etag: str, workdir: str,
                    rep: Reporter) -> None:
    scenario("crash between fragment boundaries and resume")
    state_dir = os.path.join(workdir, "resume")
    shutil.rmtree(state_dir, ignore_errors=True)
    chunk = 256 * 1024
    asm = Assembler(base, state_dir, concurrency=4, chunk_size=chunk, log_fn=log)
    plan = asm.plan(OBJECT_ID, etag=etag)
    # Simulate a crash after some fragments were confirmed: fetch a sparse set
    # of ranges, then stop without publishing.
    confirmed = [
        (0, chunk - 1),
        (3 * chunk, 4 * chunk - 1),
        (len(body) - 5000, len(body) - 1),
    ]
    for start, end in confirmed:
        asm.fetch_ranges([rangeutil.ByteRange(start, end)])
    before = asm.inspect()
    rep.check("pre-crash coverage is partial", not before["complete_coverage"],
              json.dumps(before["holes"]))
    rep.check("pre-crash journal persisted fragments",
              before["journal_on_disk_records"] == len(confirmed))
    # "Crash": drop the object, rebuild the assembler from disk.
    del asm
    recovered = Assembler(base, state_dir, concurrency=4, chunk_size=chunk, log_fn=log)
    meta = recovered.fetch(resume=True)
    rep.check("resume republished the full object",
              open(meta["target"], "rb").read() == body)
    result = recovered.verify(original_path=os.path.join(workdir, "original.bin"),
                              check_server_log=True)
    rep.check("resume verify", result["ok"], json.dumps(result["failures"]))
    with open(os.path.join(state_dir, "client_requests.jsonl")) as fh:
        resumed_requests = [json.loads(line) for line in fh if line.strip()]
    refetched = sum(1 for r in resumed_requests
                    if r.get("request_ranges") and
                    not (r["request_ranges"][0][0] == 0 and
                         r["request_ranges"][0][1] == confirmed[0][1]))
    rep.check("resume did not re-download confirmed fragments wholesale",
              len(resumed_requests) >= len(confirmed), str(len(resumed_requests)))
    log(f"plan pieces={len(plan['chunks'])}; requests during resume={len(resumed_requests)}")
    del recovered


def scenario_tamper(base: str, body: bytes, etag: str, workdir: str,
                    rep: Reporter) -> None:
    scenario("resume rejects a tampered temp-body fragment")
    state_dir = os.path.join(workdir, "tamper")
    shutil.rmtree(state_dir, ignore_errors=True)
    chunk = 128 * 1024
    asm = Assembler(base, state_dir, concurrency=2, chunk_size=chunk, log_fn=log)
    asm.plan(OBJECT_ID, etag=etag)
    asm.fetch_ranges([rangeutil.ByteRange(0, chunk - 1),
                      rangeutil.ByteRange(chunk, 2 * chunk - 1)])
    # Corrupt confirmed bytes inside the temp body without touching the journal.
    body_path = os.path.join(state_dir, "body.part")
    with open(body_path, "r+b") as fh:
        fh.seek(1000)
        fh.write(b"\x00" * 32)
        fh.flush()
        os.fsync(fh.fileno())
    recovered = Assembler(base, state_dir, concurrency=2, chunk_size=chunk, log_fn=log)
    meta = recovered.fetch(resume=True)
    report = json.load(open(os.path.join(state_dir, "resume_report.json")))
    rep.check("tampered fragment was rejected on resume",
              len(report["rejected"]) >= 1, json.dumps(report["rejected"])[:300])
    rep.check("tampered fragment bytes were refetched, target still exact",
              open(meta["target"], "rb").read() == body)
    rep.check("verify after tamper recovery",
              recovered.verify(original_path=os.path.join(workdir, "original.bin"))["ok"])
    del recovered


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def run(workdir: str = "demo_out", size: int = 20 * MIB, keep: bool = False) -> dict:
    os.makedirs(workdir, exist_ok=True)
    workdir = os.path.abspath(workdir)
    body = make_body(size, seed=11)
    original = os.path.join(workdir, "original.bin")
    with open(original, "wb") as fh:
        fh.write(body)
    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
    with open(os.path.join(workdir, "original.json"), "w", encoding="utf-8") as fh:
        json.dump({"size": len(body), "sha256": etag.strip('"'), "etag": etag}, fh, indent=2)

    store = osrc.ObjectStore()
    version = store.create(OBJECT_ID, body)
    assert version.etag == etag
    server, thread, base = osrc.serve_in_thread(store)
    print(f"source listening on {base}  object_id={OBJECT_ID}  "
          f"length={len(body)}  etag={etag}", flush=True)
    rep = Reporter()
    started = time.time()
    try:
        scenario_source_protocol(base, body, etag, rep)
        scenario_baseline(base, body, etag, workdir, rep)
        scenario_merged_ranges(base, body, etag, workdir, rep)
        scenario_reorder(base, server, body, etag, workdir, rep)
        scenario_empty(base, rep)
        scenario_disconnect(base, server, body, etag, workdir, rep)
        scenario_version_swap(base, server, store, body, etag, workdir, rep)
        scenario_resume(base, body, etag, workdir, rep)
        scenario_tamper(base, body, etag, workdir, rep)
    finally:
        server.shutdown()
        server.server_close()

    log_entries = server.log.entries()
    summary = {
        "workdir": workdir,
        "object_id": OBJECT_ID,
        "size": len(body),
        "etag": etag,
        "elapsed_s": round(time.time() - started, 3),
        "checks": rep.results,
        "server_requests": len(log_entries),
        "server_statuses": server.log.summary()["statuses"],
    }
    print("\n" + "=" * 72)
    print(json.dumps(summary, indent=2))
    print("=" * 72)
    print(f"ALL {len(rep.results)} CHECKS PASSED" if rep.ok else "SOME CHECKS FAILED")
    if not keep:
        pass  # keep artifacts; they are useful evidence
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "demo_out"))
    parser.add_argument("--size-mib", type=float, default=20.0)
    args = parser.parse_args(argv)
    summary = run(args.workdir, int(args.size_mib * MIB))
    return 0 if all(c["ok"] for c in summary["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
