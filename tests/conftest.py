"""Shared fixtures and independent (test-side) verifiers.

The verifiers in this module deliberately re-implement the specifications
with independent, naive code so the optimized services are cross-checked
against a separate enumeration — per the task's acceptance method.
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coldchain import ColdChainService, Repository  # noqa: E402
from coldchain.geometry import Rect, oriented_dims  # noqa: E402
from coldchain.models import BoxPlan, PackingPlan, Placement  # noqa: E402
from coldchain.packing import NORMALIZATION  # noqa: E402


def make_sealed_box(svc, box_type_id, zone, placements):
    """Seal a hand-built single-box plan. placements: (uid, x, y, rotated)."""
    with svc.repo.read() as con:
        bt = svc.repo.get_box_type(con, box_type_id)
        items = {uid: svc.repo.get_item(con, uid) for uid, *_ in placements}
    gross = bt["empty_weight"] + sum(it.weight for it in items.values())
    box = BoxPlan(
        box_type_id=box_type_id, zone=zone,
        placements=tuple(Placement(uid, x, y, r)
                         for uid, x, y, r in placements),
        gross_weight=gross, empty_weight=bt["empty_weight"],
        area=bt["inner_length"] * bt["inner_width"])
    versions = tuple(sorted((uid, it.version)
                            for uid, it in items.items()))
    import uuid
    plan = PackingPlan(boxes=(box,), item_versions=versions,
                       plan_id="PLN-manual-" + uuid.uuid4().hex[:12],
                       normalization=NORMALIZATION)
    return svc.sealing.seal(plan)[0]


@pytest.fixture()
def svc(tmp_path):
    db = str(tmp_path / "coldchain.db")
    service = ColdChainService(Repository(db))
    yield service


@pytest.fixture()
def scoped_svc(svc):
    """The scoped dataset: 8 items, 3 box types, (6 sealed boxes), 4 shifts."""
    svc.register_box_type("S", 4, 4, 2, 20, {"CHILLED"})
    svc.register_box_type("M", 6, 5, 4, 11, {"CHILLED"})
    svc.register_box_type("L", 8, 6, 6, 12, {"CHILLED", "FROZEN", "DEEP"})
    svc.register_item("A1", 2, 2, 3, "CHILLED")
    svc.register_item("A2", 2, 2, 3, "CHILLED")
    svc.register_item("B1", 3, 2, 4, "CHILLED")
    svc.register_item("B2", 3, 2, 4, "CHILLED")
    svc.register_item("C1", 2, 3, 5, "FROZEN")
    svc.register_item("C2", 2, 3, 5, "FROZEN")
    svc.register_item("D1", 2, 2, 2, "DEEP")
    svc.register_item("D2", 3, 3, 6, "DEEP")
    svc.register_shift("T1", {"CHILLED"}, 30, 4, 50, 10)
    svc.register_shift("T2", {"CHILLED", "FROZEN", "DEEP"}, 60, 6, 80, 12)
    svc.register_shift("T3", {"FROZEN", "DEEP"}, 40, 4, 40, 8)
    svc.register_shift("T4", {"CHILLED", "FROZEN", "DEEP"}, 25, 3, 30, 6)
    return svc


# ---------------------------------------------------------------------------
# Independent verifiers (naive, written separately from the package code)
# ---------------------------------------------------------------------------

def naive_placeable(items, inner_l, inner_w):
    """Independent feasibility check: naive recursive placement.

    items: list of (unit_id, length, width). Returns True/False.
    """
    placed = []

    def free(x, y, w, h):
        r = Rect(x, y, w, h)
        return all(not r.overlaps(p) for p in placed)

    def go(i):
        if i == len(items):
            return True
        _uid, l, w = items[i]
        for rot in (0, 1):
            ww, hh = oriented_dims(l, w, rot)
            for x in range(inner_l - ww + 1):
                for y in range(inner_w - hh + 1):
                    if free(x, y, ww, hh):
                        placed.append(Rect(x, y, ww, hh))
                        if go(i + 1):
                            return True
                        placed.pop()
        return False

    return go(0)


def naive_partitions(n):
    """All set partitions of range(n) as lists of blocks (lists of indices)."""
    if n == 0:
        yield []
        return
    for part in naive_partitions(n - 1):
        for b in part:
            b.append(n - 1)
            yield [list(x) for x in part]
            b.pop()
        yield part + [[n - 1]]


def naive_best_packing(items, box_types):
    """Independent optimum: brute force over partitions x type assignments.

    Returns (min_boxes, min_area) or None if infeasible.
    """
    n = len(items)
    best = None
    for part in naive_partitions(n):
        # zone homogeneity
        if any(len({items[i].zone for i in block}) != 1 for block in part):
            continue
        per_block_types = []
        for block in part:
            zone = items[block[0]].zone
            weight = sum(items[i].weight for i in block)
            ok = []
            for t in box_types:
                if zone not in t.allowed_zones:
                    continue
                if weight + t.empty_weight > t.max_gross_weight:
                    continue
                if not naive_placeable(
                        [(items[i].unit_id, items[i].length, items[i].width)
                         for i in block], t.inner_length, t.inner_width):
                    continue
                ok.append(t)
            if not ok:
                break
            per_block_types.append(ok)
        else:
            for combo in itertools.product(*per_block_types):
                area = sum(t.area for t in combo)
                cand = (len(part), area)
                if best is None or cand < best:
                    best = cand
    return best


def naive_best_transport(boxes, shifts):
    """Independent optimum over all shift assignments.

    boxes: list of (box_id, zone, gross, store). shifts: list of
    (shift_id, zones, max_w, max_n, fixed, stop). Returns the best
    (cost, vehicles, vector) or None.
    """
    best = None
    for assign in itertools.product(range(len(shifts)), repeat=len(boxes)):
        load = {}
        cnt = {}
        stores = {}
        ok = True
        for (bid, zone, gross, store), j in zip(boxes, assign):
            sid, zones, max_w, max_n, fixed, stop = shifts[j]
            if zone not in zones:
                ok = False
                break
            load[j] = load.get(j, 0) + gross
            cnt[j] = cnt.get(j, 0) + 1
            stores.setdefault(j, set()).add(store)
            if load[j] > max_w or cnt[j] > max_n:
                ok = False
                break
        if not ok:
            continue
        cost = sum(shifts[j][4] + len(stores[j]) * shifts[j][5]
                   for j in cnt)
        vehicles = len(cnt)
        vector = tuple(shifts[j][0] for j in assign)
        key = (cost, vehicles, vector)
        if best is None or key < best:
            best = key
    return best
