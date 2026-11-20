"""Ventilation network catalog.

A catalog is an immutable, publishable snapshot of a balanced multi-zone
ventilation network:

* one positive volume per room (m^3),
* directed non-negative inter-room flows (m^3/s),
* non-negative supply (``OUT -> room``) and exhaust (``room -> OUT``) flows.

Publishing fails when any room is out of volumetric balance.  Every published
catalog has a SHA-256 identity derived from its canonical contents, so a
downstream experiment can record exactly which basis it used.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping

from .errors import BalanceError, LimitError, NotFoundError, ValidationError
from .rational import (
    OUT,
    fraction_to_float,
    parse_nonnegative,
    parse_positive,
    rational_pair,
    rational_str,
)
import numpy as np

MAX_ROOMS = 8


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    flow: Fraction

    @property
    def is_internal(self) -> bool:
        return self.src != OUT and self.dst != OUT

    @property
    def is_supply(self) -> bool:
        return self.src == OUT and self.dst != OUT

    @property
    def is_exhaust(self) -> bool:
        return self.src != OUT and self.dst == OUT

    @property
    def key(self) -> tuple[str, str]:
        return (self.src, self.dst)

    def to_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst, "flow": rational_pair(self.flow)}


class Catalog:
    """Mutable builder that becomes an immutable published product."""

    def __init__(self, name: str = ""):
        self.name = str(name)
        self._rooms: dict[str, Fraction] = {}
        self._flows: dict[tuple[str, str], Fraction] = {}
        self._published = False
        self.identity: str | None = None
        self.revision = 0
        self.parent_identity: str | None = None

    # -- construction -----------------------------------------------------
    def _check_editable(self):
        if self._published:
            raise ValidationError(
                "catalog is published/immutable; use revise_flow() to create a new revision"
            )

    def add_room(self, name: str, volume) -> "Catalog":
        self._check_editable()
        if not isinstance(name, str) or not name:
            raise ValidationError("room name must be a non-empty string")
        if name == OUT:
            raise ValidationError(f"room name {OUT!r} is reserved for outdoor air")
        if name in self._rooms:
            raise ValidationError(f"duplicate room {name!r}")
        if len(self._rooms) >= MAX_ROOMS:
            raise LimitError(f"at most {MAX_ROOMS} rooms are supported")
        self._rooms[name] = parse_positive(volume, field=f"volume[{name}]")
        return self

    def _require_room(self, name: str):
        if name not in self._rooms:
            raise NotFoundError(f"unknown room {name!r}")

    def _set_flow(self, src: str, dst: str, flow) -> None:
        if src == OUT and dst == OUT:
            raise ValidationError("edge OUT->OUT is meaningless")
        if src == dst:
            raise ValidationError(f"self-loop on {src!r} is not allowed")
        if src != OUT:
            self._require_room(src)
        if dst != OUT:
            self._require_room(dst)
        frac = parse_nonnegative(flow, field=f"flow[{src}->{dst}]")
        self._flows[(src, dst)] = frac

    def add_transfer(self, src: str, dst: str, flow) -> "Catalog":
        """Directed inter-room flow."""
        self._check_editable()
        if src == OUT or dst == OUT:
            raise ValidationError("use add_supply/add_exhaust for outdoor edges")
        self._set_flow(src, dst, flow)
        return self

    def add_supply(self, room: str, flow) -> "Catalog":
        self._check_editable()
        self._set_flow(OUT, room, flow)
        return self

    def add_exhaust(self, room: str, flow) -> "Catalog":
        self._check_editable()
        self._set_flow(room, OUT, flow)
        return self

    def set_flow(self, src: str, dst: str, flow) -> "Catalog":
        self._check_editable()
        if src != OUT and dst != OUT:
            self.add_transfer(src, dst, flow)
        elif src == OUT:
            self.add_supply(dst, flow)
        else:
            self.add_exhaust(src, flow)
        return self

    # -- inspection -------------------------------------------------------
    @property
    def rooms(self) -> dict[str, Fraction]:
        return dict(self._rooms)

    def room_names(self) -> list[str]:
        return sorted(self._rooms)

    def edges(self) -> list[Edge]:
        return [
            Edge(src, dst, flow)
            for (src, dst), flow in sorted(self._flows.items())
        ]

    def inflow(self, room: str) -> Fraction:
        self._require_room(room)
        total = Fraction(0)
        for (src, dst), flow in self._flows.items():
            if dst == room:
                total += flow
        return total

    def outflow(self, room: str) -> Fraction:
        self._require_room(room)
        total = Fraction(0)
        for (src, dst), flow in self._flows.items():
            if src == room:
                total += flow
        return total

    def supply(self, room: str) -> Fraction:
        return self._flows.get((OUT, room), Fraction(0))

    def exhaust(self, room: str) -> Fraction:
        return self._flows.get((room, OUT), Fraction(0))

    def balance_report(self) -> dict:
        rooms = {}
        ok = True
        for room in self.room_names():
            inflow = self.inflow(room)
            outflow = self.outflow(room)
            balanced = inflow == outflow
            ok = ok and balanced
            rooms[room] = {
                "inflow": rational_str(inflow),
                "outflow": rational_str(outflow),
                "residual": rational_str(inflow - outflow),
                "balanced": balanced,
            }
        return {"balanced": ok, "rooms": rooms}

    def validate(self) -> dict:
        report = self.balance_report()
        report["name"] = self.name
        report["room_count"] = len(self._rooms)
        report["edge_count"] = len(self._flows)
        return report

    # -- publishing -------------------------------------------------------
    def canonical_payload(self) -> dict:
        if self._published:
            # identity is stable for a published revision
            pass
        return {
            "name": self.name,
            "revision": self.revision,
            "rooms": [
                {"name": name, "volume": rational_pair(self._rooms[name])}
                for name in self.room_names()
            ],
            "edges": [e.to_dict() for e in self.edges()],
        }

    def compute_identity(self) -> str:
        blob = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    def publish(self) -> "Catalog":
        """Validate balance and freeze the catalog.  Returns self."""
        if self._published:
            return self
        if not self._rooms:
            raise ValidationError("cannot publish an empty catalog")
        report = self.balance_report()
        if not report["balanced"]:
            bad = [
                f"{room}: in={rooms['inflow']} out={rooms['outflow']}"
                for room, rooms in report["rooms"].items()
                if not rooms["balanced"]
            ]
            raise BalanceError("unbalanced network; refusing to publish: " + "; ".join(bad))
        self.identity = self.compute_identity()
        self._published = True
        return self

    @property
    def published(self) -> bool:
        return self._published

    def require_published(self):
        if not self._published:
            raise ValidationError("catalog must be published before use")

    # -- revision ---------------------------------------------------------
    def revise_flow(self, src: str, dst: str, new_flow) -> "Catalog":
        """Return a *new* catalog revision with one edge's flow changed.

        The original catalog object is never mutated, so experiments already
        referenced to it keep their original basis.
        """
        return self.revise_flows({(src, dst): new_flow})

    def revise_flows(self, changes: Mapping[tuple[str, str], object]) -> "Catalog":
        """Atomically revise one or more edge flows, keeping the network balanced.

        A single asymmetric flow edit usually breaks balance, so real
        corrections are often a balanced group (e.g. both directions of an
        exchange, or an exchange plus its make-up/exhaust).  All changes are
        applied together and the result is published only if it balances.
        """
        self.require_published()
        if not changes:
            raise ValidationError("no flow changes supplied")
        revised = Catalog(self.name)
        revised._rooms = dict(self._rooms)
        revised._flows = dict(self._flows)
        for (src, dst), new_flow in changes.items():
            if (src, dst) not in self._flows:
                raise NotFoundError(f"no edge {src}->{dst} to revise")
            if src == OUT and dst == OUT:
                raise ValidationError("edge OUT->OUT is meaningless")
            revised._flows[(src, dst)] = parse_nonnegative(
                new_flow, field=f"revised flow[{src}->{dst}]"
            )
        revised.revision = self.revision + 1
        revised.parent_identity = self.identity
        report = revised.balance_report()
        if not report["balanced"]:
            bad = [
                f"{room}: in={rooms['inflow']} out={rooms['outflow']}"
                for room, rooms in report["rooms"].items()
                if not rooms["balanced"]
            ]
            raise BalanceError(
                "revised network is unbalanced; refusing to create revision: "
                + "; ".join(bad)
            )
        return revised.publish()

    # -- numeric views ----------------------------------------------------
    def matrices(self) -> dict:
        """Return numeric arrays for the ODE and mass bookkeeping.

        ODE: ``V * dc/dt = s + A @ c - diag(outflow) @ c`` where ``A[j, i]`` is
        the flow from room ``j`` to room ``i`` (zero for outdoor edges).
        """
        self.require_published()
        names = self.room_names()
        idx = {name: i for i, name in enumerate(names)}
        n = len(names)
        A = np.zeros((n, n), dtype=float)
        V = np.zeros(n, dtype=float)
        outflow = np.zeros(n, dtype=float)
        supply = np.zeros(n, dtype=float)
        exhaust = np.zeros(n, dtype=float)
        exhaust_edges: list[dict] = []
        for i, name in enumerate(names):
            V[i] = fraction_to_float(self._rooms[name])
        for (src, dst), flow in sorted(self._flows.items()):
            q = fraction_to_float(flow)
            if src == OUT and dst != OUT:
                supply[idx[dst]] += q
            elif src != OUT and dst == OUT:
                exhaust[idx[src]] += q
                exhaust_edges.append(
                    {"room": src, "flow": q, "flow_exact": rational_str(flow)}
                )
            else:
                A[idx[dst], idx[src]] += q
                outflow[idx[src]] += q
        # room->room outflows plus exhaust
        for (src, dst), flow in self._flows.items():
            if src != OUT and dst == OUT:
                outflow[idx[src]] += fraction_to_float(flow)
        K = A / V[:, None]
        K[np.diag_indices(n)] -= outflow / V
        return {
            "names": names,
            "index": idx,
            "n": n,
            "V": V,
            "A": A,
            "outflow": outflow,
            "supply": supply,
            "exhaust": exhaust,
            "K": K,
            "exhaust_edges": exhaust_edges,
            "volumes_exact": {name: self._rooms[name] for name in names},
            "flows_exact": dict(self._flows),
        }

    def to_dict(self) -> dict:
        return {
            "identity": self.identity,
            "parent_identity": self.parent_identity,
            "revision": self.revision,
            "published": self._published,
            "canonical": self.canonical_payload(),
        }

    # -- (de)serialization from external products -------------------------
    @classmethod
    def from_product(cls, product: Mapping) -> "Catalog":
        """Load an external catalog product in the same JSON shape.

        Accepts either ``{"canonical": {...}}`` or a bare canonical payload.
        Volumes/flows may be ``{num,den}`` or rational strings.
        """
        payload = product.get("canonical", product) if isinstance(product, Mapping) else product
        if not isinstance(payload, Mapping):
            raise ValidationError("catalog product must be a mapping")
        cat = cls(payload.get("name", ""))
        cat.revision = int(payload.get("revision", 0))
        rooms = payload.get("rooms")
        if not isinstance(rooms, list):
            raise ValidationError("catalog product missing 'rooms' list")
        for room in rooms:
            try:
                name = room["name"]
                vol = room["volume"]
            except (TypeError, KeyError) as exc:
                raise ValidationError("malformed room entry") from exc
            cat.add_room(name, _rational_from_json(vol, field=f"volume[{name}]"))
        edges = payload.get("edges")
        if not isinstance(edges, list):
            raise ValidationError("catalog product missing 'edges' list")
        for edge in edges:
            try:
                src, dst = edge["src"], edge["dst"]
                flow = edge["flow"]
            except (TypeError, KeyError) as exc:
                raise ValidationError("malformed edge entry") from exc
            cat._set_flow(src, dst, _rational_from_json(flow, field=f"flow[{src}->{dst}]"))
        cat.publish()
        return cat


def _rational_from_json(value, *, field: str):
    if isinstance(value, dict):
        num, den = value.get("num"), value.get("den")
        if not isinstance(num, int) or not isinstance(den, int) or den == 0:
            raise ValidationError(f"{field}: bad rational pair {value!r}")
        frac = Fraction(num, den)
    else:
        frac = parse_nonnegative(value, field=field)
    if frac < 0:
        raise ValidationError(f"{field}: negative value")
    return frac


def catalog_identity(product: Mapping | Catalog) -> str:
    """Recompute and return the canonical identity of a catalog product."""
    if isinstance(product, Catalog):
        if product.published:
            return product.identity
        return product.compute_identity()
    return Catalog.from_product(product).identity
