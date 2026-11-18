"""Local immutable-version object source with real HTTP byte-range support.

Run standalone:

    python src/object_source.py --port 0 --object-id demo --body-file original.bin

Programmatic use (tests/demo) keeps the store, fault injector and request log
in-process so fault injection is deterministic:

    srv, thread, base_url = serve_in_thread(store)
    ...
    srv.shutdown()

Protocol notes
--------------
* Objects have a stable ``object_id`` and an immutable *current* version.
* ``ETag`` is the strong validator ``"<sha256 hex of this version's body>"``.
* ``HEAD`` returns total length + ETag, never 206.
* ``GET`` without Range returns 200. With Range it returns either 206 +
  exact ``Content-Range`` or 206 ``multipart/byteranges``. Overlapping or
  adjacent requested ranges are merged, so a client must never assume that
  one request maps to one response part.
* Unknown object -> 404, failed If-Match -> 412, no satisfiable range -> 416
  (``Content-Range: bytes */total``), malformed range -> 400.
* A response snapshots one version before its body is streamed; an object
  update during the response cannot change that response.
* Fault injection: drop mid-body (connection truncation), shuffle multipart
  parts, gate (hold) requests, throttle chunks, and update versions via the
  control API.
* Every request is recorded in an in-memory request log queryable over HTTP.
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from rangeutil import (
    ByteRange,
    RangeHeaderError,
    RangeNotSatisfiable,
    format_content_range,
    merge_ranges,
    parse_range_header,
    quote_etag,
    sha256_hex,
    strong_etag_digest,
)


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Version:
    object_id: str
    number: int
    body: bytes
    digest: str

    @property
    def etag(self) -> str:
        return quote_etag(self.digest)

    @property
    def length(self) -> int:
        return len(self.body)

    def metadata(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "version": self.number,
            "length": self.length,
            "etag": self.etag,
            "sha256": self.digest,
        }


class ObjectStore:
    """Thread-safe object store. Only the current version is readable."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._versions: dict[str, list[Version]] = {}
        self._current: dict[str, int] = {}

    def create(self, object_id: str, body: bytes) -> Version:
        if not object_id or "/" in object_id:
            raise ValueError("object_id must be a non-empty path segment")
        with self._lock:
            if object_id in self._current:
                raise KeyError(f"object already exists: {object_id}")
            version = Version(object_id, 1, bytes(body), sha256_hex(body))
            self._versions[object_id] = [version]
            self._current[object_id] = 0
            return version

    def update(self, object_id: str, body: bytes) -> Version:
        """Install a new current version; the old one becomes unreadable."""
        with self._lock:
            if object_id not in self._versions:
                raise KeyError(f"unknown object: {object_id}")
            number = self._versions[object_id][-1].number + 1
            version = Version(object_id, number, bytes(body), sha256_hex(body))
            self._versions[object_id].append(version)
            self._current[object_id] = len(self._versions[object_id]) - 1
            return version

    def current(self, object_id: str) -> Version | None:
        with self._lock:
            idx = self._current.get(object_id)
            if idx is None:
                return None
            return self._versions[object_id][idx]

    def current_or_412(self, object_id: str, if_match: str | None) -> tuple[Version | None, bool]:
        """Return ``(version, if_match_ok)``.

        Absent object -> ``(None, True)``; present but If-Match failure ->
        ``(current, False)``. Old versions are never resolved, which makes a
        replaced version unreadable for in-flight assemblers.
        """
        with self._lock:
            version = self.current(object_id)
            if version is None:
                return None, True
            if if_match is None:
                return version, True
            tokens = [t.strip() for t in if_match.split(",") if t.strip()]
            if any(t == "*" for t in tokens):
                return version, True
            for token in tokens:
                digest = strong_etag_digest(token)
                if digest is not None and digest == version.digest:
                    return version, True
            return version, False

    def list_objects(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for oid in sorted(self._versions):
                cur = self.current(oid)
                assert cur is not None
                out.append(cur.metadata())
            return out


# --------------------------------------------------------------------------
# Fault injection
# --------------------------------------------------------------------------


@dataclass
class DropRule:
    remaining: int
    after_bytes: int
    object_id: str | None = None
    etag_digest: str | None = None


class Faults:
    """Deterministic, thread-safe fault injection knobs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reorder_parts = False
        self.chunk_size = 64 * 1024
        self.chunk_delay = 0.0
        self._drops: list[DropRule] = []
        self._holds: dict[str, threading.Event] = {}
        self.gate_waiters = 0

    # -- mid-body disconnect -------------------------------------------------
    def drop_next(self, after_bytes: int, *, object_id: str | None = None,
                  etag: str | None = None, count: int = 1) -> None:
        digest = strong_etag_digest(etag) if etag else None
        with self._lock:
            self._drops.append(DropRule(count, after_bytes, object_id, digest))

    def take_drop(self, object_id: str, etag_digest: str) -> int | None:
        with self._lock:
            for rule in self._drops:
                if rule.remaining <= 0:
                    continue
                if rule.object_id is not None and rule.object_id != object_id:
                    continue
                if rule.etag_digest is not None and rule.etag_digest != etag_digest:
                    continue
                rule.remaining -= 1
                return rule.after_bytes
        return None

    # -- gate / version swap -------------------------------------------------
    def hold(self, etag_or_digest: str) -> threading.Event:
        digest = strong_etag_digest(etag_or_digest)
        key = digest if digest is not None else etag_or_digest.strip('"')
        with self._lock:
            event = self._holds.get(key)
            if event is None:
                event = threading.Event()
                self._holds[key] = event
            return event

    def release(self, etag_or_digest: str) -> None:
        digest = strong_etag_digest(etag_or_digest)
        key = digest if digest is not None else etag_or_digest.strip('"')
        with self._lock:
            event = self._holds.get(key)
        if event is not None:
            event.set()

    def gate(self, etag_digest: str | None, timeout: float = 30.0) -> None:
        if etag_digest is None:
            return
        with self._lock:
            event = self._holds.get(etag_digest)
            if event is None or event.is_set():
                return
            self.gate_waiters += 1
        try:
            event.wait(timeout)
        finally:
            with self._lock:
                self.gate_waiters -= 1

    # -- misc ----------------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self.reorder_parts = False
            self.chunk_size = 64 * 1024
            self.chunk_delay = 0.0
            self._drops.clear()
            for event in self._holds.values():
                event.set()
            self._holds.clear()


# --------------------------------------------------------------------------
# Request log
# --------------------------------------------------------------------------


class RequestLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        self._seq = 0

    def begin(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": time.time(),
                "method": fields.get("method"),
                "path": fields.get("path"),
                "object_id": fields.get("object_id"),
                "request_etag": fields.get("request_etag"),
                "requested_range": fields.get("requested_range"),
                "mode": fields.get("mode", "none"),
                "status": None,
                "response_etag": None,
                "version": None,
                "served_ranges": [],
                "parts": [],
                "declared_length": None,
                "bytes_written": 0,
                "truncated": False,
                "error": None,
                "duration_ms": None,
            }
            self._entries.append(entry)
            return entry

    def finish(self, entry: dict[str, Any], **fields: Any) -> None:
        with self._lock:
            entry.update(fields)
            entry["duration_ms"] = round((time.time() - entry["ts"]) * 1000.0, 3)

    def entries(self, object_id: str | None = None, since_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            out = [
                dict(e) for e in self._entries
                if e["seq"] > since_seq
                and (object_id is None or e.get("object_id") == object_id)
            ]
        return out

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def summary(self, object_id: str | None = None) -> dict[str, Any]:
        entries = self.entries(object_id)
        statuses: dict[str, int] = {}
        for e in entries:
            key = str(e["status"])
            statuses[key] = statuses.get(key, 0) + 1
        return {
            "requests": len(entries),
            "statuses": statuses,
            "truncated": sum(1 for e in entries if e["truncated"]),
            "etag_mismatches": sum(
                1 for e in entries
                if e.get("request_etag") and e.get("response_etag")
                and e["request_etag"] != e["response_etag"]
            ),
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _parse_boundary(content_type: str | None) -> str | None:
    if not content_type:
        return None
    parts = [p.strip() for p in content_type.split(";")]
    if parts[0].lower() != "multipart/byteranges":
        return None
    for p in parts[1:]:
        if p.lower().startswith("boundary="):
            value = p.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            return value
    return None


class ObjectHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], store: ObjectStore,
                 log: RequestLog | None = None, faults: Faults | None = None,
                 verbose: bool = False):
        super().__init__(addr, ObjectHandler)
        self.store = store
        self.log = log or RequestLog()
        self.faults = faults or Faults()
        self.verbose = verbose
        self.bound_port = self.server_address[1]


class ObjectHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalObjectSource/1.0"

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    @property
    def store(self) -> ObjectStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def request_log(self) -> RequestLog:
        return self.server.log  # type: ignore[attr-defined]

    @property
    def faults(self) -> Faults:
        return self.server.faults  # type: ignore[attr-defined]

    def _route(self) -> tuple[str, str]:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path.startswith("/objects/"):
            return "object", unquote(path[len("/objects/"):])
        if path.startswith("/__control/"):
            return "control", path[len("/__control/"):]
        if path == "/__health":
            return "health", ""
        return "notfound", path

    def _send_json(self, status: int, payload: Any, *, head: bool = False,
                   extra_headers: dict[str, str] | None = None) -> int:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.close_connection = True
        if not head:
            try:
                self.wfile.write(body)
                return len(body)
            except (BrokenPipeError, ConnectionResetError):
                return 0
        return 0

    # -- HEAD ----------------------------------------------------------------
    def do_HEAD(self) -> None:
        kind, ident = self._route()
        if kind == "health":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        if kind != "object":
            self._send_json(404, {"error": "not_found", "path": self.path}, head=True)
            return

        entry = self.request_log.begin(
            method="HEAD", path=self.path, object_id=ident,
            request_etag=self.headers.get("If-Match"), mode="head",
        )
        version, match_ok = self.store.current_or_412(ident, self.headers.get("If-Match"))
        if version is None:
            self.request_log.finish(entry, status=404, error="not_found")
            self._send_json(404, {"error": "not_found", "object_id": ident}, head=True)
            return
        if not match_ok:
            self.request_log.finish(entry, status=412, response_etag=version.etag,
                                    version=version.number, error="if_match_failed")
            self._send_json(412, {"error": "if_match_failed", "object_id": ident,
                                  "etag": version.etag}, head=True)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(version.length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", version.etag)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.request_log.finish(
            entry, status=200, response_etag=version.etag, version=version.number,
            declared_length=version.length,
        )

    # -- GET -----------------------------------------------------------------
    def do_GET(self) -> None:
        kind, ident = self._route()
        if kind == "health":
            self._send_json(200, {"ok": True})
            return
        if kind == "control":
            self._handle_control_get(ident)
            return
        if kind != "object":
            self._send_json(404, {"error": "not_found", "path": self.path})
            return
        self._serve_object(ident)

    def _serve_object(self, object_id: str) -> None:
        if_match = self.headers.get("If-Match")
        range_header = self.headers.get("Range")
        entry = self.request_log.begin(
            method="GET", path=self.path, object_id=object_id,
            request_etag=if_match, requested_range=range_header,
        )

        # Fault gate: hold this request before it resolves a version. If the
        # object is updated while held, an old If-Match will fail with 412.
        pre = self.store.current(object_id)
        pre_digest = strong_etag_digest(if_match) if if_match else None
        if pre_digest is None and pre is not None:
            pre_digest = pre.digest
        self.faults.gate(pre_digest)

        version, match_ok = self.store.current_or_412(object_id, if_match)
        if version is None:
            self.request_log.finish(entry, status=404, error="not_found")
            self._send_json(404, {"error": "not_found", "object_id": object_id})
            return
        if not match_ok:
            self.request_log.finish(entry, status=412, response_etag=version.etag,
                                    version=version.number, error="if_match_failed")
            self._send_json(412, {"error": "if_match_failed", "object_id": object_id,
                                  "etag": version.etag},
                            extra_headers={"ETag": version.etag})
            return

        entry["response_etag"] = version.etag
        entry["version"] = version.number
        total = version.length

        if range_header is None:
            self._send_object_response(
                entry, version, total, 200, None,
                [(ByteRange(0, total - 1), version.body)] if total else [],
                body=version.body,
            )
            return

        try:
            wanted = parse_range_header(range_header, total)
        except RangeNotSatisfiable:
            self.request_log.finish(
                entry, status=416, error="range_not_satisfiable",
                declared_length=0,
            )
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.send_header("ETag", version.etag)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        except RangeHeaderError as exc:
            self.request_log.finish(entry, status=400, error=f"bad_range: {exc}")
            self._send_json(400, {"error": "bad_range", "detail": str(exc)},
                            extra_headers={"ETag": version.etag})
            return

        served = merge_ranges(wanted)
        if len(served) == 1:
            r = served[0]
            payload = version.body[r.start:r.end + 1]
            self._send_object_response(
                entry, version, total, 206, format_content_range(r, total),
                [(r, payload)], body=payload,
            )
        else:
            self._send_multipart(entry, version, total, served)

    def _send_object_response(self, entry: dict[str, Any], version: Version,
                              total: int, status: int, content_range: str | None,
                              parts: list[tuple[ByteRange, bytes]], *,
                              body: bytes) -> None:
        entry["mode"] = "single" if status == 206 else "full"
        entry["served_ranges"] = [r.as_list() for r, _ in parts]
        entry["parts"] = [r.as_list() for r, _ in parts]
        entry["declared_length"] = len(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", version.etag)
        if content_range is not None:
            self.send_header("Content-Range", content_range)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._stream_payload(entry, version, body, drop_after=self._drop_limit(version))

    def _send_multipart(self, entry: dict[str, Any], version: Version, total: int,
                        served: list[ByteRange]) -> None:
        boundary = "BOUNDARY-" + uuid.uuid4().hex + uuid.uuid4().hex
        chunks: list[tuple[ByteRange, bytes, bytes]] = []
        for r in served:
            header = (
                f"\r\n--{boundary}\r\n"
                "Content-Type: application/octet-stream\r\n"
                f"Content-Range: {format_content_range(r, total)}\r\n"
                "\r\n"
            ).encode("ascii")
            chunks.append((r, header, version.body[r.start:r.end + 1]))
        closing = f"\r\n--{boundary}--\r\n".encode("ascii")

        if self.faults.reorder_parts and len(chunks) > 1:
            # Reproducible shuffle: parts are allowed in any order.
            random.Random(0xC0FFEE).shuffle(chunks)

        payload = b"".join(h + d for _, h, d in chunks) + closing
        ctype = f'multipart/byteranges; boundary="{boundary}"'
        entry["mode"] = "multipart"
        entry["served_ranges"] = [r.as_list() for r in served]
        entry["parts"] = [r.as_list() for r, _, _ in chunks]
        entry["declared_length"] = len(payload)
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", version.etag)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._stream_payload(entry, version, payload, drop_after=self._drop_limit(version))

    def _drop_limit(self, version: Version) -> int | None:
        return self.faults.take_drop(version.object_id, version.digest)

    def _stream_payload(self, entry: dict[str, Any], version: Version,
                        payload: bytes, *, drop_after: int | None) -> None:
        chunk_size = max(1, self.faults.chunk_size)
        delay = self.faults.chunk_delay
        written = 0
        remaining_drop = drop_after
        truncated = False
        error = None
        try:
            offset = 0
            while offset < len(payload):
                take = min(chunk_size, len(payload) - offset)
                if remaining_drop is not None and remaining_drop < take:
                    take = remaining_drop
                    truncated = True
                if take <= 0:
                    truncated = True
                    break
                self.wfile.write(payload[offset:offset + take])
                written += take
                offset += take
                if remaining_drop is not None:
                    remaining_drop -= take
                    if remaining_drop <= 0 and offset < len(payload):
                        truncated = True
                        break
                if delay:
                    time.sleep(delay)
            try:
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                truncated = True
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            truncated = True
            error = f"client_disconnect: {exc}"
        finally:
            if truncated:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.connection.close()
                except OSError:
                    pass
            self.request_log.finish(
                entry,
                status=entry.get("status") or (206 if entry.get("mode") in ("single", "multipart") else 200),
                bytes_written=written,
                truncated=truncated,
                error=error,
            )

    # -- control API ---------------------------------------------------------
    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def do_POST(self) -> None:
        kind, ident = self._route()
        if kind != "control":
            self._send_json(404, {"error": "not_found", "path": self.path})
            return
        try:
            payload = json.loads(self._read_body() or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": "bad_json", "detail": str(exc)})
            return

        if ident == "update":
            self._control_update(payload)
        elif ident == "faults":
            self._control_faults(payload)
        else:
            self._send_json(404, {"error": "unknown_control", "action": ident})

    def _control_update(self, payload: dict[str, Any]) -> None:
        object_id = payload.get("object_id")
        if not object_id:
            self._send_json(400, {"error": "missing_object_id"})
            return
        try:
            if "body_hex" in payload:
                body = bytes.fromhex(payload["body_hex"])
            elif "body_text" in payload:
                body = payload["body_text"].encode("utf-8")
            else:
                raise ValueError("provide body_hex or body_text")
        except (ValueError, TypeError) as exc:
            self._send_json(400, {"error": "bad_body", "detail": str(exc)})
            return
        try:
            version = self.store.update(object_id, body)
        except KeyError:
            self._send_json(404, {"error": "not_found", "object_id": object_id})
            return
        self._send_json(200, version.metadata())

    def _control_faults(self, payload: dict[str, Any]) -> None:
        action = payload.get("action")
        if action == "drop_next":
            self.faults.drop_next(
                int(payload["after_bytes"]),
                object_id=payload.get("object_id"),
                etag=payload.get("etag"),
                count=int(payload.get("count", 1)),
            )
        elif action == "reorder_parts":
            self.faults.reorder_parts = bool(payload.get("value", True))
        elif action == "chunk":
            if "size" in payload:
                self.faults.chunk_size = max(1, int(payload["size"]))
            if "delay" in payload:
                self.faults.chunk_delay = max(0.0, float(payload["delay"]))
        elif action == "hold":
            self.faults.hold(payload["etag"])
        elif action == "release":
            self.faults.release(payload["etag"])
        elif action == "reset":
            self.faults.reset()
        else:
            self._send_json(400, {"error": "unknown_fault_action", "action": action})
            return
        self._send_json(200, {"ok": True, "action": action})

    def do_DELETE(self) -> None:
        kind, ident = self._route()
        if kind == "control" and ident == "log":
            self.request_log.clear()
            self._send_json(200, {"ok": True, "cleared": True})
            return
        self._send_json(404, {"error": "not_found", "path": self.path})

    def _handle_control_get(self, ident: str) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if ident == "log":
            object_id = query.get("object_id", [None])[0]
            since = int(query.get("since_seq", ["0"])[0])
            self._send_json(200, {
                "entries": self.request_log.entries(object_id, since),
                "summary": self.request_log.summary(object_id),
            })
        elif ident == "objects":
            self._send_json(200, {"objects": self.store.list_objects()})
        elif ident == "summary":
            object_id = query.get("object_id", [None])[0]
            self._send_json(200, self.request_log.summary(object_id))
        else:
            self._send_json(404, {"error": "unknown_control", "action": ident})


# --------------------------------------------------------------------------
# Programmatic / CLI entrypoints
# --------------------------------------------------------------------------


def make_server(store: ObjectStore, host: str = "127.0.0.1", port: int = 0,
                log: RequestLog | None = None, faults: Faults | None = None,
                verbose: bool = False) -> ObjectHTTPServer:
    return ObjectHTTPServer((host, port), store, log=log, faults=faults, verbose=verbose)


def serve_in_thread(store: ObjectStore, host: str = "127.0.0.1", port: int = 0,
                    verbose: bool = False) -> tuple[ObjectHTTPServer, threading.Thread, str]:
    server = make_server(store, host, port, verbose=verbose)
    thread = threading.Thread(target=server.serve_forever, name="object-source", daemon=True)
    thread.start()
    base_url = f"http://{host}:{server.bound_port}"
    return server, thread, base_url


def object_url(base_url: str, object_id: str) -> str:
    return f"{base_url}/objects/{quote(object_id, safe='')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local immutable-version object source")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--object-id", default="demo")
    parser.add_argument("--body-file", help="initial object body")
    parser.add_argument("--body-text", help="initial object body as UTF-8 text")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.body_file:
        with open(args.body_file, "rb") as fh:
            body = fh.read()
    elif args.body_text is not None:
        body = args.body_text.encode("utf-8")
    else:
        body = b""
    store = ObjectStore()
    version = store.create(args.object_id, body)
    server, _, base_url = serve_in_thread(store, args.host, args.port, verbose=args.verbose)
    print(json.dumps({
        "base_url": base_url,
        "object_url": object_url(base_url, args.object_id),
        "object_id": args.object_id,
        "length": version.length,
        "etag": version.etag,
    }, indent=2), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
