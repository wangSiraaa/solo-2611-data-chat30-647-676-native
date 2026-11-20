"""Continuous-arc service: build versioned public arc products from raw data.

The service turns per-(station, channel) raw observations plus explicit integer
cycle-slip events into a *public product* that the differential warehouse is
allowed to consume.  A product exposes, for every epoch it covers:

* the corrected phase ``raw_phase - cumulative_jump`` (exact rational),
* whether the observation is usable (``lock`` true and present),
* the continuous-arc id,
* why a new arc starts there and which jump event caused it.

Arcs are cut whenever:

* the observation is unusable (``lock == false``) -- reason ``NO_LOCK``;
* an explicit jump event lands on or before the next usable epoch -- ``JUMP``;
* the epoch spacing between consecutive usable epochs exceeds ``G`` -- ``GAP``;
* an integer epoch is missing from the feed -- ``NO_DATA``.

No displacement, baseline or wavelength enters here: the arc service knows
nothing about the differential task.

Products are immutable.  Correcting a jump event means adding a new (or
superseding an old) event and rebuilding, which yields a new product id.  Every
result records the product ids it consumed so historical tasks keep the numbers
they were computed with.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Optional

from .rational import Q
from .storage import Store

__all__ = [
    "EpochState",
    "ArcSegment",
    "ArcProduct",
    "RangeQuery",
    "ArcService",
    "CUT_REASONS",
]

#: Canonical cut-reason vocabulary published with every arc.
CUT_REASONS = ("FEED_START", "NO_LOCK", "JUMP", "GAP", "END_OF_DATA")


@dataclass(frozen=True)
class EpochState:
    """Published state of one epoch of one station-channel product."""

    epoch: int
    raw_phase: Optional[Fraction]     # None when no observation exists
    lock: bool
    cumulative_jump: int              # applied correction at this epoch
    corrected_phase: Optional[Fraction]
    usable: bool
    arc_id: Optional[int]             # None exactly when unusable
    start_reason: Optional[str]       # cut reason if this epoch begins an arc
    source_event: Optional[int]       # jump event id behind a JUMP cut

    def to_dict(self) -> dict:
        return {
            "epoch": self.epoch,
            "raw_phase": _frac_str(self.raw_phase),
            "lock": self.lock,
            "cumulative_jump": self.cumulative_jump,
            "corrected_phase": _frac_str(self.corrected_phase),
            "usable": self.usable,
            "arc_id": self.arc_id,
            "start_reason": self.start_reason,
            "source_event": self.source_event,
        }


@dataclass(frozen=True)
class ArcSegment:
    """A maximal run of consecutive usable epochs sharing one arc id."""

    arc_id: int
    start_epoch: int
    end_epoch: int
    n_epochs: int
    start_reason: str
    end_reason: str
    source_event: Optional[int]

    def to_dict(self) -> dict:
        return {
            "arc_id": self.arc_id,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
            "n_epochs": self.n_epochs,
            "start_reason": self.start_reason,
            "end_reason": self.end_reason,
            "source_event": self.source_event,
        }


@dataclass(frozen=True)
class ArcProduct:
    """An immutable public arc product for one station and channel."""

    station: str
    channel: int
    gap: int
    event_level: int                  # max active jump event id at build time
    obs_stamp: str                    # fingerprint of the observations consumed
    epochs: dict[int, EpochState]
    segments: tuple[ArcSegment, ...]
    jump_stamp: str = ""              # fingerprint of the active jump set used
    product_id: Optional[int] = None
    external: bool = False

    # ------------------------------------------------------------- accessors
    def state(self, epoch: int) -> Optional[EpochState]:
        return self.epochs.get(int(epoch))

    def usable_at(self, epoch: int) -> bool:
        st = self.epochs.get(int(epoch))
        return bool(st is not None and st.usable)

    def arc_id_at(self, epoch: int) -> Optional[int]:
        st = self.epochs.get(int(epoch))
        return None if st is None or not st.usable else st.arc_id

    def corrected_at(self, epoch: int) -> Optional[Fraction]:
        st = self.epochs.get(int(epoch))
        return None if st is None else st.corrected_phase

    def epochs_in_range(self, lo: int, hi: int) -> list[EpochState]:
        return [self.epochs[e] for e in sorted(self.epochs) if lo <= e <= hi]

    def segments_in_range(self, lo: int, hi: int) -> list[ArcSegment]:
        return [
            s
            for s in self.segments
            if s.start_epoch <= hi and s.end_epoch >= lo
        ]

    def arc_ids(self, lo: int, hi: int) -> set[int]:
        return {
            s.arc_id for s in self.segments if s.start_epoch <= hi and s.end_epoch >= lo
        }

    def range_query(self, lo: int, hi: int) -> "RangeQuery":
        if lo > hi:
            raise ValueError("range lo must be <= hi")
        epochs = self.epochs_in_range(lo, hi)
        segments = self.segments_in_range(lo, hi)
        return RangeQuery(
            station=self.station,
            channel=self.channel,
            product_id=self.product_id,
            lo=lo,
            hi=hi,
            epochs=epochs,
            segments=segments,
            arc_ids=sorted({s.arc_id for s in segments}),
        )

    # --------------------------------------------------------- serialisation
    def to_dict(self) -> dict:
        return {
            "station": self.station,
            "channel": self.channel,
            "gap": self.gap,
            "event_level": self.event_level,
            "obs_stamp": self.obs_stamp,
            "jump_stamp": self.jump_stamp,
            "product_id": self.product_id,
            "external": self.external,
            "epochs": [self.epochs[e].to_dict() for e in sorted(self.epochs)],
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ArcProduct":
        epochs = {}
        for row in data["epochs"]:
            st = EpochState(
                epoch=int(row["epoch"]),
                raw_phase=_frac_or_none(row["raw_phase"]),
                lock=bool(row["lock"]),
                cumulative_jump=int(row["cumulative_jump"]),
                corrected_phase=_frac_or_none(row["corrected_phase"]),
                usable=bool(row["usable"]),
                arc_id=row["arc_id"],
                start_reason=row["start_reason"],
                source_event=row["source_event"],
            )
            epochs[st.epoch] = st
        segs = tuple(
            ArcSegment(
                arc_id=int(s["arc_id"]),
                start_epoch=int(s["start_epoch"]),
                end_epoch=int(s["end_epoch"]),
                n_epochs=int(s["n_epochs"]),
                start_reason=s["start_reason"],
                end_reason=s["end_reason"],
                source_event=s["source_event"],
            )
            for s in data["segments"]
        )
        return cls(
            station=data["station"],
            channel=int(data["channel"]),
            gap=int(data["gap"]),
            event_level=int(data["event_level"]),
            obs_stamp=str(data.get("obs_stamp", "")),
            epochs=epochs,
            segments=segs,
            jump_stamp=str(data.get("jump_stamp", "")),
            product_id=data.get("product_id"),
            external=bool(data.get("external", True)),
        )


@dataclass(frozen=True)
class RangeQuery:
    """Result of an independent arc-range query for one station-channel."""

    station: str
    channel: int
    product_id: Optional[int]
    lo: int
    hi: int
    epochs: list[EpochState]
    segments: list[ArcSegment]
    arc_ids: list[int]

    @property
    def is_single_arc(self) -> bool:
        return len(self.arc_ids) == 1

    def to_dict(self) -> dict:
        return {
            "station": self.station,
            "channel": self.channel,
            "product_id": self.product_id,
            "lo": self.lo,
            "hi": self.hi,
            "arc_ids": self.arc_ids,
            "segments": [s.to_dict() for s in self.segments],
            "epochs": [e.to_dict() for e in self.epochs],
        }


# ----------------------------------------------------------------- helpers
def _frac_str(value: Optional[Fraction]) -> Optional[str]:
    if value is None:
        return None
    return f"{value.numerator}/{value.denominator}"


def _frac_or_none(text: Optional[str]) -> Optional[Fraction]:
    if text is None:
        return None
    return Q(text)


def _obs_stamp(station: str, channel: int, obs: dict[int, tuple[Fraction, bool]]) -> str:
    h = hashlib.sha256()
    h.update(f"{station}|{channel}\n".encode())
    for e in sorted(obs):
        phase, lock = obs[e]
        h.update(f"{e}|{phase.numerator}/{phase.denominator}|{int(lock)}\n".encode())
    return h.hexdigest()


# ----------------------------------------------------------------- service
class ArcService:
    """Builds and queries public arc products from a :class:`Store`."""

    def __init__(self, store: Store) -> None:
        self.store = store

    # -------------------------------------------------------------- building
    def build(
        self,
        station: str,
        channel: int,
        gap: int,
        *,
        persist: bool = True,
    ) -> ArcProduct:
        if gap < 1:
            raise ValueError("gap G must be a positive integer")

        obs = {
            e: (phase, lock)
            for (_s, _c, e, phase, lock) in self.store.iter_observations(
                station, channel
            )
        }
        jumps = self.store.active_jumps(station, channel)  # (id, epoch, jump)
        event_level = max((jid for jid, _, _ in jumps), default=0)
        jump_stamp = hashlib.sha256(
            repr([(jid, je, j) for (jid, je, j) in jumps]).encode()
        ).hexdigest()

        epochs: dict[int, EpochState] = {}
        segments: list[ArcSegment] = []

        # prefix of jumps sorted by epoch for cumulative correction
        jumps_sorted = sorted(jumps, key=lambda t: (t[1], t[0]))

        def cumulative(epoch: int) -> int:
            return sum(j for (_jid, je, j) in jumps_sorted if je <= epoch)

        def triggering_jump(lo_exclusive: int, hi_inclusive: int) -> Optional[int]:
            """Event id of the jump that starts a new arc at ``hi_inclusive``.

            Prefers a jump exactly on the new epoch; otherwise the latest jump in
            the open interval, breaking ties by smallest event id.
            """
            cand = [
                (je, jid)
                for (jid, je, _j) in jumps_sorted
                if lo_exclusive < je <= hi_inclusive
            ]
            if not cand:
                return None
            exact = [(je, jid) for (je, jid) in cand if je == hi_inclusive]
            pool = exact or cand
            # latest epoch, then smallest id -- deterministic
            pool.sort(key=lambda t: (-t[0], t[1]))
            return pool[0][1]

        arc_id = 0
        prev_usable: Optional[int] = None
        pending_reason: Optional[str] = None       # break reason set by unusable epoch
        pending_source: Optional[int] = None
        # track segment boundaries: (arc_id, start, reason, source)
        open_seg: Optional[dict] = None

        def close_seg(end_reason: str, source_event: Optional[int]) -> None:
            nonlocal open_seg
            if open_seg is None:
                return
            segments.append(
                ArcSegment(
                    arc_id=open_seg["arc_id"],
                    start_epoch=open_seg["start"],
                    end_epoch=open_seg["end"],
                    n_epochs=open_seg["n"],
                    start_reason=open_seg["reason"],
                    end_reason=end_reason,
                    source_event=open_seg["source"],
                )
            )
            open_seg = None

        for e in sorted(obs):
            raw, lock = obs[e]
            usable = bool(lock)
            cum = cumulative(e)
            corrected = raw - cum if raw is not None else None

            if not usable:
                # A present but unusable epoch breaks any running arc.
                if open_seg is not None:
                    close_seg("NO_LOCK", None)
                prev_usable = None
                pending_reason = "NO_LOCK"
                pending_source = None
                epochs[e] = EpochState(
                    epoch=e,
                    raw_phase=raw,
                    lock=False,
                    cumulative_jump=cum,
                    corrected_phase=corrected,
                    usable=False,
                    arc_id=None,
                    start_reason=None,
                    source_event=None,
                )
                continue

            # usable epoch: decide whether it continues the open arc
            start_reason: Optional[str] = None
            source: Optional[int] = None
            if prev_usable is None:
                # first usable after a break (or at feed start)
                if pending_reason is not None:
                    start_reason = pending_reason
                    source = pending_source
                else:
                    start_reason = "FEED_START"
                if open_seg is not None:  # safety; should already be closed
                    close_seg(start_reason, source)
                arc_id += 1
                open_seg = {
                    "arc_id": arc_id,
                    "start": e,
                    "end": e,
                    "n": 1,
                    "reason": start_reason,
                    "source": source,
                }
            else:
                reason = self._break_reason(
                    prev_usable, e, jumps_sorted, gap, triggering_jump
                )
                if reason is None:
                    # contiguous: extend open arc
                    assert open_seg is not None
                    open_seg["end"] = e
                    open_seg["n"] += 1
                else:
                    start_reason, source = reason
                    close_seg(start_reason, source)
                    arc_id += 1
                    open_seg = {
                        "arc_id": arc_id,
                        "start": e,
                        "end": e,
                        "n": 1,
                        "reason": start_reason,
                        "source": source,
                    }

            epochs[e] = EpochState(
                epoch=e,
                raw_phase=raw,
                lock=True,
                cumulative_jump=cum,
                corrected_phase=corrected,
                usable=True,
                arc_id=arc_id,
                start_reason=start_reason,
                source_event=source,
            )
            prev_usable = e
            pending_reason = None
            pending_source = None

        if open_seg is not None:
            close_seg("END_OF_DATA", None)

        product = ArcProduct(
            station=station,
            channel=int(channel),
            gap=int(gap),
            event_level=event_level,
            obs_stamp=_obs_stamp(station, channel, obs),
            jump_stamp=jump_stamp,
            epochs=epochs,
            segments=tuple(segments),
        )

        if persist:
            pid = self.store.save_arc_product(
                station=station,
                channel=int(channel),
                gap=int(gap),
                event_level=event_level,
                obs_stamp=product.obs_stamp,
                jump_stamp=product.jump_stamp,
                segments=[
                    {
                        "arc_id": s.arc_id,
                        "start_reason": s.start_reason,
                        "end_reason": s.end_reason,
                        "source_event": s.source_event,
                        "seg_order": i,
                        "start_epoch": s.start_epoch,
                        "end_epoch": s.end_epoch,
                        "n_epochs": s.n_epochs,
                    }
                    for i, s in enumerate(segments)
                ],
                epochs=[
                    {
                        "epoch": st.epoch,
                        "raw_phase": _frac_str(st.raw_phase),
                        "lock": 1 if st.lock else 0,
                        "cumulative_jump": st.cumulative_jump,
                        "corrected_phase": _frac_str(st.corrected_phase),
                        "usable": 1 if st.usable else 0,
                        "arc_id": st.arc_id,
                        "start_reason": st.start_reason,
                        "source_event": st.source_event,
                    }
                    for st in (product.epochs[e] for e in sorted(product.epochs))
                ],
            )
            # Re-attach the assigned id (frozen dataclass -> rebuild).
            product = ArcProduct(
                station=product.station,
                channel=product.channel,
                gap=product.gap,
                event_level=product.event_level,
                obs_stamp=product.obs_stamp,
                epochs=product.epochs,
                segments=product.segments,
                jump_stamp=product.jump_stamp,
                product_id=pid,
            )
        return product

    @staticmethod
    def _break_reason(
        prev_usable: int,
        epoch: int,
        jumps_sorted: list[tuple[int, int, int]],
        gap: int,
        triggering_jump,
    ) -> Optional[tuple[str, Optional[int]]]:
        """Return ``(reason, source_event)`` if the arc breaks, else ``None``.

        Precedence: JUMP (a jump on/before the new epoch) > GAP.  A missing
        epoch whose spacing is still ``<= G`` does *not* break the arc; the
        arc simply has fewer observed nodes.
        """
        jump_id = triggering_jump(prev_usable, epoch)
        if jump_id is not None:
            return ("JUMP", jump_id)
        if epoch - prev_usable > gap:
            return ("GAP", None)
        return None

    def build_all(
        self, gap: int, *, persist: bool = True
    ) -> dict[tuple[str, int], ArcProduct]:
        """Rebuild a product for every (station, channel) present in the store."""
        pairs = sorted(
            {(s, c) for (s, c, _e, _p, _l) in self.store.iter_observations()}
        )
        return {pair: self.build(pair[0], pair[1], gap, persist=persist) for pair in pairs}

    # -------------------------------------------------------------- querying
    def query(
        self,
        station: str,
        channel: int,
        lo: int,
        hi: int,
        *,
        product_id: Optional[int] = None,
    ) -> RangeQuery:
        """Independent interval query for any station-channel.

        If ``product_id`` is omitted the latest persisted product is used.
        """
        product = self.load(station, channel, product_id=product_id)
        return product.range_query(lo, hi)

    def load(
        self,
        station: str,
        channel: int,
        *,
        product_id: Optional[int] = None,
    ) -> ArcProduct:
        """Load a persisted, immutable product snapshot.

        With no ``product_id`` the latest persisted product for this
        station-channel is used.  If nothing has been persisted yet, build an
        in-memory product at gap ``G`` = 1 without persisting.
        """
        pid = product_id
        if pid is None:
            pid = self.store.latest_arc_product(station, channel)
            if pid is None:
                return self.build(station, channel, gap=1, persist=False)
        data = self.store.load_arc_product(pid)
        if data is None:
            raise KeyError(f"arc product {pid} not found")
        # Ensure the snapshot actually belongs to the requested station-channel.
        if data["station"] != station or int(data["channel"]) != int(channel):
            raise ValueError(
                f"product {pid} is for {data['station']}/ch{data['channel']}, "
                f"not {station}/ch{channel}"
            )
        return ArcProduct.from_dict(data)

    def is_stale(self, product: ArcProduct) -> bool:
        """True if the store's raw data or active jumps changed since build."""
        obs = {
            e: (phase, lock)
            for (_s, _c, e, phase, lock) in self.store.iter_observations(
                product.station, product.channel
            )
        }
        jumps = self.store.active_jumps(product.station, product.channel)
        event_level = max((jid for jid, _, _ in jumps), default=0)
        jump_stamp = hashlib.sha256(
            repr([(jid, je, j) for (jid, je, j) in jumps]).encode()
        ).hexdigest()
        return (
            product.event_level != event_level
            or product.jump_stamp != jump_stamp
            or product.obs_stamp != _obs_stamp(product.station, product.channel, obs)
        )

    def current_product(
        self, station: str, channel: int, gap: int, *, persist: bool = True
    ) -> ArcProduct:
        """Build (and optionally persist) the product reflecting current data."""
        return self.build(station, channel, gap, persist=persist)
