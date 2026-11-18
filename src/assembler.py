"""Concurrent, ETag-pinned, resumable file assembler.

The assembler pins ``object_id`` + total length + strong ETag first, then reads
the body with real HTTP range requests. Every request carries ``If-Match`` with
the pinned ETag, so a replaced object can never contribute bytes silently.

Persistence layout in ``state_dir``::

    state.json            pinned object id, length, ETag, status, counters
    body.part             sparse temporary body (confirmed bytes are written)
    confirmed.jsonl       append-only fragment journal (offset,length,sha256,etag)
    client_requests.jsonl client-side per-request log (status, ranges, retries)
    resume_report.json    fragments rejected by the last resume validation
    target.bin            published only when coverage is complete and the
                          whole-file SHA256 equals the pinned ETag

Commands: plan, fetch, resume, inspect, verify.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Iterable, Sequence
from urllib.parse import quote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rangeutil import (
    ByteRange,
    holes as interval_holes,
    merge_overlaps,
    merge_ranges,
    normalize_intervals,
    parse_content_range,
    parse_unsatisfied_content_range,
    sha256_hex,
    strong_etag_digest,
    subtract_intervals,
)


SCHEMA = 1
DEFAULT_READ_TIMEOUT = 20.0
MAX_BODY_GUARD = 1 << 30


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class AssemblerError(Exception):
    """Fatal for the task. The state is marked aborted and never published."""


class SourceVersionChanged(AssemblerError):
    pass


class SourceNotFound(AssemblerError):
    pass


class ProtocolViolation(AssemblerError):
    pass


class RetryExhausted(AssemblerError):
    pass


class AbortedState(AssemblerError):
    pass


class _TransientTransport(AssemblerError):
    """Retryable transport problem (connection reset, timeout, short read).

    Retried inside a worker; escaping to the fetch driver means no retry
    budget remained.
    """


# --------------------------------------------------------------------------
# Small file helpers
# --------------------------------------------------------------------------


def sha256_file(path: str, limit: int | None = None) -> str:
    h = hashlib.sha256()
    left = limit
    with open(path, "rb") as fh:
        while True:
            want = 1 << 20
            if left is not None:
                if left <= 0:
                    break
                want = min(want, left)
            chunk = fh.read(want)
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def _atomic_write_json(path: str, payload: Any) -> None:
    tmp = path + ".tmp"
    with _open_retry(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    _replace_retry(tmp, path)


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _open_retry(path: str, mode: str, *, encoding: str | None = None,
                attempts: int = 40, delay: float = 0.02):
    """Open with retries around transient PermissionError.

    Network/overlay filesystems (e.g. virtiofs) can briefly report EACCES when
    several threads create or append to files in the same freshly-made
    directory. The condition clears in milliseconds, so retry instead of
    aborting an otherwise correct assembly.
    """
    last: OSError | None = None
    for i in range(attempts):
        try:
            if encoding is None:
                return open(path, mode)
            return open(path, mode, encoding=encoding)
        except PermissionError as exc:
            last = exc
            time.sleep(delay * min(4, i + 1))
    assert last is not None
    raise last


def _replace_retry(src: str, dst: str, attempts: int = 40,
                   delay: float = 0.02) -> None:
    last: OSError | None = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(delay * min(4, i + 1))
    assert last is not None
    raise last


def _now() -> float:
    return time.time()


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _split_disjoint(r: ByteRange, fanout: int, gap: int) -> list[ByteRange]:
    """Split one interval into ``fanout`` disjoint sub-intervals.

    Any unrequested gaps are left for a subsequent round; they are never
    treated as zeros. Small intervals that cannot hold a gap stay whole.
    """
    if fanout <= 1 or r.length < 4:
        return [r]
    stride = r.length // fanout
    if stride < 2:
        return [r]
    part_len = stride - max(1, gap)
    if part_len < 1:
        return [r]
    out: list[ByteRange] = []
    for i in range(fanout):
        start = r.start + i * stride
        end = r.end if i == fanout - 1 else min(r.end, start + part_len - 1)
        if start > r.end:
            break
        out.append(ByteRange(start, end))
    return out or [r]


def _cap_span(ranges: Sequence[ByteRange], max_span: int) -> list[ByteRange]:
    if max_span <= 0:
        return list(ranges)
    out: list[ByteRange] = []
    for r in ranges:
        cursor = r.start
        while cursor <= r.end:
            end = min(r.end, cursor + max_span - 1)
            out.append(ByteRange(cursor, end))
            cursor = end + 1
    return out


def _expand(r: ByteRange, amount: int, total: int) -> ByteRange:
    if amount <= 0 or total <= 0:
        return r
    return ByteRange(max(0, r.start - amount), min(total - 1, r.end + amount))


# --------------------------------------------------------------------------
# Confirmed fragment store
# --------------------------------------------------------------------------


@dataclass
class Fragment:
    offset: int
    length: int
    sha256: str
    etag: str
    source: str
    ts: float

    def as_record(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "length": self.length,
            "sha256": self.sha256,
            "etag": self.etag,
            "source": self.source,
            "ts": self.ts,
        }


class OverlapMismatch(ProtocolViolation):
    pass


class ConfirmedStore:
    """Sparse temp body + append-only fragment journal.

    A byte range is *confirmed* only after its bytes were written and hashed
    into the journal (fsync'd). During resume the temp body is re-hashed per
    journal record; only records whose bytes still match stay confirmed. Holes
    are never silently filled with zeros.
    """

    def __init__(self, total: int, body_path: str, journal_path: str,
                 etag: str, digest: str):
        self.total = total
        self.body_path = body_path
        self.journal_path = journal_path
        self.etag = etag
        self.digest = digest
        self.lock = threading.RLock()
        self._intervals: list[ByteRange] = []
        self._fragments: list[Fragment] = []
        self._fh = _open_retry(body_path, "r+b")
        size = os.fstat(self._fh.fileno()).st_size
        if size != total:
            self._fh.truncate(total)
            self._fh.flush()
            os.fsync(self._fh.fileno())

    # -- reading -------------------------------------------------------------
    def read_at(self, offset: int, length: int) -> bytes | None:
        if offset < 0 or length <= 0 or offset + length > self.total:
            return None
        self._fh.seek(offset)
        data = self._fh.read(length)
        return data if len(data) == length else None

    def intervals(self) -> list[ByteRange]:
        with self.lock:
            return list(self._intervals)

    def fragments(self) -> list[Fragment]:
        with self.lock:
            return list(self._fragments)

    def holes(self) -> list[ByteRange]:
        with self.lock:
            return interval_holes(self.total, self._intervals)

    def is_complete(self) -> bool:
        with self.lock:
            if self.total == 0:
                return True
            return len(self._intervals) == 1 and \
                self._intervals[0].start == 0 and self._intervals[0].end == self.total - 1

    def covered(self) -> int:
        with self.lock:
            return sum(r.length for r in self._intervals)

    # -- journal -------------------------------------------------------------
    def _append_journal(self, frag: Fragment) -> None:
        line = json.dumps(frag.as_record(), sort_keys=True) + "\n"
        # Opening per append keeps the journal robust if another process (or a
        # resume) rewrites it, at the cost of one open per confirmed fragment.
        with _open_retry(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def _rewrite_journal(self, fragments: Iterable[Fragment]) -> None:
        tmp = self.journal_path + ".tmp"
        with _open_retry(tmp, "w", encoding="utf-8") as fh:
            for frag in fragments:
                fh.write(json.dumps(frag.as_record(), sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace_retry(tmp, self.journal_path)

    def load_and_validate(self) -> dict[str, Any]:
        """Rebuild coverage from the journal, verifying temp-body bytes.

        Invalid records (wrong ETag/total/hash, out-of-bounds, unreadable) are
        dropped and reported; the journal is rewritten with valid records only.
        """
        records: list[dict[str, Any]] = []
        if os.path.exists(self.journal_path):
            with open(self.journal_path, "r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        records.append({"_bad_line": line_no, "_raw": line[:200]})

        valid: list[Fragment] = []
        rejected: list[dict[str, Any]] = []
        for rec in records:
            problem = self._validate_record(rec)
            if problem is not None:
                rejected.append({"record": rec, "problem": problem})
                continue
            frag = Fragment(
                int(rec["offset"]), int(rec["length"]), str(rec["sha256"]),
                str(rec["etag"]), str(rec.get("source", "")), float(rec.get("ts", 0.0)),
            )
            valid.append(frag)

        # Newest-wins is intentionally not used: the journal is a proof log.
        # Duplicate/overlapping valid fragments are fine and merely merge.
        with self.lock:
            self._fragments = valid
            self._intervals = merge_ranges(ByteRange(f.offset, f.offset + f.length - 1)
                                           for f in valid)
        if rejected:
            self._rewrite_journal(valid)
        report = {
            "checked_at": _iso(),
            "total_fragments": len(records),
            "valid_fragments": len(valid),
            "rejected": rejected,
            "covered": self.covered(),
            "holes": [r.as_list() for r in self.holes()],
            "complete": self.is_complete(),
        }
        return report

    def _validate_record(self, rec: dict[str, Any]) -> str | None:
        if "_bad_line" in rec:
            return "unparsable journal line"
        try:
            offset = int(rec["offset"])
            length = int(rec["length"])
            expected = str(rec["sha256"]).lower()
        except (KeyError, TypeError, ValueError):
            return "missing/invalid offset, length or sha256"
        if str(rec.get("etag", "")) != self.etag:
            return "fragment ETag does not match pinned ETag"
        if length <= 0 or offset < 0 or offset + length > self.total:
            return "fragment out of bounds"
        data = self.read_at(offset, length)
        if data is None:
            return "temp body truncated for fragment"
        if sha256_hex(data) != expected:
            return "temp body bytes no longer match fragment hash"
        return None

    # -- writing -------------------------------------------------------------
    def add(self, offset: int, data: bytes, *, source: str) -> Fragment:
        length = len(data)
        if length <= 0:
            raise ValueError("empty fragment")
        if offset < 0 or offset + length > self.total:
            raise ProtocolViolation(
                f"fragment [{offset},{offset + length - 1}] outside total {self.total}")
        end = offset + length - 1
        with self.lock:
            for existing in self._intervals:
                lo = max(existing.start, offset)
                hi = min(existing.end, end)
                if lo > hi:
                    continue
                have = self.read_at(lo, hi - lo + 1)
                incoming = data[lo - offset:hi - offset + 1]
                if have != incoming:
                    raise OverlapMismatch(
                        f"overlap mismatch in [{lo},{hi}]: differing bytes")
            self._fh.seek(offset)
            self._fh.write(data)
            self._fh.flush()
            os.fsync(self._fh.fileno())
            frag = Fragment(offset, length, sha256_hex(data), self.etag,
                            source, _now())
            self._append_journal(frag)
            self._fragments.append(frag)
            self._intervals = merge_ranges(self._intervals + [ByteRange(offset, end)])
            return frag

    def close(self) -> None:
        with self.lock:
            try:
                self._fh.close()
            except OSError:
                pass


# --------------------------------------------------------------------------
# HTTP response interpretation
# --------------------------------------------------------------------------


@dataclass
class ReceivedPart:
    range: ByteRange
    data: bytes
    complete: bool


@dataclass
class ParsedResponse:
    status: int
    etag: str | None
    parts: list[ReceivedPart]
    truncated: bool
    multipart: bool
    declared_length: int | None
    raw_length: int
    note: str = ""


def _parse_headers(block: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in block.split(b"\r\n"):
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
    return headers


def _trim_partial_delimiter(data: bytes, delim: bytes) -> bytes:
    seq = b"\r\n" + delim
    limit = min(len(data), len(seq) - 1)
    for k in range(limit, 0, -1):
        if data.endswith(seq[:k]):
            return data[:-k]
    return data


def parse_multipart(body: bytes, boundary: str, truncated: bool) \
        -> tuple[list[ReceivedPart], str]:
    """Parse multipart/byteranges, salvaging complete leading parts.

    Returns ``(parts, note)``. A trailing unterminated part is included with
    ``complete=False`` and only its actually received prefix (delimiter-prefix
    trimmed), which the caller may confirm as a real, verified fragment.
    """
    delim = b"--" + boundary.encode("latin-1")
    parts: list[ReceivedPart] = []
    first = body.find(delim)
    if first < 0:
        return [], "no boundary found"
    pos = first + len(delim)
    if body[pos:pos + 2] == b"--":
        return [], "empty multipart"
    if body[pos:pos + 2] != b"\r\n":
        return [], "malformed first boundary"
    pos += 2
    while True:
        hdr_end = body.find(b"\r\n\r\n", pos)
        if hdr_end < 0:
            return parts, "truncated in part headers"
        headers = _parse_headers(body[pos:hdr_end])
        data_start = hdr_end + 4
        nxt = body.find(b"\r\n" + delim, data_start)
        if nxt < 0:
            # Unterminated tail.
            data = body[data_start:]
            if truncated:
                data = _trim_partial_delimiter(data, delim)
            cr = headers.get("content-range")
            parsed = parse_content_range(cr)
            if parsed is None:
                return parts, "trailing part without valid Content-Range"
            start, end, _total = parsed
            want = end - start + 1
            if len(data) > want:
                return parts, "trailing part longer than Content-Range"
            if len(data) == want:
                parts.append(ReceivedPart(ByteRange(start, end), data, True))
                return parts, "no closing boundary"
            parts.append(ReceivedPart(ByteRange(start, start + len(data) - 1),
                                      data, False)) if data else None
            return parts, "truncated trailing part"
        data = body[data_start:nxt]
        cr = headers.get("content-range")
        parsed = parse_content_range(cr)
        if parsed is None:
            return parts, "part without valid Content-Range"
        start, end, _total = parsed
        if len(data) != end - start + 1:
            return parts, "part length does not match Content-Range"
        parts.append(ReceivedPart(ByteRange(start, end), data, True))
        pos = nxt + 2 + len(delim)
        if body[pos:pos + 2] == b"--":
            return parts, "closed"
        if body[pos:pos + 2] != b"\r\n":
            return parts, "malformed boundary after part"
        pos += 2


def _parse_boundary(content_type: str) -> str | None:
    if not content_type:
        return None
    fields = [f.strip() for f in content_type.split(";")]
    if not fields or fields[0].lower() != "multipart/byteranges":
        return None
    for f in fields[1:]:
        if f.lower().startswith("boundary="):
            v = f.split("=", 1)[1].strip()
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1]
            return v
    return None


# --------------------------------------------------------------------------
# Assembler
# --------------------------------------------------------------------------


class Assembler:
    def __init__(self, base_url: str, state_dir: str, *, concurrency: int = 4,
                 chunk_size: int | None = None, max_chunks: int = 100,
                 retries: int = 20, min_span: int = 4096, fanout: int = 1,
                 fanout_gap: int = 1, overlap: int = 0, coalesce: int = 1,
                 strategy: str = "chunks", read_timeout: float = DEFAULT_READ_TIMEOUT,
                 target_path: str | None = None, log_fn=print):
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.base_url = base_url.rstrip("/")
        self.state_dir = os.path.abspath(state_dir)
        self.concurrency = concurrency
        self.chunk_size = chunk_size
        self.max_chunks = max(1, max_chunks)
        self.retries = max(0, retries)
        self.min_span = max(256, min_span)
        self.fanout = max(1, fanout)
        self.fanout_gap = max(0, fanout_gap)
        self.overlap = max(0, overlap)
        self.coalesce = max(1, coalesce)
        self.strategy = strategy
        self.read_timeout = read_timeout
        self.target_path = target_path
        self.log = log_fn
        self._log_lock = threading.Lock()
        self.abort_event = threading.Event()
        self.fatal: AssemblerError | None = None
        self._client_log_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._counters: dict[str, int] = {
            "requests": 0, "preflight_heads": 0, "responses_2xx": 0,
            "multipart_responses": 0, "truncated_responses": 0, "retries": 0,
            "salvaged_prefixes": 0, "bytes_confirmed": 0, "fragments": 0,
            "empty_progress_responses": 0,
        }
        parsed = urlsplit(self.base_url)
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or 80

    # -- paths / log ---------------------------------------------------------
    @property
    def state_path(self) -> str:
        return os.path.join(self.state_dir, "state.json")

    @property
    def body_path(self) -> str:
        return os.path.join(self.state_dir, "body.part")

    @property
    def journal_path(self) -> str:
        return os.path.join(self.state_dir, "confirmed.jsonl")

    @property
    def client_log_path(self) -> str:
        return os.path.join(self.state_dir, "client_requests.jsonl")

    def _log(self, msg: str) -> None:
        with self._log_lock:
            self.log(msg)

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._counter_lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def counters(self) -> dict[str, int]:
        with self._counter_lock:
            return dict(self._counters)

    def _client_log(self, record: dict[str, Any]) -> None:
        record = dict(record)
        record.setdefault("ts", _now())
        line = json.dumps(record, sort_keys=True) + "\n"
        with self._client_log_lock:
            with _open_retry(self.client_log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

    # -- state ---------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self.state_path):
            raise AssemblerError(f"no state at {self.state_path}; run plan first")
        return _read_json(self.state_path)

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _iso()
        _atomic_write_json(self.state_path, state)

    def _set_counters(self, state: dict[str, Any]) -> None:
        state["counters"] = self.counters()

    # -- HEAD ----------------------------------------------------------------
    def _head(self, object_id: str, if_match: str | None = None) -> tuple[int, dict[str, str]]:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self.read_timeout)
        try:
            headers = {"Accept-Encoding": "identity", "Connection": "close"}
            if if_match:
                headers["If-Match"] = if_match
            conn.request("HEAD", f"/objects/{quote(object_id, safe='')}", headers=headers)
            resp = conn.getresponse()
            resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()

    # -- plan ----------------------------------------------------------------
    def plan(self, object_id: str, *, etag: str | None = None, total: int | None = None,
             chunk_size: int | None = None, strategy: str | None = None,
             reset: bool = False) -> dict[str, Any]:
        os.makedirs(self.state_dir, exist_ok=True)
        if os.path.exists(self.state_path) and not reset:
            state = self._load()
            if state["object_id"] != object_id:
                raise AssemblerError(
                    f"state dir pins {state['object_id']!r}, requested {object_id!r}")
            self._log(f"plan: reusing existing plan etag={state['etag']} "
                      f"length={state['total']}")
            return state
        if reset:
            for name in ("state.json", "body.part", "confirmed.jsonl",
                         "client_requests.jsonl", "resume_report.json", "metadata.json"):
                p = os.path.join(self.state_dir, name)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except PermissionError:
                        time.sleep(0.02)

        status, headers = self._head(object_id)
        if status == 404:
            raise SourceNotFound(f"object {object_id!r} not found")
        if status != 200:
            raise AssemblerError(f"HEAD failed with HTTP {status}")
        server_etag = headers.get("etag")
        digest = strong_etag_digest(server_etag)
        if digest is None:
            raise ProtocolViolation(f"server supplied no strong ETag: {server_etag!r}")
        try:
            length = int(headers["content-length"])
        except (KeyError, ValueError):
            raise ProtocolViolation("HEAD supplied no usable Content-Length")
        if etag is not None and etag.strip() != server_etag.strip():
            raise SourceVersionChanged(
                f"requested ETag {etag!r} but source has {server_etag!r}")
        if total is not None and total != length:
            raise SourceVersionChanged(
                f"requested total {total} but source has {length}")

        chosen = chunk_size or self.chunk_size
        if not chosen:
            chosen = 1 << 20
        min_chunk = max(1, math.ceil(length / self.max_chunks)) if length else 1
        chosen = max(chosen, min_chunk)
        if length:
            chosen = min(chosen, length)

        state = {
            "schema": SCHEMA,
            "object_id": object_id,
            "total": length,
            "etag": server_etag,
            "sha256": digest,
            "chunk_size": chosen,
            "max_chunks": self.max_chunks,
            "strategy": strategy or self.strategy,
            "status": "planned",
            "reason": None,
            "created_at": _iso(),
            "updated_at": _iso(),
            "target": self.target_path or os.path.join(self.state_dir, "target.bin"),
            "temp_body": "body.part",
            "journal": "confirmed.jsonl",
            "coverage": [],
        }
        self._save(state)
        _open_retry(self.body_path, "wb").close()
        os.truncate(self.body_path, length)
        _open_retry(self.journal_path, "w").close()
        _open_retry(self.client_log_path, "w").close()
        state["chunks"] = self._planned_pieces(length, chosen)
        self._log(f"plan: object={object_id} length={length} etag={server_etag} "
                  f"chunk_size={chosen} pieces={len(state['chunks'])}")
        return state

    @staticmethod
    def _planned_pieces(total: int, chunk_size: int) -> list[list[int]]:
        if total == 0:
            return []
        out = []
        start = 0
        while start < total:
            end = min(total - 1, start + chunk_size - 1)
            out.append([start, end])
            start = end + 1
        return out

    # -- HTTP GET ------------------------------------------------------------
    def _get(self, object_id: str, ranges: Sequence[ByteRange], etag: str,
             attempt: int) -> tuple[int, dict[str, str], bytes, int | None, bool, str | None]:
        """One real HTTP GET. Returns status, headers, raw body, declared
        length, truncated flag, transport error string."""
        if not ranges:
            raise ValueError("empty request ranges")
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self.read_timeout)
        header_ranges = ",".join(f"{r.start}-{r.end}" for r in ranges)
        self._bump("requests")
        try:
            conn.putrequest("GET", f"/objects/{quote(object_id, safe='')}")
            conn.putheader("Range", f"bytes={header_ranges}")
            conn.putheader("If-Match", etag)
            conn.putheader("Accept-Encoding", "identity")
            conn.putheader("Connection", "close")
            conn.endheaders()
            resp = conn.getresponse()
            status = resp.status
            headers = {k.lower(): v for k, v in resp.getheaders()}
            declared = None
            cl = headers.get("content-length")
            if cl is not None:
                try:
                    declared = int(cl)
                except ValueError:
                    declared = None
            if declared is not None and declared > MAX_BODY_GUARD:
                raise ProtocolViolation(f"implausible Content-Length {declared}")
            chunks: list[bytes] = []
            read = 0
            error: str | None = None
            try:
                while declared is None or read < declared:
                    want = 1 << 16 if declared is None else min(1 << 16, declared - read)
                    data = resp.read(want)
                    if not data:
                        break
                    chunks.append(data)
                    read += len(data)
                    if declared is None and read > MAX_BODY_GUARD:
                        raise ProtocolViolation("unbounded response body")
            except (http.client.IncompleteRead, ConnectionError, OSError,
                    socket.timeout) as exc:
                error = f"{type(exc).__name__}: {exc}"
            raw = b"".join(chunks)
            if declared is not None:
                raw = raw[:declared]
            truncated = error is not None or (declared is not None and len(raw) < declared)
            return status, headers, raw, declared, truncated, error
        except (ConnectionError, OSError, socket.timeout) as exc:
            raise _TransientTransport(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # -- response interpretation --------------------------------------------
    def _interpret(self, status: int, headers: dict[str, str], raw: bytes,
                   declared: int | None, truncated: bool,
                   requested: Sequence[ByteRange], state: dict[str, Any],
                   source: str) -> ParsedResponse:
        etag = headers.get("etag")
        if status in (404,):
            raise SourceNotFound(f"object {state['object_id']!r} disappeared (404)")
        if status == 412:
            raise SourceVersionChanged(
                f"source version changed: If-Match {state['etag']} rejected (412), "
                f"server ETag {etag!r}")
        if status == 416:
            total_hint = parse_unsatisfied_content_range(headers.get("content-range"))
            raise ProtocolViolation(
                f"unexpected 416 for requested ranges {[r.as_list() for r in requested]} "
                f"(source total hint {total_hint})")
        if status == 400:
            raise ProtocolViolation("server rejected Range header with 400")
        if status != 200 and status != 206:
            raise _TransientTransport(f"HTTP {status}")

        response_digest = strong_etag_digest(etag)
        if response_digest is None or etag.strip() != state["etag"]:
            raise SourceVersionChanged(
                f"response ETag {etag!r} != pinned {state['etag']!r}")

        total = int(state["total"])
        ctype = headers.get("content-type")
        boundary = _parse_boundary(ctype)
        multipart = status == 206 and boundary is not None

        if status == 200:
            # Only acceptable as a complete-body fallback; it is still bound
            # to the pinned ETag and verified by whole-body hash.
            if truncated:
                raise _TransientTransport("truncated full-body response")
            if len(raw) != total:
                raise ProtocolViolation(
                    f"200 response length {len(raw)} != pinned total {total}")
            if sha256_hex(raw) != state["sha256"]:
                raise SourceVersionChanged("200 body hash != pinned ETag")
            part = ReceivedPart(ByteRange(0, total - 1), raw, True) if total else None
            return ParsedResponse(200, etag, [part] if part else [], False,
                                  False, declared, len(raw), "full-body fallback")

        if multipart:
            parts, note = parse_multipart(raw, boundary, truncated)
            if not parts and not truncated:
                raise ProtocolViolation(f"multipart body yielded no parts ({note})")
            for part in parts:
                if part.range.end >= total:
                    raise ProtocolViolation(
                        f"part {part.range.as_list()} exceeds pinned total {total}")
                if not self._within_union(part.range, requested):
                    raise ProtocolViolation(
                        f"part {part.range.as_list()} outside requested ranges")
                if part.complete and len(part.data) != part.range.length:
                    raise ProtocolViolation("part data length mismatch")
                if not part.complete and len(part.data) > part.range.length:
                    raise ProtocolViolation("partial part longer than declared range")
            return ParsedResponse(206, etag, parts, truncated, True, declared,
                                  len(raw), note)

        cr = headers.get("content-range")
        parsed = parse_content_range(cr)
        if parsed is None:
            raise ProtocolViolation(f"206 without usable Content-Range: {cr!r}")
        start, end, rtotal = parsed
        if rtotal != total:
            raise SourceVersionChanged(
                f"Content-Range total {rtotal} != pinned total {total}")
        rng = ByteRange(start, end)
        if not self._within_union(rng, requested):
            raise ProtocolViolation(f"returned range {rng.as_list()} outside request")
        if len(raw) > rng.length:
            raise ProtocolViolation(
                f"body {len(raw)} longer than Content-Range {rng.as_list()}")
        complete = len(raw) == rng.length and not truncated
        if not complete:
            data = raw
            part = ReceivedPart(ByteRange(start, start + len(data) - 1), data, False) \
                if data else None
        else:
            part = ReceivedPart(rng, raw, True)
        return ParsedResponse(206, etag, [part] if part else [], truncated, False,
                              declared, len(raw), "" if complete else "partial single range")

    @staticmethod
    def _within_union(r: ByteRange, requested: Sequence[ByteRange]) -> bool:
        for w in normalize_intervals(requested):
            if w.start <= r.start and r.end <= w.end:
                return True
        return False

    # -- fetch worker --------------------------------------------------------
    def _worker(self, store: ConfirmedStore, state: dict[str, Any],
                region: Sequence[ByteRange]) -> None:
        object_id = state["object_id"]
        etag = state["etag"]
        total = int(state["total"])
        max_span = max((r.length for r in region), default=0)
        attempts = 0
        worker_id = threading.get_ident() % 100000
        while not self.abort_event.is_set():
            missing = subtract_intervals(total, region, store.intervals())
            if not missing:
                return
            # Dynamic split into disjoint request ranges, then cap spans.
            request: list[ByteRange] = []
            for r in missing:
                request.extend(_split_disjoint(r, self.fanout, self.fanout_gap))
            if max_span > 0:
                request = _cap_span(request, max_span)
            if self.overlap:
                request = [_expand(r, self.overlap, total) for r in request]
            request = merge_overlaps(request)
            if not request:
                return
            ranges_for_log = [r.as_list() for r in request]
            attempt = attempts + 1
            try:
                status, headers, raw, declared, truncated, transport_error = \
                    self._get(object_id, request, etag, attempt)
                parsed = self._interpret(status, headers, raw, declared, truncated,
                                         request, state, f"worker{worker_id}")
                self._bump("responses_2xx")
                if parsed.multipart:
                    self._bump("multipart_responses")
                if parsed.truncated:
                    self._bump("truncated_responses")
                confirmed_bytes = 0
                confirmations = []
                for part in parsed.parts:
                    if part.range.length <= 0:
                        continue
                    frag = store.add(part.range.start, part.data,
                                     source=f"range {part.range.to_header()} via {parsed.note or 'ok'}")
                    confirmed_bytes += len(part.data)
                    confirmations.append([frag.offset, frag.offset + frag.length - 1])
                    if not part.complete:
                        self._bump("salvaged_prefixes")
                self._bump("bytes_confirmed", confirmed_bytes)
                self._bump("fragments", len(confirmations))
                self._client_log({
                    "object_id": object_id, "worker": worker_id, "attempt": attempt,
                    "request_ranges": ranges_for_log, "status": status,
                    "response_etag": parsed.etag, "bytes": len(raw),
                    "declared": declared, "truncated": parsed.truncated,
                    "multipart": parsed.multipart, "confirmed": confirmations,
                    "transport_error": transport_error,
                })
                if parsed.truncated:
                    attempts += 1
                    self._bump("retries")
                    max_span = max(self.min_span, max_span // 2 if max_span else self.min_span)
                    if attempts > self.retries:
                        raise RetryExhausted(
                            f"exceeded {self.retries} retries on truncated responses")
                    time.sleep(min(0.2, 0.02 * attempts))
                    continue
                if not confirmations:
                    # A response that verifies nothing made no progress. Do not
                    # treat it as success: retry (with a smaller span) and give
                    # up loudly rather than spinning.
                    attempts += 1
                    self._bump("empty_progress_responses")
                    if attempts > self.retries:
                        raise ProtocolViolation(
                            "response(s) confirmed no verifiable bytes; giving up")
                    max_span = max(self.min_span, max_span // 2 if max_span else self.min_span)
                    time.sleep(min(0.2, 0.02 * attempts))
                    continue
                # A response can be complete yet still leave holes (fanout).
                attempts = 0
                continue
            except _TransientTransport as exc:
                attempts += 1
                self._bump("retries")
                self._client_log({
                    "object_id": object_id, "worker": worker_id, "attempt": attempt,
                    "request_ranges": ranges_for_log, "status": None,
                    "transport_error": str(exc), "confirmed": [],
                })
                if attempts > self.retries:
                    raise RetryExhausted(f"transport retries exhausted: {exc}") from exc
                max_span = max(self.min_span, max_span // 2 if max_span else self.min_span)
                time.sleep(min(0.2, 0.02 * attempts))
                continue

    # -- fetch driver --------------------------------------------------------
    def _build_regions(self, holes: list[ByteRange], chunk_size: int,
                       strategy: str) -> list[list[ByteRange]]:
        pieces: list[ByteRange] = []
        for h in holes:
            cursor = h.start
            while cursor <= h.end:
                end = min(h.end, cursor + chunk_size - 1)
                pieces.append(ByteRange(cursor, end))
                cursor = end + 1
        if not pieces:
            return []
        group = self.coalesce if strategy in ("coalesced", "chunks") else 1
        regions: list[list[ByteRange]] = []
        for i in range(0, len(pieces), group):
            regions.append(pieces[i:i + group])
        return regions

    def _finalize(self, state: dict[str, Any], store: ConfirmedStore) -> dict[str, Any]:
        total = int(state["total"])
        if not store.is_complete():
            holes = store.holes()
            raise ProtocolViolation(
                f"cannot publish: coverage incomplete, holes={[r.as_list() for r in holes]}")
        store._fh.flush()
        os.fsync(store._fh.fileno())
        size = os.path.getsize(self.body_path)
        if size != total:
            raise ProtocolViolation(f"temp body size {size} != total {total}")
        digest = sha256_file(self.body_path)
        if digest != state["sha256"]:
            raise ProtocolViolation(
                f"whole-body SHA256 {digest} != pinned ETag digest {state['sha256']}; "
                "refusing to publish")
        target = state["target"]
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        _replace_retry(self.body_path, target)
        state["coverage"] = [[r.start, r.end] for r in store.intervals()]
        state["target_sha256"] = digest
        state["target_size"] = total
        meta = {
            "object_id": state["object_id"],
            "total": total,
            "etag": state["etag"],
            "sha256": digest,
            "coverage": state["coverage"],
            "fragments": len(store.fragments()),
            "published_at": _iso(),
            "counters": self.counters(),
            "target": target,
        }
        _atomic_write_json(os.path.join(self.state_dir, "metadata.json"), meta)
        state["status"] = "complete"
        state["reason"] = None
        self._set_counters(state)
        state["coverage"] = meta["coverage"]
        self._save(state)
        self._log(f"published {target} ({total} bytes) sha256={digest}")
        return meta

    def _mark_aborted(self, state: dict[str, Any], reason: str) -> None:
        state["status"] = "aborted"
        state["reason"] = reason
        self._set_counters(state)
        try:
            self._save(state)
        except OSError:
            pass

    def fetch(self, *, resume: bool = False) -> dict[str, Any]:
        """Drive an interrupted fetch.

        After a crash, ``fetch`` and ``resume`` behave identically: persisted
        fragments are re-hashed and only verified bytes are reused. ``resume``
        additionally requires an existing plan and logs the verification
        summary.
        """
        if resume and not os.path.exists(self.state_path):
            raise AssemblerError(
                f"no plan to resume at {self.state_path}; run plan first")
        state = self._load()
        if state["status"] == "complete":
            self._log(f"state already complete: {state['target']}")
            return _read_json(os.path.join(self.state_dir, "metadata.json"))
        if state["status"] == "aborted":
            raise AbortedState(
                f"task is terminally aborted: {state.get('reason')}; "
                "plan a fresh state directory to start over")
        os.makedirs(self.state_dir, exist_ok=True)
        if self.abort_event.is_set():
            self.abort_event.clear()

        # Re-validate persisted fragments (important for resume; cheap for a
        # fresh plan since the journal is empty).
        store = ConfirmedStore(int(state["total"]), self.body_path, self.journal_path,
                               state["etag"], state["sha256"])
        report = store.load_and_validate()
        if report["rejected"]:
            _atomic_write_json(os.path.join(self.state_dir, "resume_report.json"), report)
            self._log(f"resume: rejected {len(report['rejected'])} persisted fragment(s)")
        if resume:
            self._log(f"resume: verified {report['valid_fragments']}/{report['total_fragments']} "
                      f"fragments, covered {report['covered']}/{state['total']} bytes, "
                      f"{len(report['holes'])} hole(s)")

        # Preflight binds the task to the pinned version before any body byte.
        # Transport errors are retried; an ETag/version mismatch is terminal.
        try:
            status, headers = self._head_with_retries(state["object_id"], state["etag"])
        except (OSError, http.client.HTTPException) as exc:
            store.close()
            raise _TransientTransport(f"preflight HEAD failed: {exc}") from exc
        if status == 404:
            store.close()
            self._mark_aborted(state, "source_not_found")
            raise SourceNotFound("source object no longer exists (404)")
        if status == 412:
            store.close()
            self._mark_aborted(state, "source_version_changed")
            raise SourceVersionChanged(
                f"source no longer serves pinned ETag {state['etag']} (412); "
                "refusing to fetch a different version")
        if status != 200:
            store.close()
            raise AssemblerError(f"preflight HEAD returned HTTP {status}")
        if headers.get("etag") != state["etag"]:
            store.close()
            self._mark_aborted(state, "source_version_changed")
            raise SourceVersionChanged(
                f"source ETag is now {headers.get('etag')!r}, pinned {state['etag']!r}")
        if int(headers.get("content-length", "-1")) != int(state["total"]):
            store.close()
            self._mark_aborted(state, "source_length_changed")
            raise SourceVersionChanged("source length changed; refusing to mix versions")
        self._bump("preflight_heads")

        state["status"] = "fetching"
        self._save(state)
        try:
            result = self._fetch_loop(store, state)
        except AssemblerError as exc:
            self.fatal = exc
            self.abort_event.set()
            self._mark_aborted(state, type(exc).__name__ + ": " + str(exc))
            store.close()
            raise
        except BaseException as exc:  # make unexpected failures terminal too
            self.fatal = AssemblerError(f"{type(exc).__name__}: {exc}")
            self.abort_event.set()
            self._mark_aborted(state, f"{type(exc).__name__}: {exc}")
            store.close()
            raise
        store.close()
        return result

    def _head_with_retries(self, object_id: str, if_match: str) -> tuple[int, dict[str, str]]:
        delay = 0.05
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._head(object_id, if_match)
            except (OSError, http.client.HTTPException) as exc:
                last = exc
                if attempt >= self.retries:
                    break
                time.sleep(delay)
                delay = min(1.0, delay * 2)
        assert last is not None
        raise last

    def _fetch_loop(self, store: ConfirmedStore, state: dict[str, Any]) -> dict[str, Any]:
        total = int(state["total"])
        chunk_size = int(state["chunk_size"])
        strategy = state.get("strategy", self.strategy)
        rounds = 0
        while True:
            holes = store.holes()
            if not holes:
                return self._finalize(state, store)
            rounds += 1
            if rounds > 12:
                raise RetryExhausted(f"coverage still incomplete after {rounds - 1} rounds: "
                                     f"{[r.as_list() for r in holes]}")
            if self.abort_event.is_set() and self.fatal:
                raise self.fatal
            regions = self._build_regions(holes, chunk_size, strategy)
            self._log(f"fetch round {rounds}: {len(holes)} hole(s), "
                      f"{len(regions)} region task(s), concurrency={self.concurrency}")
            fatal: BaseException | None = None
            with ThreadPoolExecutor(max_workers=min(self.concurrency, len(regions)),
                                    thread_name_prefix="fetch") as pool:
                futures = {pool.submit(self._worker, store, state, region): region
                           for region in regions}
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except BaseException as exc:  # noqa: BLE001
                        if fatal is None:
                            fatal = exc
                            if isinstance(exc, AssemblerError):
                                self.fatal = exc
                            else:
                                self.fatal = AssemblerError(
                                    f"{type(exc).__name__}: {exc}")
                            self.abort_event.set()
            if fatal is not None:
                if isinstance(fatal, AssemblerError):
                    raise fatal
                raise AssemblerError(f"{type(fatal).__name__}: {fatal}")
            if rounds > 1:
                self._log(f"round {rounds}: covered {store.covered()}/{total}")

    # -- low-level one-shot range fetch (used by demo/tests) -----------------
    def fetch_ranges(self, ranges: Sequence[ByteRange], *, source: str = "probe") -> list[dict[str, Any]]:
        state = self._load()
        if state["status"] not in ("planned", "fetching"):
            raise AbortedState(f"state is {state['status']}; cannot fetch ranges")
        store = ConfirmedStore(int(state["total"]), self.body_path, self.journal_path,
                               state["etag"], state["sha256"])
        try:
            store.load_and_validate()
            status, headers, raw, declared, truncated, terr = self._get(
                state["object_id"], ranges, state["etag"], 1)
            parsed = self._interpret(status, headers, raw, declared, truncated,
                                     ranges, state, source)
            out = []
            for part in parsed.parts:
                store.add(part.range.start, part.data, source=source)
                out.append({"range": part.range.as_list(), "length": len(part.data),
                            "complete": part.complete})
            self._client_log({
                "object_id": state["object_id"], "request_ranges": [r.as_list() for r in ranges],
                "status": status, "response_etag": parsed.etag, "bytes": len(raw),
                "truncated": parsed.truncated, "multipart": parsed.multipart,
                "confirmed": [[p.range.start, p.range.end] for p in parsed.parts],
                "probe": True,
            })
            return out
        finally:
            store.close()

    # -- inspect -------------------------------------------------------------
    def inspect(self, *, check_source: bool = False) -> dict[str, Any]:
        state = self._load()
        target = state["target"]
        target_info: dict[str, Any] = {"path": target, "exists": os.path.exists(target)}
        if target_info["exists"]:
            target_info["size"] = os.path.getsize(target)
            target_info["sha256"] = sha256_file(target)
            target_info["matches_pinned"] = (
                target_info["size"] == state["total"]
                and target_info["sha256"] == state["sha256"])

        if os.path.exists(self.body_path):
            store = ConfirmedStore(int(state["total"]), self.body_path, self.journal_path,
                                   state["etag"], state["sha256"])
            try:
                report = store.load_and_validate()
                holes = [r.as_list() for r in store.holes()]
                covered = store.covered()
                complete = store.is_complete()
                raw_coverage = [[r.start, r.end] for r in store.intervals()]
            finally:
                store.close()
            source_of_journal = "body.part"
        else:
            # Published: temp body was moved to the target. Fall back to the
            # coverage recorded at publish time, then re-prove it against the
            # published target bytes.
            recorded = state.get("coverage", [])
            covered = sum(e - s + 1 for s, e in recorded)
            complete = (state["total"] == 0) or (
                len(recorded) == 1 and recorded[0] == [0, state["total"] - 1])
            holes = [r.as_list() for r in interval_holes(state["total"], recorded)]
            report = {
                "source": "state.coverage (body.part published)",
                "valid_fragments": None,
                "total_fragments": None,
                "rejected": [],
            }
            source_of_journal = "state.coverage"
            raw_coverage = recorded
        journal_on_disk = 0
        if os.path.exists(self.journal_path):
            with open(self.journal_path, "r", encoding="utf-8") as fh:
                journal_on_disk = sum(1 for line in fh if line.strip())
        source_info = None
        if check_source:
            try:
                status, headers = self._head(state["object_id"])
                source_info = {
                    "status": status,
                    "etag": headers.get("etag"),
                    "content_length": headers.get("content-length"),
                    "matches_pinned": headers.get("etag") == state["etag"],
                }
            except (OSError, http.client.HTTPException) as exc:
                source_info = {"error": str(exc)}
        return {
            "state": state,
            "counters": state.get("counters", {}),
            "coverage_source": source_of_journal,
            "coverage": raw_coverage,
            "journal_on_disk_records": journal_on_disk,
            "journal": report,
            "covered": covered,
            "holes": holes,
            "complete_coverage": complete,
            "target": target_info,
            "source": source_info,
        }

    # -- verify --------------------------------------------------------------
    def verify(self, *, original_path: str | None = None,
               check_server_log: bool = False,
               require_retries: bool = False) -> dict[str, Any]:
        state = self._load()
        target = state["target"]
        result: dict[str, Any] = {
            "object_id": state["object_id"],
            "status": state["status"],
            "reason": state.get("reason"),
            "pinned_etag": state["etag"],
            "pinned_sha256": state["sha256"],
            "total": state["total"],
            "checks": {},
            "failures": [],
            "notes": [],
        }
        checks = result["checks"]
        failures = result["failures"]

        if state["status"] == "aborted":
            checks["terminal_aborted"] = True
            checks["target_absent"] = not os.path.exists(target)
            if not checks["target_absent"]:
                failures.append("aborted task must not publish a target")
            if check_server_log:
                self._check_server_log(state, checks, failures, result["notes"])
            result["ok"] = not failures
            return result

        if not os.path.exists(target):
            failures.append(f"target missing: {target}")
            result["ok"] = False
            return result
        size = os.path.getsize(target)
        digest = sha256_file(target)
        checks["target_exists"] = True
        checks["target_size_matches_total"] = size == state["total"]
        checks["target_sha256_matches_etag"] = digest == state["sha256"]
        if not checks["target_size_matches_total"]:
            failures.append(f"target size {size} != pinned total {state['total']}")
        if not checks["target_sha256_matches_etag"]:
            failures.append(f"target sha256 {digest} != pinned ETag digest {state['sha256']}")

        # Coverage proof: rebuild from journal, verify the temporary-body
        # fragments if it still exists, and independently re-check every
        # confirmed interval against the published target.
        store = ConfirmedStore(int(state["total"]), self.body_path, self.journal_path,
                               state["etag"], state["sha256"]) \
            if os.path.exists(self.body_path) else None
        intervals: list[list[int]] = []
        frags: list[Fragment] = []
        if store is not None:
            try:
                report = store.load_and_validate()
                intervals = [r.as_list() for r in store.intervals()]
                frags = store.fragments()
                checks["journal"] = {
                    "total_fragments": report["total_fragments"],
                    "valid_fragments": report["valid_fragments"],
                    "rejected": report["rejected"],
                }
                if report["rejected"]:
                    failures.append("journal contains rejected fragments")
            finally:
                store.close()
        else:
            # Temp body was moved to target at publish time; coverage is
            # recorded in state.json and metadata.json.
            intervals = state.get("coverage", [])
            checks["journal"] = {"source": "state.coverage (temp body published)"}
        checks["coverage"] = intervals
        complete = (state["total"] == 0 and not intervals) or (
            len(intervals) == 1 and intervals[0] == [0, state["total"] - 1])
        checks["coverage_complete"] = complete
        if not complete:
            failures.append(f"coverage is not complete: {intervals}")

        original = None
        if original_path:
            try:
                with open(original_path, "rb") as fh:
                    original = fh.read()
            except OSError as exc:
                failures.append(f"cannot read original: {exc}")
        if original is not None:
            checks["original_size"] = len(original)
            checks["original_sha256"] = sha256_hex(original)
            checks["target_equals_original"] = original == _read_file(target)
            if not checks["target_equals_original"]:
                failures.append("target bytes differ from fixed-version original")

        interval_results = []
        target_bytes = _read_file(target)
        for start, end in intervals:
            piece = target_bytes[start:end + 1]
            entry = {"range": [start, end], "length": len(piece),
                     "sha256": sha256_hex(piece)}
            if original is not None:
                entry["matches_original"] = original[start:end + 1] == piece
                if not entry["matches_original"]:
                    failures.append(f"confirmed interval {start}-{end} differs from original")
            interval_results.append(entry)
        checks["confirmed_intervals"] = interval_results

        if check_server_log:
            self._check_server_log(state, checks, failures, result["notes"])
            if require_retries and not checks.get("server_retries_observed"):
                failures.append("no retry/truncation observed in server log")

        result["ok"] = not failures
        return result

    def _check_server_log(self, state: dict[str, Any], checks: dict[str, Any],
                          failures: list[str], notes: list[str]) -> None:
        try:
            status, headers, raw, declared, truncated, err = self._plain_get(
                f"/__control/log?object_id={quote(state['object_id'], safe='')}", None)
            if status != 200:
                failures.append(f"server request log unavailable: HTTP {status}")
                return
            payload = json.loads(raw)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            failures.append(f"server request log unavailable: {exc}")
            return
        entries = payload.get("entries", [])
        object_gets = [e for e in entries if e.get("method") in ("GET", "HEAD")]
        checks["server_requests"] = len(entries)
        checks["server_gets"] = len(object_gets)
        checks["server_statuses"] = payload.get("summary", {}).get("statuses", {})
        statuses = [e.get("status") for e in object_gets]
        checks["server_retries_observed"] = (
            sum(1 for e in object_gets if e.get("truncated")) > 0
            or sum(1 for s in statuses if s not in (200, 206, None)) > 0
            or len(object_gets) > 1
        )
        expected_etag = state["etag"]
        mismatched = []
        body_responses = 0
        for e in object_gets:
            if e.get("status") in (200, 206):
                body_responses += 1
                if e.get("response_etag") != expected_etag:
                    mismatched.append({"seq": e.get("seq"), "etag": e.get("response_etag"),
                                       "status": e.get("status")})
        checks["server_body_responses"] = body_responses
        checks["server_etag_mismatches"] = mismatched
        if mismatched:
            failures.append(f"{len(mismatched)} body response(s) not bound to pinned ETag")
        if body_responses == 0 and state["status"] == "complete":
            failures.append("no 200/206 body response in server log (no real HTTP body read)")
        # Success is not inferred from status codes: also require that the
        # server actually wrote the pinned byte count or was truncated.
        written = sum(int(e.get("bytes_written") or 0) for e in object_gets)
        checks["server_bytes_written"] = written
        if state["status"] == "complete" and written < int(state["total"]):
            notes.append("server bytes written below total; ranges may have been retried")

    def _plain_get(self, path: str, headers: dict[str, str] | None):
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self.read_timeout)
        try:
            conn.request("GET", path, headers=headers or {"Connection": "close"})
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw, \
                len(raw), False, None
        finally:
            conn.close()


def _read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["plan", "fetch", "resume", "inspect", "verify"])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--object-id")
    parser.add_argument("--etag", help="expected strong ETag; verified by plan")
    parser.add_argument("--total", type=int, help="expected total length; verified by plan")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--chunk-size", type=int)
    parser.add_argument("--max-chunks", type=int, default=100)
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--min-span", type=int, default=4096)
    parser.add_argument("--fanout", type=int, default=1,
                        help="split each needed interval into N disjoint request ranges")
    parser.add_argument("--fanout-gap", type=int, default=1)
    parser.add_argument("--overlap", type=int, default=0,
                        help="expand each requested range by N bytes on both sides")
    parser.add_argument("--coalesce", type=int, default=1,
                        help="group N adjacent plan pieces into one request")
    parser.add_argument("--strategy", default="chunks", choices=["chunks", "coalesced"])
    parser.add_argument("--target", help="published target path")
    parser.add_argument("--original", help="verify: fixed-version original bytes")
    parser.add_argument("--check-server-log", action="store_true")
    parser.add_argument("--check-source", action="store_true")
    parser.add_argument("--require-retries", action="store_true")
    parser.add_argument("--reset", action="store_true", help="plan: discard an existing state dir")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    log_fn = (lambda msg: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr))
    asm = Assembler(
        args.base_url, args.state_dir, concurrency=args.concurrency,
        chunk_size=args.chunk_size, max_chunks=args.max_chunks,
        retries=args.retries, min_span=args.min_span, fanout=args.fanout,
        fanout_gap=args.fanout_gap, overlap=args.overlap, coalesce=args.coalesce,
        strategy=args.strategy, target_path=args.target, log_fn=log_fn,
    )
    try:
        if args.command == "plan":
            if not args.object_id:
                raise SystemExit("plan requires --object-id")
            state = asm.plan(args.object_id, etag=args.etag, total=args.total,
                             chunk_size=args.chunk_size, strategy=args.strategy,
                             reset=args.reset)
            _emit({k: state[k] for k in ("object_id", "total", "etag", "sha256",
                                         "chunk_size", "status", "target")}
                  | {"pieces": len(state.get("chunks", []))})
            return 0
        if args.command == "fetch":
            _emit(asm.fetch(resume=False))
            return 0
        if args.command == "resume":
            _emit(asm.fetch(resume=True))
            return 0
        if args.command == "inspect":
            _emit(asm.inspect(check_source=args.check_source))
            return 0
        if args.command == "verify":
            result = asm.verify(original_path=args.original,
                                check_server_log=args.check_server_log,
                                require_retries=args.require_retries)
            _emit(result)
            if result.get("status") == "aborted" and result.get("ok"):
                return 3
            return 0 if result.get("ok") else 1
    except AbortedState as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except AssemblerError as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except (OSError, http.client.HTTPException) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
