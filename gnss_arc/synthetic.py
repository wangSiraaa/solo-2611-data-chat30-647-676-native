"""Synthetic finite GNSS-like scenarios (no real protocol, no ephemeris).

Observation model for one station/channel::

    raw_phase(t) = clean_phase(t) + cumulative_declared_jump(t)

The arc service subtracts the cumulative declared jump, recovering
``clean_phase(t)``; the differential rover-base difference of clean phases is the
desired ``z_j(t)``.  Epochs are integers and every phase is an exact rational.
Nothing here implements a real GNSS protocol; "truth" is authored directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Optional

from .rational import Q
from .storage import Store

__all__ = ["Scenario", "build_same_wavelength", "build_dual_wavelength"]


@dataclass
class Scenario:
    store: Store
    stations: list[str]
    channels: list[int]
    epochs: list[int]
    truth: dict = field(default_factory=dict)


def _cumulative(slips: dict[int, int]) -> Callable[[int], int]:
    ordered = sorted(slips.items())

    def cum(t: int) -> int:
        return sum(j for (e, j) in ordered if e <= t)

    return cum


def _emit(
    store: Store,
    station: str,
    channel: int,
    epochs: list[int],
    clean: Callable[[int], Fraction],
    slips: dict[int, int],
    locks: dict[int, bool],
    missing: set[int],
) -> None:
    cum = _cumulative(slips)
    rows = []
    for t in epochs:
        if t in missing:
            continue
        rows.append(
            (station, channel, t, clean(t) + cum(t), locks.get(t, True))
        )
    store.put_observations(rows)
    for t in sorted(slips):
        store.add_jump(station, channel, t, slips[t])


def build_same_wavelength(
    *,
    station: str = "ROV",
    base: str = "BASE",
    epochs: int = 10,
    lam: object = Fraction(1, 100),
    displacement: object = 0,
    n1: int = 3,
    n2: int = 3,
    dlo: object = 0,
    dhi: object = Fraction(1, 10),
    lock_drop: Optional[int] = None,
    gap_break: Optional[int] = None,
    jump_at: Optional[int] = None,
    jump: int = 1,
) -> Scenario:
    """Same-wavelength scenario (channel 1 and 2 share ``lam``).

    With one wavelength the two channels carry no independent integer
    information, so several ``(n1, n2)`` usually remain feasible.
    """
    lam = Q(lam)
    d0 = Q(displacement)
    store = Store(":memory:")
    ep = list(range(1, epochs + 1))
    chans = [1, 2]
    slips = {jump_at: jump} if jump_at is not None else {}
    locks = {lock_drop: False} if lock_drop is not None else {}
    missing = {gap_break} if gap_break is not None else set()

    for ch, nn in ((1, n1), (2, n2)):
        z_val = d0 / lam + nn
        # rover carries z; base is the zero reference
        _emit(store, station, ch, ep, lambda t, v=z_val: v, slips, locks, missing)
        _emit(store, base, ch, ep, lambda t: Fraction(0), slips, locks, missing)

    return Scenario(
        store=store,
        stations=[station, base],
        channels=chans,
        epochs=ep,
        truth={
            "type": "same_wavelength",
            "lambda": lam,
            "lambda1": lam,
            "lambda2": lam,
            "n1": n1,
            "n2": n2,
            "d": {t: d0 for t in ep},
            "dlo": Q(dlo),
            "dhi": Q(dhi),
        },
    )


def build_dual_wavelength(
    *,
    station: str = "ROV",
    base: str = "BASE",
    epochs: int = 6,
    lam1: object = Fraction(1, 5),
    lam2: object = Fraction(1, 7),
    displacement: object = Fraction(1, 10),
    n1: int = 2,
    n2: int = 1,
    dlo: object = 0,
    dhi: object = Fraction(1, 2),
    lock_drop: Optional[int] = None,
    gap_break: Optional[int] = None,
    jump_at: Optional[int] = None,
    jump: int = 1,
) -> Scenario:
    """Dual-wavelength scenario; ``displacement`` may be a scalar or per-epoch list."""
    lam1, lam2 = Q(lam1), Q(lam2)
    ep = list(range(1, epochs + 1))
    if isinstance(displacement, (tuple, list)):
        dvals = [Q(x) for x in displacement]
        if len(dvals) != epochs:
            raise ValueError("per-epoch displacement length must equal epochs")
    else:
        dvals = [Q(displacement)] * epochs
    chans = [1, 2]
    store = Store(":memory:")
    slips = {jump_at: jump} if jump_at is not None else {}
    locks = {lock_drop: False} if lock_drop is not None else {}
    missing = {gap_break} if gap_break is not None else set()

    dmap = dict(zip(ep, dvals))
    _emit(
        store, station, 1, ep,
        lambda t: dmap[t] / lam1 + n1, slips, locks, missing,
    )
    _emit(store, base, 1, ep, lambda t: Fraction(0), slips, locks, missing)
    _emit(
        store, station, 2, ep,
        lambda t: dmap[t] / lam2 + n2, slips, locks, missing,
    )
    _emit(store, base, 2, ep, lambda t: Fraction(0), slips, locks, missing)

    return Scenario(
        store=store,
        stations=[station, base],
        channels=chans,
        epochs=ep,
        truth={
            "type": "dual_wavelength",
            "lambda1": lam1,
            "lambda2": lam2,
            "n1": n1,
            "n2": n2,
            "d": dmap,
            "dlo": Q(dlo),
            "dhi": Q(dhi),
        },
    )
