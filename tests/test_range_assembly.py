"""Tests for a local byte-range source and an ETag-pinned file assembler.

Run with either of:

    python -m unittest discover -s tests -v
    python -m pytest tests -q        # if pytest is installed

Every scenario drives real HTTP over a loopback socket. Checks assert on actual
received bytes, per-fragment hashes, coverage and request logs; HTTP 200/206
alone is never treated as success.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import object_source as osrc
import rangeutil
from rangeutil import ByteRange, parse_range_header
from assembler import (
    AbortedState,
    Assembler,
    ProtocolViolation,
    SourceVersionChanged,
    parse_multipart,
)


MIB = 1024 * 1024


def make_body(size: int, seed: int = 3) -> bytes:
    out = bytearray()
    h = hashlib.sha256(str(seed).encode()).digest()
    i = 0
    while len(out) < size:
        h = hashlib.sha256(h + i.to_bytes(8, "big")).digest()
        out += h
        i += 1
    return bytes(out[:size])


def http(url: str, method: str = "GET", headers: dict | None = None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for c in iter(lambda: fh.read(MIB), b""):
            h.update(c)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Pure helper tests
# --------------------------------------------------------------------------


class RangeUtilTests(unittest.TestCase):
    def test_single_and_clamped(self):
        self.assertEqual(parse_range_header("bytes=0-9", 100), [ByteRange(0, 9)])
        self.assertEqual(parse_range_header("bytes=90-999", 100), [ByteRange(90, 99)])
        self.assertEqual(parse_range_header("bytes=5-", 100), [ByteRange(5, 99)])
        self.assertEqual(parse_range_header("bytes=-10", 100), [ByteRange(90, 99)])
        self.assertEqual(parse_range_header("bytes=-500", 100), [ByteRange(0, 99)])

    def test_unsatisfiable_member_ignored_but_all_unsat_raises(self):
        self.assertEqual(parse_range_header("bytes=0-1,500-", 100), [ByteRange(0, 1)])
        for spec in ("bytes=100-", "bytes=200-300", "bytes=50-40", "bytes=-0"):
            with self.assertRaises(rangeutil.RangeNotSatisfiable):
                parse_range_header(spec, 100)

    def test_malformed(self):
        for spec in ("bytes=abc", "items=0-1", "bytes=0-1,", "bytes=-", "bytes"):
            with self.assertRaises(rangeutil.RangeHeaderError):
                parse_range_header(spec, 100)

    def test_merge_and_subtract(self):
        merged = rangeutil.merge_ranges([ByteRange(20, 29), ByteRange(0, 9),
                                         ByteRange(5, 12), ByteRange(31, 40)])
        self.assertEqual(merged, [ByteRange(0, 12), ByteRange(20, 29),
                                  ByteRange(31, 40)])
        self.assertEqual(rangeutil.merge_overlaps([ByteRange(0, 9), ByteRange(10, 19)]),
                         [ByteRange(0, 9), ByteRange(10, 19)])
        self.assertEqual(rangeutil.subtract_intervals(100, [ByteRange(0, 99)],
                                                      [ByteRange(10, 19)]),
                         [ByteRange(0, 9), ByteRange(20, 99)])


class MultipartParserTests(unittest.TestCase):
    def _mp(self, parts, boundary="BOUND"):
        out = b""
        for rng, data in parts:
            out += (f"\r\n--{boundary}\r\n"
                    f"Content-Range: bytes {rng[0]}-{rng[1]}/999\r\n\r\n"
                    .encode()) + data
        out += f"\r\n--{boundary}--\r\n".encode()
        return out

    def test_reordered_parts(self):
        parts = [((20, 29), b"abcdefghij"), ((0, 9), b"0123456789")]
        parsed, note = parse_multipart(self._mp(parts), "BOUND", False)
        self.assertEqual(note, "closed")
        self.assertEqual({p.range: p.data for p in parsed},
                         {ByteRange(20, 29): b"abcdefghij",
                          ByteRange(0, 9): b"0123456789"})

    def test_truncated_tail_is_partial(self):
        body = self._mp([((0, 9), b"0123456789"), ((20, 29), b"abcdefghij")])
        # Cut in the middle of the second part's data.
        cut = body.index(b"abcdefghij") + 4
        parsed, note = parse_multipart(body[:cut], "BOUND", True)
        self.assertEqual(len(parsed), 2)
        self.assertTrue(parsed[0].complete)
        self.assertEqual(parsed[0].data, b"0123456789")
        self.assertFalse(parsed[1].complete)
        self.assertEqual(parsed[1].data, b"abcd")
        self.assertEqual(parsed[1].range, ByteRange(20, 23))

    def test_partial_boundary_prefix_is_trimmed(self):
        body = self._mp([((0, 9), b"0123456789"), ((20, 29), b"abcdefghij")])
        # Include a partial "\r\n--BOU" prefix in the received tail.
        cut = body.index(b"abcdefghij") + 3
        tail = body[:cut] + b"\r\n--BOU"
        parsed, _ = parse_multipart(tail, "BOUND", True)
        self.assertEqual(parsed[1].data, b"abc")
        self.assertEqual(parsed[1].range, ByteRange(20, 22))


# --------------------------------------------------------------------------
# Live server tests
# --------------------------------------------------------------------------


class LiveServerCase(unittest.TestCase):
    object_id = "obj"
    body_seed = 5
    body_size = 512 * 1024 + 333

    @classmethod
    def setUpClass(cls):
        cls.body = make_body(cls.body_size, cls.body_seed)
        cls.store = osrc.ObjectStore()
        cls.version = cls.store.create(cls.object_id, cls.body)
        cls.server, cls.thread, cls.base = osrc.serve_in_thread(cls.store)
        cls.url = osrc.object_url(cls.base, cls.object_id)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.faults.reset()
        self.tmp = tempfile.mkdtemp(prefix="solo-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.server.faults.reset()

    def assembler(self, **kw):
        kw.setdefault("concurrency", 3)
        kw.setdefault("chunk_size", 128 * 1024)
        kw.setdefault("log_fn", lambda msg: None)
        return Assembler(self.base, os.path.join(self.tmp, "state"), **kw)

    def do_full_fetch(self, asm, expect_body=None):
        asm.plan(self.object_id)
        meta = asm.fetch()
        data = read_file(meta["target"])
        self.assertEqual(data, expect_body if expect_body is not None else self.body)
        return meta


class ProtocolTests(LiveServerCase):
    def test_head_and_get(self):
        s, h, _ = http(self.url, "HEAD")
        self.assertEqual(s, 200)
        self.assertEqual(h["ETag"], self.version.etag)
        self.assertEqual(int(h["Content-Length"]), len(self.body))
        s, h, b = http(self.url)
        self.assertEqual((s, b), (200, self.body))

    def test_single_range_and_suffix(self):
        s, h, b = http(self.url, headers={"Range": "bytes=10-19"})
        self.assertEqual((s, h["Content-Range"], b),
                         (206, f"bytes 10-19/{len(self.body)}", self.body[10:20]))
        s, h, b = http(self.url, headers={"Range": "bytes=-7"})
        self.assertEqual((s, h["Content-Range"], b),
                         (206, f"bytes {len(self.body) - 7}-{len(self.body) - 1}/"
                               f"{len(self.body)}", self.body[-7:]))

    def test_multipart_ranges_and_merge(self):
        s, h, b = http(self.url, headers={"Range": "bytes=0-4,10-14"})
        self.assertEqual(s, 206)
        self.assertTrue(h["Content-Type"].startswith("multipart/byteranges"))
        parts = parse_multipart(b, h["Content-Type"].split("boundary=")[1].strip('"'), False)[0]
        self.assertEqual({p.range: p.data for p in parts},
                         {ByteRange(0, 4): self.body[0:5],
                          ByteRange(10, 14): self.body[10:15]})
        # Overlapping/adjacent ranges are merged server-side.
        s, h, b = http(self.url, headers={"Range": "bytes=0-9,5-14,15-19"})
        self.assertEqual((s, h["Content-Range"], b),
                         (206, f"bytes 0-19/{len(self.body)}", self.body[0:20]))

    def test_statuses_are_distinct(self):
        s, h, b = http(self.url, headers={"Range": f"bytes={len(self.body) + 1}-"})
        self.assertEqual(s, 416)
        self.assertEqual(h["Content-Range"], f"bytes */{len(self.body)}")
        s, h, b = http(self.url, headers={"If-Match": '"' + "0" * 64 + '"'})
        self.assertEqual(s, 412)
        s, _, _ = http(osrc.object_url(self.base, "missing"))
        self.assertEqual(s, 404)
        s, _, _ = http(self.url, headers={"Range": "bytes=bad"})
        self.assertEqual(s, 400)

    def test_if_match_strong_only(self):
        s, _, b = http(self.url, headers={"If-Match": self.version.etag})
        self.assertEqual((s, b), (200, self.body))
        s, _, _ = http(self.url, headers={"If-Match": "W/" + self.version.etag})
        self.assertEqual(s, 412)

    def test_etag_is_sha256(self):
        self.assertEqual(self.version.etag,
                         '"' + hashlib.sha256(self.body).hexdigest() + '"')

    def test_response_is_a_version_snapshot(self):
        """An update mid-response must not alter that response's body."""
        body_a = make_body(64 * 1024, 21)
        body_b = make_body(64 * 1024, 22)
        store = osrc.ObjectStore()
        store.create("snap", body_a)
        server, _, base = osrc.serve_in_thread(store)
        try:
            server.faults.chunk_size = 1024
            server.faults.chunk_delay = 0.002
            result = {}

            def read():
                result["resp"] = http(osrc.object_url(base, "snap"))

            t = threading.Thread(target=read)
            t.start()
            time.sleep(0.05)
            store.update("snap", body_b)
            t.join(30)
            s, h, b = result["resp"]
            self.assertEqual((s, b), (200, body_a))
            self.assertEqual(h["ETag"], '"' + hashlib.sha256(body_a).hexdigest() + '"')
        finally:
            server.shutdown()
            server.server_close()


