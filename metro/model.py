"""Shared, immutable data structures used across the pipeline.

``Stop`` deliberately carries ``seq`` (the stop-sequence identity) rather than
``stop_id``: a train may call at the same station several times (loop lines),
and the same station has different stop identities on different trains.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class StationInfo:
    station: str
    name: str
    entry_walk: int
    exit_walk: int
    min_transfer: int


@dataclass(frozen=True, slots=True)
class Stop:
    train_id: str
    seq: int
    station: str
    arr_ts: int
    dep_ts: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_id: int
    name: str
    stations: dict[str, StationInfo]
    trains: dict[str, tuple[Stop, ...]]

    def stop(self, train_id: str, seq: int) -> Stop:
        return self.trains[train_id][seq - 1]

    def all_stops(self):
        for train_id, stops in self.trains.items():
            for stop in stops:
                yield stop

    @property
    def n_stops(self) -> int:
        return sum(len(s) for s in self.trains.values())


@dataclass(frozen=True, slots=True)
class GateEvent:
    event_id: str
    card_id: str
    station: str
    ts: int
    direction: str


@dataclass(frozen=True, slots=True)
class Leg:
    train_id: str
    board_seq: int
    alight_seq: int
    board_station: str
    alight_station: str
    board_dep: int
    alight_arr: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "train": self.train_id,
            "board_seq": self.board_seq,
            "alight_seq": self.alight_seq,
            "board_station": self.board_station,
            "alight_station": self.alight_station,
            "board_dep": self.board_dep,
            "alight_arr": self.alight_arr,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """A complete, self-contained explanation of one trip.

    A direct plan has a single leg.  A transfer plan has two legs, at most one
    transfer, on two different trains, and stores the transfer wait evidence.
    """

    plan_id: str
    trip_id: str
    kind: str  # DIRECT | TRANSFER
    legs: tuple[Leg, ...]
    transfer_station: str | None = None
    transfer_min: int | None = None
    transfer_arrive: int | None = None
    transfer_depart: int | None = None
    transfer_slack: int | None = None
    sort_key: str = ""
    signature: str = ""

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    @property
    def primary_train(self) -> str:
        return self.legs[0].train_id

    def interval_uses(self) -> list[tuple[int, str, int, int]]:
        """(leg_no, train, from_seq, to_seq) for every adjacent-station hop."""
        uses = []
        for leg_no, leg in enumerate(self.legs, start=1):
            uses.append((leg_no, leg.train_id, leg.board_seq, leg.alight_seq))
        return uses

    def signature_tuple(self) -> tuple:
        """Identity of the *ride* implied by the plan.

        Two explanations that board/alight the same trains at the same stop
        sequences are the same plan even if transfer sub-times differ.
        """
        return tuple(
            (leg.train_id, leg.board_seq, leg.alight_seq) for leg in self.legs
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "kind": self.kind,
            "legs": [leg.as_dict() for leg in self.legs],
            "transfer": (
                {
                    "station": self.transfer_station,
                    "min_transfer": self.transfer_min,
                    "arrive": self.transfer_arrive,
                    "depart": self.transfer_depart,
                    "slack": self.transfer_slack,
                }
                if self.kind == "TRANSFER"
                else None
            ),
        }


@dataclass
class Trip:
    trip_id: str
    card_id: str
    entry_event_id: str
    exit_event_id: str
    entry_station: str
    entry_ts: int
    exit_station: str
    exit_ts: int
    plans: list[Plan] = field(default_factory=list)
    diag: dict[str, Any] = field(default_factory=dict)

    @property
    def explainable(self) -> bool:
        return bool(self.plans)

    @property
    def status(self) -> str:
        return "EXPLAINABLE" if self.plans else "UNEXPLAINABLE"

    def best_plan(self) -> Plan | None:
        """A deterministic, *labelled* representative — never a claim of truth."""
        if not self.plans:
            return None
        return min(self.plans, key=lambda p: (p.sort_key, p.plan_id))
