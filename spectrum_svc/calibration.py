"""Published calibration products and their exact channel<->energy geometry.

The private calibration points live in the catalog SQLite database.  Once
published they become an immutable *product*: plain JSON with rational
numbers as exact ``"n/d"`` strings, independently re-checkable and portable to
other repositories.  Outside the calibrated point range every lookup fails --
no extrapolation is ever performed.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Tuple

from .geometry import bin_coverages, coverage_in_bounds
from .limits import COMPLETE, INCOMPLETE, MAX_CALIBRATION_POINTS
from .rationals import Number, as_rational, rational_to_str


class CalibrationError(ValueError):
    """Raised for malformed calibration data or out-of-range lookups."""


@dataclass(frozen=True)
class Segment:
    """One piecewise-linear piece between two calibration points."""

    c0: Fraction
    c1: Fraction
    e0: Fraction
    e1: Fraction

    @property
    def slope(self) -> Fraction:
        """dE/dc (energy per channel)."""
        return (self.e1 - self.e0) / (self.c1 - self.c0)

    def channel_to_energy(self, c: Fraction) -> Fraction:
        return self.e0 + self.slope * (c - self.c0)

    def energy_to_channel(self, e: Fraction) -> Fraction:
        return self.c0 + (e - self.e0) / self.slope


class CalibrationProduct:
    """Immutable, hashable piecewise-linear channel/energy calibration."""

    def __init__(self, device_id: str, version: int,
                 points: Sequence[Tuple[Number, Number]]):
        if len(points) < 2:
            raise CalibrationError("at least two calibration points are needed")
        if len(points) > MAX_CALIBRATION_POINTS:
            raise CalibrationError(
                f"at most {MAX_CALIBRATION_POINTS} calibration points allowed")
        pts = [(as_rational(c), as_rational(e)) for c, e in points]
        for i in range(1, len(pts)):
            if pts[i][0] <= pts[i - 1][0]:
                raise CalibrationError("channel values must be strictly increasing")
            if pts[i][1] <= pts[i - 1][1]:
                raise CalibrationError("energy values must be strictly increasing")
        self.device_id = str(device_id)
        self.version = int(version)
        self.points: List[Tuple[Fraction, Fraction]] = pts
        self.segments: List[Segment] = [
            Segment(pts[i][0], pts[i + 1][0], pts[i][1], pts[i + 1][1])
            for i in range(len(pts) - 1)
        ]
        self._c = [p[0] for p in pts]
        self._e = [p[1] for p in pts]
        self.product_id = self._compute_id()

    # ---- ranges -------------------------------------------------------
    @property
    def channel_range(self) -> Tuple[Fraction, Fraction]:
        return self._c[0], self._c[-1]

    @property
    def energy_range(self) -> Tuple[Fraction, Fraction]:
        return self._e[0], self._e[-1]

    # ---- segment lookup ----------------------------------------------
    def _segment_for_channel(self, c: Fraction) -> Segment:
        if c < self._c[0] or c > self._c[-1]:
            raise CalibrationError(
                f"channel {c} outside calibrated range "
                f"[{self._c[0]}, {self._c[-1]}]")
        idx = bisect.bisect_right(self._c, c) - 1
        if idx == len(self.segments):
            idx -= 1
        return self.segments[idx]

    def _segment_for_energy(self, e: Fraction) -> Segment:
        if e < self._e[0] or e > self._e[-1]:
            raise CalibrationError(
                f"energy {e} outside calibrated range "
                f"[{self._e[0]}, {self._e[-1]}]")
        idx = bisect.bisect_right(self._e, e) - 1
        if idx == len(self.segments):
            idx -= 1
        return self.segments[idx]

    # ---- point projections -------------------------------------------
    def channel_to_energy(self, c: Number) -> Fraction:
        c = as_rational(c)
        return self._segment_for_channel(c).channel_to_energy(c)

    def energy_to_channel(self, e: Number) -> Fraction:
        e = as_rational(e)
        return self._segment_for_energy(e).energy_to_channel(e)

    # ---- interval projections ----------------------------------------
    def channel_interval_to_energy(self, c_lo: Number, c_hi: Number
                                   ) -> Tuple[Fraction, Fraction]:
        """Project a channel interval onto energy; range checked, no extrap."""
        c_lo, c_hi = as_rational(c_lo), as_rational(c_hi)
        if c_hi <= c_lo:
            raise CalibrationError("interval must be non-empty (lo < hi)")
        return (self.channel_to_energy(c_lo),
                self.channel_to_energy(c_hi))

    def energy_interval_to_channel(self, e_lo: Number, e_hi: Number
                                   ) -> Tuple[Fraction, Fraction]:
        """Back-project a half-open energy window onto channel coordinates."""
        e_lo, e_hi = as_rational(e_lo), as_rational(e_hi)
        if e_hi <= e_lo:
            raise CalibrationError("energy window must be non-empty (lo < hi)")
        return (self.energy_to_channel(e_lo),
                self.energy_to_channel(e_hi))

    # ---- window back-projection used by the repository ---------------
    def backproject_window(self, e_lo: Number, e_hi: Number, n_bins: int
                           ) -> Dict:
        """Back-project an energy window into per-bin rational coverages.

        Returns a dict with status COMPLETE/INCOMPLETE.  A window is
        INCOMPLETE when it leaves the calibrated energy domain or covers any
        channel bin that the spectrum did not measure -- the unknown part is
        never silently treated as zero.
        """
        e_lo, e_hi = as_rational(e_lo), as_rational(e_hi)
        dom_lo, dom_hi = self.energy_range
        if e_lo < dom_lo or e_hi > dom_hi:
            return {
                "status": INCOMPLETE,
                "reason": "energy window outside calibrated energy domain",
                "energy_lo": e_lo,
                "energy_hi": e_hi,
                "channel_lo": None,
                "channel_hi": None,
                "coverage": [],
                "coverage_total": Fraction(0),
                "in_measured_bins": False,
            }
        c_lo, c_hi = self.energy_interval_to_channel(e_lo, e_hi)
        entries, total = bin_coverages(c_lo, c_hi, n_bins)
        in_bounds = coverage_in_bounds(entries, n_bins)
        return {
            "status": COMPLETE if in_bounds else INCOMPLETE,
            "reason": None if in_bounds else
            "window back-projects onto unmeasured channel bins "
            "(calibration/measurement gap)",
            "energy_lo": e_lo,
            "energy_hi": e_hi,
            "channel_lo": c_lo,
            "channel_hi": c_hi,
            "coverage": entries,
            "coverage_total": total,
            "in_measured_bins": in_bounds,
        }

    # ---- (de)serialization -------------------------------------------
    def _compute_id(self) -> str:
        body = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict:
        return {
            "device_id": self.device_id,
            "version": self.version,
            "points": [[rational_to_str(c), rational_to_str(e)]
                       for c, e in self.points],
            "segments": [
                {
                    "c0": rational_to_str(s.c0), "c1": rational_to_str(s.c1),
                    "e0": rational_to_str(s.e0), "e1": rational_to_str(s.e1),
                    "slope": rational_to_str(s.slope),
                } for s in self.segments
            ],
            "channel_range": [rational_to_str(self._c[0]),
                              rational_to_str(self._c[-1])],
            "energy_range": [rational_to_str(self._e[0]),
                             rational_to_str(self._e[-1])],
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_json_with_id(self, indent: Optional[int] = 2) -> str:
        """Portable document including the content-derived product id."""
        return json.dumps({"product_id": self.product_id,
                           **self.to_dict()}, indent=indent,
                          sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict) -> "CalibrationProduct":
        product = cls(
            data["device_id"], data["version"],
            [(as_rational(c), as_rational(e)) for c, e in data["points"]],
        )
        # Re-check embedded recomputable pieces and the identity hash.
        for embedded, rebuilt in zip(data["segments"], product.segments):
            if (as_rational(embedded["slope"]) != rebuilt.slope or
                    as_rational(embedded["c0"]) != rebuilt.c0):
                raise CalibrationError("product segment check failed")
        if data.get("product_id") is not None and \
                data["product_id"] != product.product_id:
            raise CalibrationError("product id does not match content")
        return product

    @classmethod
    def from_json(cls, text: str) -> "CalibrationProduct":
        return cls.from_dict(json.loads(text))
