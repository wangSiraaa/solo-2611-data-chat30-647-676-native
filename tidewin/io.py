"""JSON (de)serialization helpers for channels, vessels, requests."""
from __future__ import annotations

from fractions import Fraction
from typing import Dict

from .rational import F
from .vault import Channel, Segment, Vessel


def segment_from_dict(d: Dict) -> Segment:
    rev = d.get("revision", "current")
    if rev in (None, "current", "latest", ""):
        rev = None
    else:
        rev = int(rev)
    return Segment(d["port"], rev, F(d["depth"]), F(d["duration"]))


def channel_from_dict(d: Dict) -> Channel:
    return Channel(d["name"], [segment_from_dict(s) for s in d["segments"]])


def vessel_from_dict(d: Dict) -> Vessel:
    return Vessel(F(d["draft"]), F(d["margin"]))


def request_from_dict(d: Dict) -> Dict:
    return {
        "id": d.get("id"),
        "channel": d["channel"],
        "vessel": vessel_from_dict(d),
        "S": F(d["S"]),
        "E": F(d["E"]),
        "note": d.get("note", ""),
    }
