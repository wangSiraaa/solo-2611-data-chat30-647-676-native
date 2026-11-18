"""Scale benchmark: 10 000 documents x ~100 KiB, 50 queries, depth 20.

Verification strategy
---------------------
The index-free reference is computed in **one pass** over every document: each
payload is parsed once and all 50 paths are evaluated against it.  For each
query every match -- doc id, revision, hit paths and hit values -- is folded
into a SHA-256 digest; strict-mode errors are folded into a second digest.

The index-assisted search is streamed for the same query (one pass gives both
matches and errors) and folded the same way.  Comparing digest *and* count
verifies the complete result set, not just counts, while memory stays bounded.

The reference digests are cached in ``data/reference_digests.json``; they are
invalidated automatically when the document set or the query definitions
change (a content fingerprint is part of the cache key).

Usage::

    python3 -m benchmarks.run_benchmark [--db path] [--docs 10000] [--rebuild]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Iterable

from docrepo import Repository
from jsonpath_service.codec import path_to_json
from jsonpath_service.engine import PathError, evaluate

from .generate import documents, queries

DEFAULT_DB = os.path.join("data", "benchmark.db")
DEFAULT_REPORT = os.path.join("data", "benchmark_report.json")
DEFAULT_CACHE = os.path.join("data", "reference_digests.json")


# ---------------------------------------------------------------------------
# Fast, structure-aware digest encoding
# ---------------------------------------------------------------------------


def _encode(value: Any, out: list[str]) -> None:
    """Append a canonical, type-tagged encoding of a JSON value."""

    if value is None:
        out.append("z")
    elif value is True:
        out.append("t")
    elif value is False:
        out.append("f")
    elif isinstance(value, int):
        out.append("i")
        out.append(str(value))
        out.append(";")
    elif isinstance(value, str):
        out.append("s")
        out.append(str(len(value)))
        out.append(":")
        out.append(value)
    elif isinstance(value, list):
        out.append("l")
        out.append(str(len(value)))
        out.append("[")
        for item in value:
            _encode(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("d")
        out.append(str(len(value)))
        out.append("{")
        for key in sorted(value):
            out.append(str(len(key)))
            out.append(":")
            out.append(key)
            _encode(value[key], out)
        out.append("}")
    else:  # pragma: no cover - JSON documents only
        raise TypeError(f"unexpected JSON value {type(value)!r}")


class Digest:
    """Incremental digest of (doc_id, revision, hit paths, hit values)."""

    __slots__ = ("hasher", "count", "_buf", "_size")

    def __init__(self) -> None:
        self.hasher = hashlib.sha256()
        self.count = 0
        self._buf: list[str] = []
        self._size = 0

    def _flush(self) -> None:
        if self._buf:
            self.hasher.update("".join(self._buf).encode("utf-8", "surrogatepass"))
            self._buf.clear()
            self._size = 0

    def add(self, doc_id: str, revision: int, hits: Iterable[Any]) -> None:
        buf = self._buf
        _encode(doc_id, buf)
        _encode(revision, buf)
        for hit in hits:
            _encode(list(hit.path), buf)
            _encode(hit.value, buf)
        buf.append("|")
        self.count += 1
        self._size += 1
        if self._size >= 512:
            self._flush()

    def add_error(self, doc_id: str, message: str) -> None:
        buf = self._buf
        buf.append("E")
        _encode(doc_id, buf)
        _encode(message, buf)
        self.count += 1
        self._size += 1
        if self._size >= 512:
            self._flush()

    def hexdigest(self) -> str:
        self._flush()
        return self.hasher.hexdigest()

    def state(self) -> dict[str, Any]:
        return {"digest": self.hexdigest(), "count": self.count}


# ---------------------------------------------------------------------------
# Reference
# ---------------------------------------------------------------------------


def _fingerprint(db_path: str, specs: dict[str, tuple[list[Any], str]]) -> str:
    """Content fingerprint of the stored corpus and query definitions.

    Hashes every document id, revision and payload plus the query paths and
    modes.  A different corpus or query set invalidates the cached reference.
    """

    hasher = hashlib.sha256()
    with Repository(db_path) as repo:
        for doc_id, revision, raw in repo.iter_raw_documents():
            hasher.update(repr((doc_id, revision)).encode("utf-8", "surrogatepass"))
            hasher.update(raw.encode("utf-8", "surrogatepass"))
            hasher.update(b"\x1e")
    hasher.update(b"\xff")
    for name in sorted(specs):
        path, mode = specs[name]
        hasher.update(name.encode("utf-8", "surrogatepass"))
        hasher.update(mode.encode())
        encoded: list[str] = []
        _encode(path_to_json(path), encoded)
        hasher.update("".join(encoded).encode("utf-8", "surrogatepass"))
        hasher.update(b"\xfe")
    return hasher.hexdigest()


def reference_pass(
    db_path: str, specs: dict[str, tuple[list[Any], str]]
) -> tuple[dict[str, Digest], dict[str, Digest], float]:
    match_digests = {name: Digest() for name in specs}
    error_digests = {name: Digest() for name in specs}
    started = time.perf_counter()
    seen = 0
    with Repository(db_path) as repo:
        for doc_id, revision, raw in repo.iter_raw_documents():
            payload = json.loads(raw)
            raw = None  # release the string before evaluating
            for name, (path, mode) in specs.items():
                try:
                    hits = evaluate(payload, path, strict=(mode == "strict"))
                except PathError as exc:
                    error_digests[name].add_error(doc_id, str(exc))
                    continue
                if hits:
                    match_digests[name].add(doc_id, revision, hits)
            seen += 1
            if seen % 2000 == 0:
                print(
                    f"  reference pass: {seen} documents "
                    f"({time.perf_counter() - started:.1f}s)",
                    flush=True,
                )
    return match_digests, error_digests, time.perf_counter() - started


def load_reference(
    db_path: str,
    specs: dict[str, tuple[list[Any], str]],
    cache_path: str | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], float, str]:
    """Return match/error reference digests, preferring a valid cache."""

    fingerprint = _fingerprint(db_path, specs)
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("fingerprint") == fingerprint:
                print("  using cached reference digests", flush=True)
                return (
                    cached["matches"],
                    cached["errors"],
                    float(cached.get("seconds", 0.0)),
                    fingerprint,
                )
        except (OSError, ValueError, KeyError):
            pass

    print("  computing reference digests (index-free, one pass) ...", flush=True)
    match_digests, error_digests, seconds = reference_pass(db_path, specs)
    matches = {name: d.state() for name, d in match_digests.items()}
    errors = {name: d.state() for name, d in error_digests.items()}
    if cache_path:
        os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "fingerprint": fingerprint,
                    "matches": matches,
                    "errors": errors,
                    "seconds": seconds,
                },
                fh,
            )
    return matches, errors, seconds, fingerprint


# ---------------------------------------------------------------------------
# Build and run
# ---------------------------------------------------------------------------


def build(db_path: str, count: int, *, force: bool = False) -> None:
    if force:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except FileNotFoundError:
                pass
    with Repository(db_path) as repo:
        if repo.count() != count:
            print(f"loading {count} documents ...", flush=True)
            started = time.time()
            loaded = repo.bulk_load(documents(count), chunk_size=100)
            print(
                f"loaded {loaded} documents in {time.time() - started:.1f}s "
                f"({os.path.getsize(db_path) / 1e6:.1f} MB)",
                flush=True,
            )
        print("registering 50 queries ...", flush=True)
        for name, (path, mode) in queries().items():
            repo.register_query(name, path, mode=mode)
        started = time.time()
        scanned = repo.ensure_indexes()
        print(
            f"index build over {scanned} documents in {time.time() - started:.1f}s",
            flush=True,
        )


def run(db_path: str, cache_path: str | None = DEFAULT_CACHE, *, jobs: int = 1) -> dict[str, Any]:
    specs = queries()
    with Repository(db_path) as repo:
        total = repo.count()

    reference_matches, reference_errors, reference_seconds, fingerprint = load_reference(
        db_path, specs, cache_path
    )

    indexed_by_name = _indexed_digests(db_path, specs, jobs=jobs)
    plans = _candidate_plans(db_path, specs)

    rows: list[dict[str, Any]] = []
    for name, (_path, mode) in specs.items():
        got, got_errors, indexed_ms = indexed_by_name[name]
        expected = reference_matches[name]
        expected_errors = reference_errors[name]
        plan = plans[name]
        matches_ok = got == expected
        errors_ok = got_errors == expected_errors
        rows.append(
            {
                "query": name,
                "mode": mode,
                "matches": got["count"],
                "reference_matches": expected["count"],
                "errors": got_errors["count"],
                "reference_errors": expected_errors["count"],
                "candidates": plan["candidates"],
                "used_index": plan["used_index"],
                "indexed_ms": round(indexed_ms, 3),
                "exact_payload_digest_match": matches_ok,
                "error_digest_match": errors_ok,
                "evidence_ok": matches_ok and errors_ok,
            }
        )

    return {
        "documents": total,
        "queries": len(rows),
        "all_match": all(r["evidence_ok"] for r in rows),
        "reference_fingerprint": fingerprint[:16] if fingerprint else None,
        "reference_seconds": round(reference_seconds, 1),
        "indexed_total_ms": round(sum(r["indexed_ms"] for r in rows), 1),
        "results": rows,
    }


# ---------------------------------------------------------------------------
# Query worker (also usable through a process pool)
# ---------------------------------------------------------------------------


def _indexed_digest(
    db_path: str, name: str
) -> tuple[dict[str, Any], dict[str, Any], float]:
    """Run one indexed search and fold matches and errors into digests."""

    started = time.perf_counter()
    match_digest = Digest()
    error_digest = Digest()
    with Repository(db_path) as repo:
        for kind, payload in repo.iter_search_events(name):
            if kind == "match":
                match_digest.add(payload.doc_id, payload.revision, payload.hits)
            else:
                doc_id, message = payload
                error_digest.add_error(doc_id, message)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return match_digest.state(), error_digest.state(), elapsed_ms


def _worker(arg: tuple[str, str]) -> tuple[str, tuple[dict[str, Any], dict[str, Any], float]]:
    db_path, name = arg
    return name, _indexed_digest(db_path, name)


def _indexed_digests(
    db_path: str, specs: dict[str, tuple[list[Any], str]], *, jobs: int
) -> dict[str, tuple[dict[str, Any], dict[str, Any], float]]:
    names = list(specs)
    if jobs <= 1 or len(names) <= 1:
        return {name: _indexed_digest(db_path, name) for name in names}

    from concurrent.futures import ProcessPoolExecutor

    results: dict[str, tuple[dict[str, Any], dict[str, Any], float]] = {}
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for name, outcome in pool.map(_worker, [(db_path, n) for n in names]):
            results[name] = outcome
    return results


def _candidate_plans(
    db_path: str, specs: dict[str, tuple[list[Any], str]]
) -> dict[str, dict[str, Any]]:
    plans: dict[str, dict[str, Any]] = {}
    with Repository(db_path) as repo:
        for name in specs:
            query = repo.get_query(name)
            with repo._read_tx() as conn:  # read-only snapshot
                candidates, _plan, _reason = repo._candidate_set(conn, query)
                total = int(
                    conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                )
            plans[name] = {
                "candidates": total if candidates is None else len(candidates),
                "used_index": candidates is not None,
            }
    return plans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="10k x 100KiB x 50 queries benchmark")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--docs", type=int, default=10000)
    parser.add_argument("--rebuild", action="store_true", help="delete and reload the database")
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--jobs", type=int, default=1, help="parallel query workers")
    args = parser.parse_args(argv)

    started = time.time()
    build(args.db, args.docs, force=args.rebuild)
    report = run(
        args.db,
        cache_path=None if args.no_cache else args.cache,
        jobs=args.jobs,
    )
    report["wall_seconds"] = round(time.time() - started, 1)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if report["all_match"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