class AssemblerTests(LiveServerCase):
    def test_baseline_and_verify(self):
        asm = self.assembler()
        meta = self.do_full_fetch(asm)
        self.assertEqual(sha(meta["target"]), self.version.digest)
        result = asm.verify(check_server_log=True)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["checks"]["server_etag_mismatches"], [])
        self.assertGreater(result["checks"]["server_gets"], 0)

    def test_overlap_fanout_reorder(self):
        self.server.faults.reorder_parts = True
        asm = self.assembler(concurrency=4, chunk_size=96 * 1024, fanout=4,
                             fanout_gap=512, overlap=32)
        meta = self.do_full_fetch(asm)
        self.assertEqual(sha(meta["target"]), self.version.digest)
        self.assertGreater(asm.counters()["multipart_responses"], 0)
        self.assertTrue(asm.verify(original_path=None)["ok"])

    def test_coalesced_strategy(self):
        asm = self.assembler(concurrency=2, chunk_size=64 * 1024, coalesce=3,
                             strategy="coalesced")
        meta = self.do_full_fetch(asm)
        self.assertEqual(sha(meta["target"]), self.version.digest)
        logs = read_jsonl(os.path.join(asm.state_dir, "client_requests.jsonl"))
        self.assertTrue(any(len(r["request_ranges"]) > 1 for r in logs))

    def test_disconnect_salvage_and_retry(self):
        self.server.faults.chunk_size = 2048
        self.server.faults.drop_next(50_000, count=2)
        asm = self.assembler(concurrency=2, chunk_size=128 * 1024)
        meta = self.do_full_fetch(asm)
        c = asm.counters()
        self.assertGreaterEqual(c["retries"], 2)
        self.assertGreaterEqual(c["salvaged_prefixes"], 1)
        res = asm.verify(check_server_log=True, require_retries=True)
        self.assertTrue(res["ok"], res["failures"])

    def test_truncated_part_not_marked_complete(self):
        """A truncated single-range response only confirms its actual prefix."""
        self.server.faults.chunk_size = 1024
        self.server.faults.drop_next(3000, count=1)
        asm = self.assembler(concurrency=1, chunk_size=8192, retries=0)
        asm.plan(self.object_id)
        parts = asm.fetch_ranges([ByteRange(0, 7999)])
        self.assertEqual(len(parts), 1)
        self.assertFalse(parts[0]["complete"])
        insp = asm.inspect()
        covered = insp["covered"]
        self.assertGreater(covered, 0)
        self.assertLess(covered, 8000)
        self.assertFalse(insp["complete_coverage"])
        # Confirm the salvaged bytes are real body bytes.
        cov = insp["coverage"][0]
        with open(os.path.join(asm.state_dir, "body.part"), "rb") as fh:
            fh.seek(cov[0])
            got = fh.read(cov[1] - cov[0] + 1)
        self.assertEqual(got, self.body[cov[0]:cov[1] + 1])

    def test_empty_object(self):
        store = osrc.ObjectStore()
        store.create("empty", b"")
        server, _, base = osrc.serve_in_thread(store)
        try:
            asm = Assembler(base, os.path.join(self.tmp, "empty"),
                            log_fn=lambda m: None)
            asm.plan("empty")
            meta = asm.fetch()
            self.assertEqual(os.path.getsize(meta["target"]), 0)
            self.assertTrue(asm.verify()["ok"])
        finally:
            server.shutdown()
            server.server_close()

    def test_version_changed_same_length_terminates(self):
        store = osrc.ObjectStore()
        body = make_body(256 * 1024, 31)
        v1 = store.create("swap", body)
        server, _, base = osrc.serve_in_thread(store)
        try:
            state_dir = os.path.join(self.tmp, "swap")
            asm = Assembler(base, state_dir, concurrency=1, chunk_size=64 * 1024,
                            log_fn=lambda m: None)
            asm.plan("swap")
            asm.fetch_ranges([ByteRange(0, 999)])
            server.faults.hold(v1.etag)
            out = {}

            def run():
                try:
                    asm.fetch()
                    out["r"] = "ok"
                except Exception as e:
                    out["r"] = e

            t = threading.Thread(target=run)
            t.start()
            for _ in range(500):
                if server.faults.gate_waiters > 0:
                    break
                time.sleep(0.01)
            changed = bytearray(body)
            changed[100:200] = b"\xff" * 100
            v2 = store.update("swap", bytes(changed))
            self.assertEqual(v2.length, v1.length)
            server.faults.release(v1.etag)
            t.join(30)
            self.assertIsInstance(out["r"], SourceVersionChanged)
            st = read_json(os.path.join(state_dir, "state.json"))
            self.assertEqual(st["status"], "aborted")
            self.assertFalse(os.path.exists(os.path.join(state_dir, "target.bin")))
            with self.assertRaises(AbortedState):
                asm.fetch(resume=True)
            ver = asm.verify(check_server_log=True)
            self.assertTrue(ver["ok"], ver["failures"])
            self.assertIn("412", ver["checks"]["server_statuses"])
        finally:
            server.shutdown()
            server.server_close()

    def test_resume_reuses_confirmed_fragments(self):
        chunk = 128 * 1024
        asm = self.assembler(concurrency=2, chunk_size=chunk)
        asm.plan(self.object_id)
        for rng in (ByteRange(0, chunk - 1), ByteRange(2 * chunk, 3 * chunk - 1)):
            asm.fetch_ranges([rng])
        before = asm.inspect()
        self.assertEqual(before["journal_on_disk_records"], 2)
        del asm
        rec = self.assembler(concurrency=2, chunk_size=chunk)
        meta = rec.fetch(resume=True)
        self.assertEqual(read_file(meta["target"]), self.body)
        logs = [r for r in read_jsonl(os.path.join(rec.state_dir,
                                                   "client_requests.jsonl"))
                if not r.get("probe")]
        # The two already-confirmed fragments must not be requested again.
        for r in logs:
            for a, b in r["request_ranges"]:
                self.assertFalse(a == 0 and b == chunk - 1,
                                 f"re-fetched confirmed fragment: {r}")
                self.assertFalse(a == 2 * chunk and b == 3 * chunk - 1,
                                 f"re-fetched confirmed fragment: {r}")

    def test_resume_rejects_tampered_fragment(self):
        chunk = 64 * 1024
        asm = self.assembler(concurrency=2, chunk_size=chunk)
        asm.plan(self.object_id)
        asm.fetch_ranges([ByteRange(0, chunk - 1)])
        with open(os.path.join(asm.state_dir, "body.part"), "r+b") as fh:
            fh.seek(100)
            fh.write(b"\x00" * 16)
            fh.flush()
            os.fsync(fh.fileno())
        rec = self.assembler(concurrency=2, chunk_size=chunk)
        meta = rec.fetch(resume=True)
        report = read_json(os.path.join(rec.state_dir, "resume_report.json"))
        self.assertTrue(report["rejected"])
        self.assertEqual(read_file(meta["target"]), self.body)

    def test_incomplete_coverage_never_publishes(self):
        asm = self.assembler(concurrency=1)
        asm.plan(self.object_id)
        asm.fetch_ranges([ByteRange(0, 1000)])
        target = os.path.join(asm.state_dir, "target.bin")
        self.assertFalse(os.path.exists(target))
        self.assertFalse(asm.inspect()["complete_coverage"])
        # Directly asking the finaliser to publish incomplete coverage must fail.
        from assembler import ConfirmedStore
        state = asm._load()
        store = ConfirmedStore(state["total"], asm.body_path, asm.journal_path,
                               state["etag"], state["sha256"])
        try:
            store.load_and_validate()
            with self.assertRaises(ProtocolViolation):
                asm._finalize(state, store)
        finally:
            store.close()
        self.assertFalse(os.path.exists(target))

    def test_permanent_truncation_aborts_without_publish(self):
        """If every response is cut before any byte, retries run out and the
        task aborts; nothing is published and no byte is invented."""
        self.server.faults.chunk_size = 4096
        self.server.faults.drop_next(0, count=100_000)
        asm = self.assembler(concurrency=1, chunk_size=8192, retries=3)
        asm.plan(self.object_id)
        with self.assertRaises(Exception):
            asm.fetch()
        self.server.faults.reset()
        st = read_json(os.path.join(asm.state_dir, "state.json"))
        self.assertEqual(st["status"], "aborted")
        self.assertFalse(os.path.exists(os.path.join(asm.state_dir, "target.bin")))
        insp = asm.inspect()
        self.assertFalse(insp["complete_coverage"])

    def test_416_on_get_is_fatal_not_empty(self):
        asm = self.assembler(concurrency=1)
        state = asm.plan(self.object_id)
        with self.assertRaises(ProtocolViolation):
            asm._interpret(416, {"etag": self.version.etag,
                                 "content-range": f"bytes */{len(self.body)}"},
                           b"", 0, False, [ByteRange(0, 9)], state, "test")

    def test_404_on_plan(self):
        asm = self.assembler()
        with self.assertRaises(Exception):
            asm.plan("nope")

    def test_plan_etag_mismatch_detected(self):
        asm = self.assembler()
        with self.assertRaises(SourceVersionChanged):
            asm.plan(self.object_id, etag='"' + "1" * 64 + '"')

    def test_foreign_etag_response_aborts(self):
        """If a proxy/man-in-the-middle returns a different ETag, abort."""
        asm = self.assembler(concurrency=1, chunk_size=64 * 1024)
        asm.plan(self.object_id)
        # Directly exercise interpretation with a mismatched ETag.
        with self.assertRaises(SourceVersionChanged):
            asm._interpret(206, {"etag": '"' + "2" * 64 + '"',
                                 "content-range": "bytes 0-9/10"},
                           b"0123456789", 10, False, [ByteRange(0, 9)],
                           asm._load(), "test")

    def test_200_fallback_is_verified(self):
        asm = self.assembler(concurrency=1, chunk_size=64 * 1024)
        state = asm.plan(self.object_id)
        parsed = asm._interpret(200, {"etag": self.version.etag}, self.body,
                                len(self.body), False, [ByteRange(0, 9)],
                                state, "test")
        self.assertEqual(parsed.status, 200)
        self.assertEqual(parsed.parts[0].data, self.body)
        with self.assertRaises(SourceVersionChanged):
            asm._interpret(200, {"etag": self.version.etag}, b"x" * len(self.body),
                           len(self.body), False, [ByteRange(0, 9)], state, "test")


class RequestLogTests(LiveServerCase):
    def test_log_records_etag_ranges_and_truncation(self):
        self.server.log.clear()
        from assembler import Assembler as A
        asm = A(self.base, os.path.join(self.tmp, "log"), concurrency=2,
                chunk_size=64 * 1024, log_fn=lambda m: None)
        self.server.faults.chunk_size = 1024
        self.server.faults.drop_next(5000, count=1)
        asm.plan(self.object_id)
        asm.fetch()
        s, _, b = http(f"{self.base}/__control/log?object_id={self.object_id}")
        self.assertEqual(s, 200)
        payload = json.loads(b)
        entries = [e for e in payload["entries"] if e["method"] == "GET"]
        self.assertTrue(entries)
        body_entries = [e for e in entries if e["status"] in (200, 206)]
        self.assertTrue(body_entries)
        for e in body_entries:
            self.assertEqual(e["response_etag"], self.version.etag)
            self.assertEqual(e["request_etag"], self.version.etag)
        self.assertTrue(any(e["truncated"] for e in entries))


if __name__ == "__main__":
    unittest.main(verbosity=2)
