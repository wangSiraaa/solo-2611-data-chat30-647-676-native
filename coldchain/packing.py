"""Packing optimization service (装箱服务).

Pure, side-effect free and deterministic: given items and box types it returns
the optimal single-layer 2D packing. Packers can call it independently of any
database; persistence is layered on top by the sealing service.

Optimization hierarchy (exact, by exhaustive enumeration — scope is small):
  1. minimize the number of boxes;
  2. minimize the total box footprint area (sum of used box-type areas);
  3. minimize the canonical plan key (lexicographic tie-break).

Canonical normalization ``coldchain-norm-v1``
---------------------------------------------
* Items inside a box are sorted by ascending ``unit_id``.
* The coordinate tuple of a box is the lexicographically minimal feasible
  placement vector ``((x,y,rotated) for each item in unit_id order)`` —
  see :func:`coldchain.geometry.find_canonical_placement`.
* A box record is ``(box_type_id, (unit_id, ...), ((x,y,rotated), ...))``.
* A plan key is the tuple of its box records sorted ascending; plans compare
  lexicographically. ``rotated`` is 0/1, coordinates are non-negative ints.
* ``plan_id`` = ``"PLN-" + sha256(canonical_json)[:16]`` where
  ``canonical_json`` is the UTF-8 JSON of the normalized plan with sorted
  keys and compact separators. The normalization version string is stored
  inside every persisted plan.
"""
from __future__ import annotations

import hashlib
import json
from itertools import product

from .errors import PackingInfeasibleError
from .geometry import find_canonical_placement
from .models import BoxPlan, BoxType, Item, PackingPlan, Placement

NORMALIZATION = "coldchain-norm-v1"


# ---------------------------------------------------------------------------
# Canonical keys
# ---------------------------------------------------------------------------

def box_record(box_type_id: str,
               placements: tuple[tuple[str, int, int, int], ...]) -> tuple:
    """Canonical record of one box: (type, unit ids, coordinate tuples)."""
    ordered = tuple(sorted(placements, key=lambda p: p[0]))
    return (box_type_id,
            tuple(p[0] for p in ordered),
            tuple((p[1], p[2], p[3]) for p in ordered))


def plan_key_of(box_records: list[tuple]) -> tuple:
    """Canonical key of a whole plan: sorted box records."""
    return tuple(sorted(box_records))


