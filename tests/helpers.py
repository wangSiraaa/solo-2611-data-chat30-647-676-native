"""Shared helpers for the test-suite (unittest based, no third-party deps)."""

from __future__ import annotations

import json
import os
import random
import tempfile
from contextlib import contextmanager
from typing import Any, Iterator



@contextmanager
def temp_db() -> Iterator[str]:
    fd, path = tempfile.mkstemp(prefix="docrepo-test-", suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        yield path
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass


def make_document(seed: int, items: int = 8) -> dict[str, Any]:
    rng = random.Random(seed)
    return {
        "tenant": f"t{seed % 5}",
        "status": rng.choice(["active", "paused", "closed"]),
        "priority": rng.randint(0, 9),
        "nullable": None if seed % 3 == 0 else seed,
        "empty": [],
        "tags": [f"tag{rng.randint(0, 4)}" for _ in range(rng.randint(0, 4))],
        "customer": {
            "name": f"customer-{seed}",
            "segment": rng.choice(["s", "m", "l"]),
            "profile": {"region": rng.choice(["e", "w"]), "tier": rng.randint(1, 3)},
        },
        "items": [
            {
                "id": i,
                "kind": rng.choice(["alpha", "beta", "gamma"]),
                "score": rng.randint(0, 20),
                "note": None if (seed + i) % 7 == 0 else f"n{seed}-{i}",
                "flags": [rng.choice([True, False]) for _ in range(2)],
            }
            for i in range(items)
        ],
        "nested": [
            {"k": "x", "v": rng.randint(0, 3), "children": [{"c": rng.randint(0, 2)}]},
            {"k": "y", "v": rng.randint(0, 3), "children": []},
        ],
    }


def hit_paths(hits: Any) -> list[tuple[Any, ...]]:
    return [h.path for h in hits]


def search_signature(results: Any) -> list[tuple[str, int, tuple[tuple[Any, ...], ...]]]:
    return [(r.doc_id, r.revision, tuple(h.path for h in r.hits)) for r in results]


def result_payload(results: Any) -> list[dict[str, Any]]:
    return [
        {"doc_id": r.doc_id, "revision": r.revision, "hits": r.evidence()} for r in results
    ]


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
