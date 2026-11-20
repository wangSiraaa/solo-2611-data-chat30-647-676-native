"""Independent transfer response of a ventilation catalog.

A published catalog exposes its linear response without any observation
chamber, experiment or visit records:

* ``concentration_operator(t)`` -- ``exp(K t)``: unit initial concentration in
  room ``j`` to concentration in room ``i`` after ``t`` seconds.
* ``source_operator(a, b)`` -- maps a constant source vector (mg/s) over
  ``[a, b)`` to the concentration change at ``b``:
  ``int_a^b exp(K (b - u)) diag(1/V) du``.
* ``exhaust_operator(t)`` -- cumulative fractional mass exhausted to outdoors
  per source/initial room.

These are pure functions of the topology and flows, recomputable from the
published identity, and use the matrix exponential (so they tolerate singular
``K`` such as closed components).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.linalg import expm

from .catalog import Catalog


@dataclass
class TransferResponse:
    room_names: list[str]
    identities: str
    K: np.ndarray
    V: np.ndarray
    exhaust: np.ndarray

    def concentration_operator(self, t: float) -> np.ndarray:
        """``G(t) = exp(K t)`` maps initial c to c(t) with zero sources."""
        return expm(self.K * t)

    def source_operator(self, a: float, b: float) -> np.ndarray:
        """Map a constant source (mg/s) held on ``[a, b)`` to c(b) delta.

        With zero initial concentration, ``c(b) = H(a,b) @ s``.
        """
        n = self.K.shape[0]
        m = 2 * n
        # augmented generator [[K, diag(1/V)], [0, 0]]
        A = np.zeros((m, m))
        A[:n, :n] = self.K
        A[:n, n:] = np.diag(1.0 / self.V)
        F = expm(A * (b - a))
        return F[:n, n:]

    def exhaust_operator(self, t: float) -> np.ndarray:
        """Cumulative outdoor mass per room from unit initial c at t=0.

        Returns an ``n x n`` matrix; column ``j`` is the mass exhausted from
        each room after ``t`` seconds given ``c_j(0)=1`` and all else zero.
        """
        n = self.K.shape[0]
        m = 2 * n + 1
        B = np.zeros((m, m))
        B[:n, :n] = self.K
        B[n : 2 * n, :n] = np.diag(self.exhaust)
        F = expm(B * t)
        return F[n : 2 * n, :n]

    def response_table(self, t: float) -> dict:
        G = self.concentration_operator(t)
        E = self.exhaust_operator(t)
        conc = {}
        for j, src in enumerate(self.room_names):
            for i, dst in enumerate(self.room_names):
                conc[f"{src}->{dst}"] = float(G[i, j])
        exh = {}
        for j, src in enumerate(self.room_names):
            for i, dst in enumerate(self.room_names):
                exh[f"{src}->OUT[{dst}]"] = float(E[i, j])
        return {
            "t": t,
            "catalog_identity": self.identities,
            "rooms": self.room_names,
            "concentration_from_initial": conc,
            "cumulative_outdoor_mass_from_initial": exh,
        }


def transfer_response(catalog: Catalog) -> TransferResponse:
    catalog.require_published()
    m = catalog.matrices()
    return TransferResponse(
        room_names=list(m["names"]),
        identities=catalog.identity,
        K=m["K"],
        V=m["V"],
        exhaust=m["exhaust"],
    )