def canonical_json(plan: PackingPlan) -> str:
    """Serialized normalized plan — this exact string is hashed for plan_id."""
    doc = {
        "normalization": plan.normalization,
        "boxes": [
            {
                "box_type_id": b.box_type_id,
                "zone": b.zone,
                "items": [
                    {"unit_id": p.unit_id, "x": p.x, "y": p.y,
                     "rotated": p.rotated}
                    for p in sorted(b.placements, key=lambda p: p.unit_id)
                ],
            }
            for b in plan.boxes  # already in canonical order
        ],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def plan_id_from_canonical(canon: str) -> str:
    return "PLN-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------

def optimize(items: list[Item] | tuple[Item, ...],
             box_types: list[BoxType] | tuple[BoxType, ...]) -> PackingPlan:
    """Return the optimal packing plan for ``items`` using ``box_types``.

    Raises :class:`PackingInfeasibleError` when no feasible packing exists.
    Deterministic: identical inputs always yield the identical plan.
    """
    if not items:
        raise PackingInfeasibleError("no items to pack")
    if not box_types:
        raise PackingInfeasibleError("no box types available")
    ids = [it.unit_id for it in items]
    if len(set(ids)) != len(ids):
        raise PackingInfeasibleError("duplicate unit_id in input")

    items = tuple(sorted(items, key=lambda it: it.unit_id))
    types = tuple(sorted(box_types, key=lambda t: t.box_type_id))
    n = len(items)

    # --- per-item necessary condition: fits in at least one box type alone --
    unmet: list[str] = []
    for it in items:
        ok = any(
            it.zone in t.allowed_zones
            and it.weight + t.empty_weight <= t.max_gross_weight
            and _fits_dims(it, t)
            for t in types
        )
        if not ok:
            unmet.append(
                f"item {it.unit_id} (zone={it.zone}, {it.length}x{it.width}, "
                f"w={it.weight}) fits in no box type alone")
    if unmet:
        raise PackingInfeasibleError(
            "no feasible packing: some items fit nowhere", unmet)

    min_item_area = {it.unit_id: min(t.area for t in types
                                     if it.zone in t.allowed_zones
                                     and _fits_dims(it, t)
                                     and it.weight + t.empty_weight
                                     <= t.max_gross_weight)
                     for it in items}

    # --- geometry cache: (sorted unit ids, box type) -> placement or None ---
    geom_cache: dict[tuple[tuple[str, ...], str],
                     tuple[tuple[str, int, int, int], ...] | None] = {}

    def placement_for(member_ids: tuple[str, ...], t: BoxType):
        key = (member_ids, t.box_type_id)
        if key in geom_cache:
            return geom_cache[key]
        members = [it for it in items if it.unit_id in key[0]]
        # cheap necessary conditions before the exact search
        if sum(it.length * it.width for it in members) > t.area:
            geom_cache[key] = None
            return None
        res = find_canonical_placement(
            [(it.unit_id, it.length, it.width) for it in members],
            t.inner_length, t.inner_width)
        geom_cache[key] = res
        return res

    def feasible_types(member_idx: list[int]) -> list[tuple[BoxType, tuple]]:
        """Box types that can physically hold this (partial or full) block."""
        zone = items[member_idx[0]].zone
        weight = sum(items[i].weight for i in member_idx)
        out = []
        for t in types:
            if zone not in t.allowed_zones:
                continue
            if weight + t.empty_weight > t.max_gross_weight:
                continue
            if not all(_fits_dims(items[i], t) for i in member_idx):
                continue
            out.append(t)
        return out

    # --- best solution tracker ---------------------------------------------
    best: dict = {"k": None, "area": None, "key": None, "solution": None}

    # blocks: list of [zone, [item indices]]
    blocks: list[list] = []

    def evaluate_leaf() -> None:
        k = len(blocks)
        if best["k"] is not None and k > best["k"]:
            return
        # per block: feasible types with canonical placement, keep min area
        per_block: list[list[tuple[BoxType, tuple]]] = []
        min_area_sum = 0
        for _zone, idxs in blocks:
            member_ids = tuple(sorted(items[i].unit_id for i in idxs))
            cands = []
            for t in feasible_types(idxs):
                pl = placement_for(member_ids, t)
                if pl is not None:
                    cands.append((t, pl))
            if not cands:
                return
            block_min = min(t.area for t, _ in cands)
            per_block.append([(t, pl) for t, pl in cands
                              if t.area == block_min])
            min_area_sum += block_min
        if best["k"] == k and best["area"] is not None \
                and min_area_sum > best["area"]:
            return
        # enumerate the (usually tiny) combinations of min-area types
        for combo in product(*per_block):
            records = [box_record(t.box_type_id, pl) for t, pl in combo]
            key = plan_key_of(records)
            better = (
                best["k"] is None
                or k < best["k"]
                or (k == best["k"] and min_area_sum < best["area"])
                or (k == best["k"] and min_area_sum == best["area"]
                    and key < best["key"])
            )
            if better:
                best["k"] = k
                best["area"] = min_area_sum
                best["key"] = key
                best["solution"] = [
                    (t, pl, _zone) for (t, pl), (_zone, _idxs)
                    in zip(combo, blocks)
                ]

    def lower_bound_area(open_blocks: list[list], start: int) -> int:
        """Admissible lower bound on the final total area.

        Open blocks keep their identity: each ends with an area >= its
        current minimum (growing a block only shrinks its feasible type
        set). Remaining items may join open blocks for free, so they add
        nothing — except items whose zone matches no open block: those
        need at least one new box, contributing at least the smallest
        individual area among them.
        """
        lb = 0
        for _zone, idxs in open_blocks:
            cands = feasible_types(idxs)
            lb += min((t.area for t in cands), default=10 ** 9)
        open_zones = {z for z, _ in open_blocks}
        outsiders = [min_item_area[items[j].unit_id]
                     for j in range(start, n)
                     if items[j].zone not in open_zones]
        if outsiders:
            lb += min(outsiders)
        return lb

    def recurse(i: int) -> None:
        if best["k"] is not None and len(blocks) > best["k"]:
            return
        if (best["k"] is not None and len(blocks) == best["k"]
                and best["area"] is not None
                and lower_bound_area(blocks, i) > best["area"]):
            return
        if i == n:
            evaluate_leaf()
            return
        it = items[i]
        for block in blocks:
            if block[0] != it.zone:
                continue
            idxs = block[1] + [i]
            if not feasible_types(idxs):
                continue
            block[1].append(i)
            recurse(i + 1)
            block[1].pop()
        blocks.append([it.zone, [i]])
        recurse(i + 1)
        blocks.pop()

    recurse(0)

    if best["solution"] is None:
        raise PackingInfeasibleError(
            "no feasible packing exists for these items and box types",
            ["no partition of the item set is physically packable"])

    return _build_plan(items, types, best["solution"])


def _fits_dims(it: Item, t: BoxType) -> bool:
    return ((it.length <= t.inner_length and it.width <= t.inner_width)
            or (it.width <= t.inner_length and it.length <= t.inner_width))


def _build_plan(items: tuple[Item, ...],
                types: tuple[BoxType, ...],
                solution: list) -> PackingPlan:
    by_id = {it.unit_id: it for it in items}
    box_plans: list[BoxPlan] = []
    records = []
    for t, placement, zone in solution:
        gross = t.empty_weight + sum(by_id[uid].weight
                                     for uid, *_ in placement)
        records.append(box_record(t.box_type_id, placement))
        box_plans.append((records[-1], BoxPlan(
            box_type_id=t.box_type_id,
            zone=zone,
            placements=tuple(Placement(uid, x, y, r)
                             for uid, x, y, r in placement),
            gross_weight=gross,
            empty_weight=t.empty_weight,
            area=t.area,
        )))
    box_plans.sort(key=lambda pair: pair[0])
    ordered_boxes = tuple(bp for _rec, bp in box_plans)
    versions = tuple(sorted((it.unit_id, it.version) for it in items))
    skeleton = PackingPlan(boxes=ordered_boxes, item_versions=versions,
                           plan_id="", normalization=NORMALIZATION)
    pid = plan_id_from_canonical(canonical_json(skeleton))
    return PackingPlan(boxes=ordered_boxes, item_versions=versions,
                       plan_id=pid, normalization=NORMALIZATION)
