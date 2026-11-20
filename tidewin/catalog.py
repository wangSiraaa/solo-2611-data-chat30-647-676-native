"""Independent synthetic tide catalog with revisionary publishing.

The catalog owns port tide models and exposes the public query products:

* level at a specified instant (with per-harmonic breakdown),
* extrema / minimum over a requested interval (via exact critical points),
* local term contributions.

Models are published as *revisions*: a port always serves its most recently
published revision, but every older revision stays queryable forever.  All
analysis and passage results record the revision they consumed, so an atomic
model correction ("new product") changes future passage windows while all prior
analysis remains reproduced against the revision it was run under.
"""
from __future__ import annotations

import json
from fractions import Fraction
from typing import Dict, List, Optional

from .model import TideModel
from .rational import F as _F


class CatalogError(KeyError):
    pass


class TideCatalog:
    """A collection of port tide models indexed by (port, revision)."""

    def __init__(self):
        self._models: Dict[str, Dict[int, TideModel]] = {}
        self._current: Dict[str, int] = {}

    # ------------------------------------------------------------ publishing
    def publish(self, port: str, b, a, c, revision: Optional[int] = None,
                note: str = "") -> TideModel:
        """Publish a new/product revision for ``port`` and make it current.

        Coefficients must be rational.  Publishing is atomic: the new revision
        becomes current only once it is fully constructed and stored.
        """
        if revision is None:
            revision = self._current.get(port, 0) + 1
        revision = int(revision)
        if port in self._models and revision in self._models[port]:
            raise CatalogError(
                f"port {port!r} already has a revision {revision}; "
                "publish a new revision number instead of overwriting")
        model = TideModel(port, b, a, c, revision=revision, note=note)
        self._models.setdefault(port, {})[revision] = model
        # make current only if this is the highest revision for the port
        if revision >= self._current.get(port, 0):
            self._current[port] = revision
        return model

    # compatibility alias: "add / edit revision"
    def add_revision(self, port: str, b, a, c, revision: Optional[int] = None,
                     note: str = "") -> TideModel:
        return self.publish(port, b, a, c, revision=revision, note=note)

    # -------------------------------------------------------------- resolving
    def current_revision(self, port: str) -> int:
        if port not in self._current:
            raise CatalogError(f"unknown port {port!r}")
        return self._current[port]

    def get_model(self, port: str, revision: Optional[int] = None) -> TideModel:
        """Return a specific revision (default: the current one)."""
        if port not in self._models:
            raise CatalogError(f"unknown port {port!r}")
        if revision is None:
            revision = self._current[port]
        try:
            return self._models[port][int(revision)]
        except KeyError:
            raise CatalogError(
                f"port {port!r} has no revision {revision}") from None

    def ports(self) -> List[str]:
        return sorted(self._models)

    def revisions(self, port: str) -> List[int]:
        if port not in self._models:
            raise CatalogError(f"unknown port {port!r}")
        return sorted(self._models[port])

    # ----------------------------------------------------------------- queries
    def level_at(self, port: str, t, revision: Optional[int] = None) -> dict:
        model = self.get_model(port, revision)
        return {"port": port, "revision": model.revision, "time": float(t),
                **model.local_terms_mp(t)}

    def extrema(self, port: str, lo, hi, revision: Optional[int] = None) -> dict:
        model = self.get_model(port, revision)
        return model.extrema(float(lo), float(hi))

    def local_terms(self, port: str, t, revision: Optional[int] = None) -> dict:
        model = self.get_model(port, revision)
        return {"port": port, "revision": model.revision, "time": float(t),
                **model.local_terms_mp(t)}

    # ------------------------------------------------------------ serialization
    def to_dict(self) -> dict:
        return {
            "current": dict(self._current),
            "models": [
                m.to_dict()
                for port in self._models
                for rev, m in sorted(self._models[port].items())
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TideCatalog":
        cat = cls()
        for md in d["models"]:
            m = TideModel.from_dict(md)
            cat._models.setdefault(m.port, {})[m.revision] = m
        for port, rev in d.get("current", {}).items():
            cat._current[port] = int(rev)
        # if current missing, infer highest
        for port in cat._models:
            if port not in cat._current:
                cat._current[port] = max(cat._models[port])
        return cat
