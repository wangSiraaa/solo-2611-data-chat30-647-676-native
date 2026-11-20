"""Synthetic example scenarios for VentNet.

Each builder returns a published :class:`Catalog` and a fully specified
experiment payload ready for ``Store.import_experiment``.  All data is
synthetic and for demonstration/testing only.
"""

from __future__ import annotations

from .catalog import Catalog

# A small, human-readable naming convention for scenario ids.
SCENARIOS = {}


def _register(name):
    def deco(fn):
        SCENARIOS[name] = fn
        return fn

    return deco


@_register("two_room_exchange")
def two_room_exchange():
    """Two symmetric rooms exchanging air; source in A, tracer reaches B."""
    c = Catalog("two-room-exchange")
    c.add_room("A", 30).add_room("B", 20)
    c.add_supply("A", "0.10").add_supply("B", "0.10")
    c.add_transfer("A", "B", "0.05").add_transfer("B", "A", "0.05")
    c.add_exhaust("A", "0.10").add_exhaust("B", "0.10")
    c.publish()
    exp = {
        "initial_concentration": {"A": 0, "B": 0},
        "source_events": [
            {"time": 0, "room": "A", "strength": "1.0"},
            {"time": 60, "room": "A", "strength": 0},
        ],
        "window": [0, 300],
        "visits": [
            {"visit_id": "a1", "person": "alice", "room": "A", "start": 0, "end": 120},
            {"visit_id": "b1", "person": "alice", "room": "B", "start": 120, "end": 300},
            {"visit_id": "a2", "person": "bob", "room": "A", "start": 30, "end": 90},
        ],
    }
    return c, exp


@_register("closed_zero_exhaust")
def closed_zero_exhaust():
    """Two rooms exchanging with zero outdoor flow: singular K, mass conserved."""
    c = Catalog("closed-zero-exhaust")
    c.add_room("A", 25).add_room("B", 25)
    c.add_transfer("A", "B", "0.20").add_transfer("B", "A", "0.20")
    c.publish()
    exp = {
        "initial_concentration": {"A": "5.0", "B": 0},
        "source_events": [],
        "window": [0, 500],
        "visits": [
            {"visit_id": "a1", "person": "carol", "room": "A", "start": 0, "end": 500},
        ],
    }
    return c, exp


@_register("piecewise_source")
def piecewise_source():
    """Single room with a source switched on/off/on, half-open segments."""
    c = Catalog("piecewise-source")
    c.add_room("A", 12)
    c.add_supply("A", "0.30").add_exhaust("A", "0.30")
    c.publish()
    exp = {
        "initial_concentration": {"A": 0},
        "source_events": [
            {"time": 0, "room": "A", "strength": "2.0"},
            {"time": 100, "room": "A", "strength": 0},
            {"time": 200, "room": "A", "strength": "1.0"},
            {"time": 300, "room": "A", "strength": 0},
        ],
        "window": [0, 400],
        "visits": [
            {"visit_id": "v1", "person": "dave", "room": "A", "start": 50, "end": 250},
        ],
    }
    return c, exp


@_register("boundary_flow")
def boundary_flow():
    """Supply/exhaust at the boundary: one room gets clean outdoor supply.

    Room A has a source and exhausts; room B is supplied only by outdoor air
    and by transfer from A.
    """
    c = Catalog("boundary-flow")
    c.add_room("A", 40).add_room("B", 40)
    c.add_supply("B", "0.20")          # clean make-up air to B
    c.add_exhaust("A", "0.10")
    c.add_transfer("A", "B", "0.10")   # A -> B carries tracer
    c.add_transfer("B", "A", "0.20")   # B -> A return
    c.add_exhaust("B", "0.10")
    c.publish()
    exp = {
        "initial_concentration": {"A": 0, "B": 0},
        "source_events": [
            {"time": 0, "room": "A", "strength": "3.0"},
            {"time": 150, "room": "A", "strength": 0},
        ],
        "window": [0, 400],
        "visits": [
            {"visit_id": "a1", "person": "erin", "room": "A", "start": 0, "end": 200},
            {"visit_id": "b1", "person": "erin", "room": "B", "start": 200, "end": 400},
        ],
    }
    return c, exp


@_register("legit_flow_correction")
def legit_flow_correction():
    """A balanced catalog plus a balanced flow correction that changes B.

    Returns a catalog *assembler* rather than a single catalog: the caller
    receives the base catalog and the balanced revision changes.
    """
    c = Catalog("legit-flow-correction")
    c.add_room("A", 30).add_room("B", 30)
    c.add_supply("A", "0.10").add_supply("B", "0.10")
    c.add_transfer("A", "B", "0.02").add_transfer("B", "A", "0.02")
    c.add_exhaust("A", "0.10").add_exhaust("B", "0.10")
    c.publish()
    exp = {
        "initial_concentration": {"A": 0, "B": 0},
        "source_events": [
            {"time": 0, "room": "A", "strength": "2.0"},
            {"time": 120, "room": "A", "strength": 0},
        ],
        "window": [0, 300],
        "visits": [
            {"visit_id": "b1", "person": "frank", "room": "B", "start": 0, "end": 300},
        ],
    }
    # Balanced correction: raise exchange in both directions (A<->B) equally.
    revision = {("A", "B"): "0.08", ("B", "A"): "0.08"}
    return c, exp, revision


def build(name: str):
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; choose from {sorted(SCENARIOS)}")
    return SCENARIOS[name]()
