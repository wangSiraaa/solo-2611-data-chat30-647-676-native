"""Frozen data models shared by the catalogue and the repository.

These dataclasses are the in-memory representation of *published products*
and of imported borehole data.  JSON (de)serialisation lives here so that
external products and SQLite payloads share one canonical format.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


# --------------------------------------------------------------------------- #
# Catalogue / product models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Horizon:
    """A finite horizon with a stable ID and an acceptable lithology set."""

    horizon_id: str
    name: str
    acceptable_lithology: frozenset[str]
    source: str

    def to_dict(self) -> dict:
        return {
            "horizon_id": self.horizon_id,
            "name": self.name,
            "acceptable_lithology": sorted(self.acceptable_lithology),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Horizon":
        return cls(
            horizon_id=data["horizon_id"],
            name=data["name"],
            acceptable_lithology=frozenset(data["acceptable_lithology"]),
            source=data["source"],
        )


@dataclass(frozen=True)
class Edge:
    """A direct ``shallower_than`` edge: ``parent`` is shallower than ``child``."""

    parent_id: str
    child_id: str
    source: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Edge":
        return cls(
            parent_id=data["parent_id"],
            child_id=data["child_id"],
            source=data["source"],
        )


@dataclass(frozen=True)
class Product:
    """An immutable, versioned catalogue product.

    A product is a self-contained partial-order snapshot consumed by the
    repository.  ``kind`` is ``"local"`` for catalogue publications or
    ``"external"`` for products imported directly into a repository.
    """

    product_id: str
    version: int
    kind: str
    note: str
    horizons: tuple[Horizon, ...]
    edges: tuple[Edge, ...]

    def to_json(self, indent: int | None = 2) -> str:
        payload = {
            "product_id": self.product_id,
            "version": self.version,
            "kind": self.kind,
            "note": self.note,
            "horizons": [h.to_dict() for h in self.horizons],
            "edges": [e.to_dict() for e in self.edges],
        }
        return json.dumps(payload, ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "Product":
        data = json.loads(text)
        return cls(
            product_id=data["product_id"],
            version=int(data["version"]),
            kind=data["kind"],
            note=data.get("note", ""),
            horizons=tuple(Horizon.from_dict(h) for h in data["horizons"]),
            edges=tuple(Edge.from_dict(e) for e in data["edges"]),
        )


# --------------------------------------------------------------------------- #
# Borehole input models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SegmentInput:
    """One cored, non-overlapping interval ``[start, end)`` with positive length."""

    segment_id: str
    start: str
    end: str
    lithology: str
    weight: int
    fixed_horizon: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SegmentInput":
        return cls(
            segment_id=data["segment_id"],
            start=data["start"],
            end=data["end"],
            lithology=data["lithology"],
            weight=int(data["weight"]),
            fixed_horizon=data.get("fixed_horizon"),
        )


@dataclass(frozen=True)
class GapInput:
    """An explicit uncored interval ``[start, end)`` with positive length."""

    gap_id: str
    start: str
    end: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GapInput":
        return cls(gap_id=data["gap_id"], start=data["start"], end=data["end"])


@dataclass(frozen=True)
class BoreholeInput:
    """A borehole: shallow-to-deep segments and explicit uncored gaps.

    All entries (segments and gaps) are non-overlapping and ordered shallow
    to deep; touching endpoints are allowed (``prev.end == next.start``).
    """

    borehole_id: str
    segments: tuple[SegmentInput, ...]
    gaps: tuple[GapInput, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "borehole_id": self.borehole_id,
            "segments": [s.to_dict() for s in self.segments],
            "gaps": [g.to_dict() for g in self.gaps],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BoreholeInput":
        return cls(
            borehole_id=data["borehole_id"],
            segments=tuple(SegmentInput.from_dict(s) for s in data["segments"]),
            gaps=tuple(GapInput.from_dict(g) for g in data.get("gaps", [])),
        )


@dataclass(frozen=True)
class SampleInput:
    """A sample with one exact depth (rational metres).

    ``borehole_id`` is optional in the import format; when omitted the
    repository searches every borehole and reports ambiguity if the depth is
    contained in more than one borehole.
    """

    sample_id: str
    depth: str
    borehole_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SampleInput":
        return cls(
            sample_id=data["sample_id"],
            depth=data["depth"],
            borehole_id=data.get("borehole_id"),
        )
