"""Sealing service (封箱).

Turns an explicit, fully validated packing plan into immutable sealed-box
products. Sealing is:

* **atomic** — all boxes of a plan are written in one transaction; any
  validation or persistence failure leaves zero partial state;
* **idempotent** — ``sealed_box_id`` is a content hash, so re-sealing an
  identical plan (复读) returns the same ids without duplicating rows;
* **immutable** — the database rejects UPDATE/DELETE on sealed boxes, and
  every sealed box snapshots the item dimensions at seal time, so later
  changes to the item master data can only produce *new* plans/boxes and
  never alter old sealed content.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .errors import NotFoundError, PlanValidationError, StalePlanError
from .geometry import validate_explicit
from .models import PackingPlan, SealedBox, SealedItem
from .packing import NORMALIZATION
from .repository import Repository


def sealed_box_canonical_json(box_type_id: str, zone: str, empty_weight: int,
                              gross_weight: int,
                              items: list[SealedItem]) -> str:
    doc = {
        "normalization": NORMALIZATION,
        "box_type_id": box_type_id,
        "zone": zone,
        "empty_weight": empty_weight,
        "gross_weight": gross_weight,
        "items": [
            {"unit_id": it.unit_id, "x": it.x, "y": it.y,
             "rotated": it.rotated, "length": it.length, "width": it.width,
             "weight": it.weight}
            for it in sorted(items, key=lambda i: i.unit_id)
        ],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def sealed_box_id_from_canonical(canon: str) -> str:
    return "SBX-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


class SealingService:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ------------------------------------------------------------------
    def seal(self, plan: PackingPlan) -> list[SealedBox]:
        """Validate and atomically seal every box of ``plan``.

        Returns the sealed boxes in canonical order. If the plan was already
        sealed before, the existing boxes are returned unchanged (复读).
        """
        with self.repo.write_tx() as con:
            boxes = self._validate(con, plan)
            out = []
            for box, canon in boxes:
                existing = self.repo.get_sealed_box(con, box.sealed_box_id)
                if existing is not None:
                    out.append(existing)  # idempotent re-read
                    continue
                self.repo.insert_sealed_box(con, box, canon, {})
                out.append(box)
            return out

    # ------------------------------------------------------------------
    def get(self, sealed_box_id: str) -> SealedBox:
        with self.repo.read() as con:
            box = self.repo.get_sealed_box(con, sealed_box_id)
        if box is None:
            raise NotFoundError(f"sealed box {sealed_box_id} not found")
        return box

    def list(self) -> list[SealedBox]:
        with self.repo.read() as con:
            return self.repo.all_sealed_boxes(con)

    # ------------------------------------------------------------------
    def _validate(self, con, plan: PackingPlan) -> list[tuple[SealedBox, str]]:
        violations: list[str] = []
        plan_versions = dict(plan.item_versions)

        # 1. every referenced item exists at exactly the planned version
        items = {}
        for unit_id, version in plan_versions.items():
            cur = self.repo.get_item(con, unit_id)
            if cur is None:
                violations.append(f"item {unit_id}: not registered")
                continue
            if cur.version != version:
                raise StalePlanError(
                    f"item {unit_id} changed (plan v{version},"
                    f" current v{cur.version}); re-optimize to a new plan")
            items[unit_id] = cur

        # 2. each item appears in exactly one box of the plan
        seen: dict[str, int] = {}
        for box in plan.boxes:
            for p in box.placements:
                seen[p.unit_id] = seen.get(p.unit_id, 0) + 1
        for uid in plan_versions:
            c = seen.get(uid, 0)
            if c == 0:
                violations.append(f"item {uid}: missing from plan")
            elif c > 1:
                violations.append(f"item {uid}: appears in {c} boxes")
        for uid in seen:
            if uid not in plan_versions:
                violations.append(f"item {uid}: placed but not declared"
                                  f" in plan item set")

        built: list[tuple[SealedBox, str]] = []
        for bi, box in enumerate(plan.boxes):
            bt = self.repo.get_box_type(con, box.box_type_id)
            if bt is None:
                violations.append(f"box {bi}: unknown box type"
                                  f" {box.box_type_id}")
                continue
            placements = list(box.placements)
            if not placements:
                violations.append(f"box {bi}: empty box")
                continue
            zones = {items[p.unit_id].zone for p in placements
                     if p.unit_id in items}
            if len(zones) > 1:
                violations.append(
                    f"box {bi}: mixed temperature zones {sorted(zones)}")
                continue
            if not zones:
                continue  # placements reference unknown items; reported above
            zone = next(iter(zones))
            allowed = set(json.loads(bt["allowed_zones"]))
            if zone not in allowed:
                violations.append(
                    f"box {bi}: zone {zone} not allowed in"
                    f" {bt['box_type_id']}")
                continue
            # geometry: real coordinates, non-negative ints, no overlap
            geo_v = validate_explicit(
                [(p.unit_id, p.x, p.y, p.rotated,
                  items[p.unit_id].length, items[p.unit_id].width)
                 for p in placements],
                bt["inner_length"], bt["inner_width"])
            violations.extend(f"box {bi}: {v}" for v in geo_v)
            if geo_v:
                continue
            # weight: gross includes the empty box weight
            gross = bt["empty_weight"] + sum(items[p.unit_id].weight
                                             for p in placements)
            if gross > bt["max_gross_weight"]:
                violations.append(
                    f"box {bi}: gross weight {gross} exceeds max"
                    f" {bt['max_gross_weight']} (empty box"
                    f" {bt['empty_weight']} included)")
                continue
            if gross != box.gross_weight:
                violations.append(
                    f"box {bi}: declared gross {box.gross_weight} != actual"
                    f" {gross}")
                continue
            sealed_items = tuple(sorted(
                (SealedItem(unit_id=p.unit_id, x=p.x, y=p.y,
                            rotated=p.rotated,
                            length=items[p.unit_id].length,
                            width=items[p.unit_id].width,
                            weight=items[p.unit_id].weight,
                            zone=items[p.unit_id].zone)
                 for p in placements),
                key=lambda i: i.unit_id))
            canon = sealed_box_canonical_json(
                bt["box_type_id"], zone, bt["empty_weight"], gross,
                list(sealed_items))
            built.append((SealedBox(
                sealed_box_id=sealed_box_id_from_canonical(canon),
                plan_id=plan.plan_id,
                box_type_id=bt["box_type_id"],
                zone=zone,
                gross_weight=gross,
                empty_weight=bt["empty_weight"],
                items=sealed_items,
                created_at=datetime.now(timezone.utc).isoformat(),
            ), canon))

        if violations:
            raise PlanValidationError(
                f"plan failed sealing validation ({len(violations)}"
                f" violation(s))", violations)
        return built
