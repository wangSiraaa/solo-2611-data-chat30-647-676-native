"""Deterministic synthetic documents and 50 registered query definitions.

The document layout is fixed by this module so that the scale benchmark, the
demo and the tests all exercise the same shapes:

* roughly 100 KiB of JSON per document;
* top-level ``l01`` .. ``l20`` chain for exactly depth-20 paths;
* ``items`` array (about 600 elements) with repeated values, missing keys,
  explicit nulls, booleans and nested labels;
* ``matrix`` nested arrays, small object wildcards, tags, empty arrays and
  missing fields;
* some documents deliberately missing fields so strict queries have locatable
  errors and lax queries have partial hits.
"""

from __future__ import annotations

import random
from typing import Any, Iterator

from jsonpath_service.engine import (
    ArrayWildcard,
    Filter,
    Index,
    Key,
    ObjectWildcard,
    and_,
    eq,
    exists,
    not_,
    or_,
)

KINDS = ("alpha", "beta", "gamma", "delta", "epsilon")
SEGMENTS = ("s", "m", "l")
REGIONS = ("e", "w")
STATUSES = ("active", "paused", "closed")
ITEM_COUNT = 600


def document(seed: int) -> dict[str, Any]:
    """Return a deterministic document of roughly 100 KiB."""

    rng = random.Random(seed)
    items: list[dict[str, Any]] = []
    for j in range(ITEM_COUNT):
        item: dict[str, Any] = {
            "id": j,
            "kind": KINDS[(seed + j) % len(KINDS)],
            # A document-wide cohort value: only some documents match it, so
            # equality queries on it are selective.
            "batch": seed % 50,
            "grade": (seed * 7 + j * 3) % 10,
            "flag": bool((seed + j) % 3),
            "note": f"item-{seed}-{j}-" + "x" * 30,
            "metrics": [(seed + j) % 5, (seed + 2 * j) % 7],
            "labels": {"a": (seed + j) % 11, "b": f"b{(seed * 3 + j) % 13}"},
            "maybe": None if (seed + j) % 9 == 0 else (seed + j) % 4,
        }
        # Some documents have elements missing ``kind`` so strict paths fail
        # in a locatable way while lax paths keep the other elements.
        if seed % 17 == 0 and j % 97 == 5:
            del item["kind"]
        if (seed + j) % 23 == 0:
            del item["labels"]
        items.append(item)

    deep_chain: dict[str, Any] = {"l20": (seed % 4)}
    for level in range(19, 0, -1):
        deep_chain = {f"l{level:02d}": deep_chain}

    doc: dict[str, Any] = {
        "tenant": f"t{seed % 50}",
        "status": STATUSES[seed % len(STATUSES)],
        "priority": seed % 10,
        "region": REGIONS[seed % 2],
        "nullable": None if seed % 4 == 0 else seed,
        "empty": [],
        "tags": [f"tag{(seed * 5 + k) % 400}" for k in range(12)],
        "customer": {
            "name": f"customer-{seed}-" + "c" * 40,
            "segment": SEGMENTS[seed % len(SEGMENTS)],
            "profile": {"region": REGIONS[(seed // 2) % 2], "tier": seed % 6 + 1},
        },
        "items": items,
        "matrix": [[(seed + r + c) % 6 for c in range(4)] for r in range(5)],
        "labels": {"b": f"top{seed % 9}", "a": seed % 11, "Z": seed % 3},
        **deep_chain,
    }
    if seed % 13 != 0:
        doc["sparse"] = {"present": seed % 5}
    if seed % 19 == 0:
        doc["customer"]["profile"].pop("tier", None)
    if seed % 29 == 0:
        doc["status"] = None
    return doc


def documents(count: int, start: int = 0) -> Iterator[tuple[str, Any]]:
    for i in range(start, start + count):
        yield f"doc{i:05d}", document(i)


def _deep_path(depth: int) -> list[Any]:
    """A path of exactly ``depth`` steps ending at a scalar leaf."""

    assert 1 <= depth <= 20
    steps: list[Any] = [Key(f"l{level:02d}") for level in range(1, depth + 1)]
    return steps


def queries() -> dict[str, tuple[list[Any], str]]:
    """Exactly 50 named queries: ``{name: (path, mode)}``."""

    q: dict[str, tuple[list[Any], str]] = {}

    # 8 deep paths, depths 4, 6, 8, 10, 12, 14, 18, 20.
    for depth in (4, 6, 8, 10, 12, 14, 18, 20):
        q[f"deep_{depth:02d}"] = (_deep_path(depth), "lax")

    # 8 scalar equality filters (all indexable, some conjunctions).
    q["filter_kind"] = ([Key("items"), Filter(eq([Key("kind")], "alpha"))], "lax")
    q["filter_batch"] = (
        [Key("items"), Filter(and_(eq([Key("kind")], "alpha"), eq([Key("batch")], 7)))],
        "lax",
    )
    q["filter_kind_grade"] = (
        [Key("items"), Filter(and_(eq([Key("kind")], "beta"), eq([Key("grade")], 3)))],
        "lax",
    )
    q["filter_kind_flag"] = (
        [Key("items"), Filter(and_(eq([Key("kind")], "gamma"), eq([Key("flag")], True)))],
        "lax",
    )
    q["filter_nested"] = (
        [Key("items"), Filter(eq([Key("labels"), Key("a")], 2))],
        "lax",
    )
    q["filter_nested_and"] = (
        [
            Key("items"),
            Filter(and_(eq([Key("labels"), Key("a")], 2), eq([Key("kind")], "alpha"))),
        ],
        "lax",
    )
    q["filter_three"] = (
        [
            Key("items"),
            Filter(
                and_(
                    eq([Key("kind")], "delta"),
                    eq([Key("labels"), Key("b")], "b1"),
                    eq([Key("grade")], 4),
                )
            ),
        ],
        "lax",
    )
    q["filter_bool"] = (
        [Key("items"), Filter(and_(eq([Key("flag")], False), eq([Key("kind")], "alpha")))],
        "lax",
    )
    q["filter_maybe_null"] = (
        [Key("items"), Filter(eq([Key("maybe")], None))],
        "lax",
    )

    # 8 non-indexable boolean queries (or/not/exists), full-scan candidates.
    q["filter_or"] = (
        [
            Key("items"),
            Filter(or_(eq([Key("kind")], "alpha"), eq([Key("kind")], "beta"))),
        ],
        "lax",
    )
    q["filter_not"] = (
        [Key("items"), Filter(not_(eq([Key("kind")], "alpha")))],
        "lax",
    )
    q["filter_exists"] = (
        [Key("items"), Filter(exists([Key("labels")]))],
        "lax",
    )
    q["filter_exists_maybe"] = (
        [Key("items"), Filter(exists([Key("maybe")]))],
        "lax",
    )
    q["filter_or_and"] = (
        [
            Key("items"),
            Filter(
                or_(
                    and_(eq([Key("kind")], "alpha"), eq([Key("grade")], 1)),
                    and_(eq([Key("kind")], "beta"), eq([Key("grade")], 2)),
                )
            ),
        ],
        "lax",
    )
    q["filter_not_exists"] = (
        [Key("items"), Filter(not_(exists([Key("labels")])))],
        "lax",
    )
    q["filter_nested_exists"] = (
        [Key("items"), Filter(exists([Key("labels"), Key("b")]))],
        "lax",
    )
    q["filter_metrics"] = (
        [Key("items"), Filter(eq([Key("metrics"), Index(0)], 1))],
        "lax",
    )

    # 6 plain scalar paths (no index possible; full scan reference).
    q["plain_tenant"] = ([Key("tenant")], "lax")
    q["plain_priority"] = ([Key("priority")], "lax")
    q["plain_nullable"] = ([Key("nullable")], "lax")
    q["plain_customer_region"] = ([Key("customer"), Key("profile"), Key("region")], "lax")
    q["plain_sparse"] = ([Key("sparse"), Key("present")], "lax")
    q["plain_missing"] = ([Key("does_not_exist"), Key("ever")], "lax")

    # 5 wildcard / negative-index paths.
    q["wild_tags"] = ([Key("tags"), ArrayWildcard()], "lax")
    q["wild_matrix_inner"] = (
        [Key("matrix"), ArrayWildcard(), Index(-1)],
        "lax",
    )
    q["wild_customer_obj"] = ([Key("customer"), ObjectWildcard()], "lax")
    q["neg_items_last"] = ([Key("items"), Index(-1), Key("kind")], "lax")
    q["neg_tags_last"] = ([Key("tags"), Index(-1)], "lax")

    # 4 nested array / filter combinations.
    q["nested_matrix_filter"] = (
        [Key("matrix"), ArrayWildcard(), Filter(exists([Index(0)]))],
        "lax",
    )
    q["nested_items_metrics"] = (
        [Key("items"), Filter(exists([Key("metrics"), Index(-1)]))],
        "lax",
    )
    q["nested_obj_groups"] = (
        [Key("customer"), Key("profile"), ObjectWildcard()],
        "lax",
    )
    q["nested_deep_obj"] = (
        [Key("l01"), Key("l02"), Key("l03"), Key("l04"), Key("l05"),
         Key("l06"), Key("l07"), Key("l08"), Key("l09"), Key("l10"),
         ObjectWildcard()],
        "lax",
    )

    # 5 strict queries: some documents have a locatable error, some match.
    q["strict_items_kind"] = ([Key("items"), ArrayWildcard(), Key("kind")], "strict")
    q["strict_items_labels"] = ([Key("items"), ArrayWildcard(), Key("labels")], "strict")
    q["strict_customer_tier"] = (
        [Key("customer"), Key("profile"), Key("tier")],
        "strict",
    )
    q["strict_sparse"] = ([Key("sparse"), Key("present")], "strict")
    q["strict_deep20"] = (_deep_path(20), "strict")

    # 6 additional lax queries: empty array, repeated values, dotted keys,
    # object order, nested different elements, plain deep leaf.
    q["lax_empty"] = ([Key("empty"), ArrayWildcard()], "lax")
    q["lax_repeated"] = (
        [Key("items"), Filter(or_(eq([Key("kind")], "alpha"), eq([Key("kind")], "delta")))],
        "lax",
    )
    q["lax_object_order"] = ([Key("labels"), ObjectWildcard()], "lax")
    q["lax_cross_elements"] = (
        [
            Key("items"),
            Filter(and_(exists([Key("kind")]), exists([Key("labels")]))),
        ],
        "lax",
    )
    q["lax_depth19"] = (_deep_path(19), "lax")

    assert len(q) == 50, len(q)
    return q
