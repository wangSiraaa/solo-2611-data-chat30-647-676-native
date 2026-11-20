"""Two-station differential displacement warehouse (exact, no floating point).

Model (given in full by the task)
---------------------------------
For each epoch ``t`` in the requested window and each channel ``j in {1,2}``::

    z_j(t) = corrected_phase_rover_j(t) - corrected_phase_base_j(t)
    lambda_j * (z_j(t) - n_j) = d(t)

where ``n1, n2`` are integers *fixed over the whole window* and ``d(t)`` is a
rational displacement that must satisfy ``Dlo <= d(t) <= Dhi`` for every ``t``.
``lambda_j > 0``.

Because a single ``d(t)`` is shared by both channels at a given epoch, the two
channels must agree exactly::

    lambda_1 * (z_1(t) - n_1) = lambda_2 * (z_2(t) - n_2)

for every ``t``.  Equivalently, with ``A = lambda_1`` and ``B = lambda_2``::

    A * n_1 - B * n_2 = A * z_1(t) - B * z_2(t)  =:  K(t)

The right-hand side must be the *same* rational ``K`` for all epochs (otherwise
no integer pair can reconcile the channels).  Clearing denominators turns
``A n1 - B n2 = K`` into a single linear Diophantine equation whose integer
solutions form a one-parameter arithmetic progression

    (n1, n2) = (n1_0 + b' s, n2_0 + a' s),   s in Z,

with ``a' = A/g``, ``b' = B/g`` (after integer scaling) and ``g`` a gcd.  The
closed displacement bound then restricts ``s`` to an integer interval

    s in [ ceil(max_t (base_t - Dhi)/step),  floor(min_t (base_t - Dlo)/step) ].

Every integer ``s`` in that interval is a distinct feasible ``(n1, n2)`` with a
fully determined displacement sequence ``d(t) = base_t - step*s``.  This is why
the exact solution count, the sorted solution list, and *per-epoch* displacement
extrema over the whole feasible set can all be produced without enumerating the
entire (possibly huge) set: counts and extremes come straight from the interval
endpoints.

Status
------
* ``INCOMPLETE``   -- a requested epoch is unusable for some station/channel, or
                      a series changes arc id inside the window.  Bad epochs are
                      never dropped to shrink the window.
* ``INCONSISTENT`` -- data cannot be explained by any integer pair (cross-channel
                      mismatch, no Diophantine solution, or empty ``s`` range).
* ``AMBIGUOUS``    -- more than one feasible ``(n1, n2)``.
* ``UNIQUE``       -- exactly one feasible ``(n1, n2)``.

The warehouse only ever reads the public :class:`ArcProduct` corrected phases; it
never touches raw observations and never rewrites a phase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from math import gcd
from typing import Optional

from .arc import ArcProduct
from .rational import Q, ceil_frac, floor_frac

__all__ = [
    "DiffTask",
    "DiffSolution",
    "DiffResult",
    "solve_differential",
    "DifferentialWarehouse",
    "OUTPUT_CAP",
]

#: Maximum number of ``(n1, n2)`` pairs listed in a result payload.
OUTPUT_CAP = 1000


@dataclass(frozen=True)
class DiffTask:
    rover: str
    base: str
    channel1: int
    channel2: int
    lambda1: Fraction
    lambda2: Fraction
    win_lo: int
    win_hi: int
    dlo: Fraction
    dhi: Fraction

    def __post_init__(self) -> None:
        if self.lambda1 <= 0 or self.lambda2 <= 0:
            raise ValueError("wavelengths must be positive")
        if self.win_lo > self.win_hi:
            raise ValueError("window lo must be <= hi")
        if self.dlo > self.dhi:
            raise ValueError("displacement bound lo must be <= hi")

    @classmethod
    def make(
        cls,
        rover: str,
        base: str,
        channel1: int,
        channel2: int,
        lambda1: object,
        lambda2: object,
        win_lo: int,
        win_hi: int,
        dlo: object,
        dhi: object,
    ) -> "DiffTask":
        return cls(
            rover=rover,
            base=base,
            channel1=int(channel1),
            channel2=int(channel2),
            lambda1=Q(lambda1),
            lambda2=Q(lambda2),
            win_lo=int(win_lo),
            win_hi=int(win_hi),
            dlo=Q(dlo),
            dhi=Q(dhi),
        )

    @property
    def epochs(self) -> list[int]:
        return list(range(self.win_lo, self.win_hi + 1))

    def series(self) -> list[tuple[str, int]]:
        return [
            (self.rover, self.channel1),
            (self.rover, self.channel2),
            (self.base, self.channel1),
            (self.base, self.channel2),
        ]


@dataclass(frozen=True)
class DiffSolution:
    n1: int
    n2: int
    displacements: tuple[tuple[int, Fraction], ...]  # (epoch, d) sorted

    def displacement_dict(self) -> dict[int, Fraction]:
        return dict(self.displacements)

    def to_dict(self) -> dict:
        return {
            "n1": self.n1,
            "n2": self.n2,
            "displacements": [
                {"epoch": e, "d": f"{d.numerator}/{d.denominator}"}
                for e, d in self.displacements
            ],
        }


@dataclass
class DiffResult:
    status: str
    reason: str
    task: DiffTask
    solutions: list[DiffSolution] = field(default_factory=list)   # capped at OUTPUT_CAP
    solution_count: int = 0          # exact count of feasible (n1,n2)
    truncated: bool = False
    n1_range: Optional[tuple[int, int]] = None   # inclusive independent bound range
    n2_range: Optional[tuple[int, int]] = None
    per_epoch_extremes: dict[int, tuple[Fraction, Fraction]] = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    result_id: Optional[int] = None

    @property
    def is_solved(self) -> bool:
        return self.status in ("UNIQUE", "AMBIGUOUS")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "task": {
                "rover": self.task.rover,
                "base": self.task.base,
                "channel1": self.task.channel1,
                "channel2": self.task.channel2,
                "lambda1": _s(self.task.lambda1),
                "lambda2": _s(self.task.lambda2),
                "win_lo": self.task.win_lo,
                "win_hi": self.task.win_hi,
                "dlo": _s(self.task.dlo),
                "dhi": _s(self.task.dhi),
            },
            "solution_count": self.solution_count,
            "truncated": self.truncated,
            "n1_range": self.n1_range,
            "n2_range": self.n2_range,
            "per_epoch_d_extremes": {
                str(e): {"min": _s(mn), "max": _s(mx)}
                for e, (mn, mx) in sorted(self.per_epoch_extremes.items())
            },
            "solutions": [s.to_dict() for s in self.solutions],
            "provenance": self.provenance,
            "result_id": self.result_id,
        }


def _s(v: Fraction) -> str:
    return f"{v.numerator}/{v.denominator}"


def _incomplete(
    task: DiffTask, reason: str, provenance: dict
) -> DiffResult:
    return DiffResult(status="INCOMPLETE", reason=reason, task=task, provenance=provenance)


def solve_differential(
    task: DiffTask,
    products: dict[tuple[str, int], ArcProduct],
    *,
    provenance: Optional[dict] = None,
) -> DiffResult:
    """Exactly solve one differential task from public arc products.

    ``products`` maps ``(station, channel)`` to the public product to consume.
    Only corrected phases from these products are read.
    """
    provenance = dict(provenance or {})
    epochs = task.epochs

    # ---- availability & arc-identity checks (never drop epochs) -------------
    for key in task.series():
        prod = products.get(key)
        if prod is None:
            return _incomplete(
                task, f"missing product for station/channel {key}", provenance
            )
        arc_ids = set()
        for e in epochs:
            st = prod.state(e)
            if st is None or not st.usable:
                why = "absent" if st is None else ("no lock" if not st.lock else "unusable")
                return _incomplete(
                    task,
                    f"{key} not usable at epoch {e} ({why})",
                    provenance,
                )
            arc_ids.add(st.arc_id)
        if len(arc_ids) != 1:
            return _incomplete(
                task,
                f"{key} changes arc id within window {sorted(arc_ids)}",
                provenance,
            )
        provenance[f"arc_id:{key[0]}/ch{key[1]}"] = next(iter(arc_ids))
        provenance[f"product_id:{key[0]}/ch{key[1]}"] = prod.product_id

    # ---- build z1(t), z2(t) --------------------------------------------------
    z1: dict[int, Fraction] = {}
    z2: dict[int, Fraction] = {}
    for e in epochs:
        r1 = products[(task.rover, task.channel1)].corrected_at(e)
        b1 = products[(task.base, task.channel1)].corrected_at(e)
        r2 = products[(task.rover, task.channel2)].corrected_at(e)
        b2 = products[(task.base, task.channel2)].corrected_at(e)
        z1[e] = r1 - b1
        z2[e] = r2 - b2

    A, B = task.lambda1, task.lambda2

    # ---- independent per-n integer ranges from the closed bound -------------
    # Dlo <= A*(z1 - n1) <= Dhi  ==>  z1 - Dhi/A <= n1 <= z1 - Dlo/A, all t.
    n1_lo = max(z1[e] - task.dhi / A for e in epochs)
    n1_hi = min(z1[e] - task.dlo / A for e in epochs)
    n2_lo = max(z2[e] - task.dhi / B for e in epochs)
    n2_hi = min(z2[e] - task.dlo / B for e in epochs)
    n1_range = (ceil_frac(n1_lo), floor_frac(n1_hi))
    n2_range = (ceil_frac(n2_lo), floor_frac(n2_hi))

    union_result = DiffResult(
        status="INCONSISTENT",
        reason="",
        task=task,
        n1_range=n1_range,
        n2_range=n2_range,
        provenance=provenance,
    )

    # The bound applies to each channel independently (d = lambda_j*(z_j-n_j)),
    # so an empty independent integer range already makes the task impossible.
    if n1_range[0] > n1_range[1] or n2_range[0] > n2_range[1]:
        union_result.reason = (
            "no integer n inside the closed displacement bound for a channel "
            f"(n1 in {n1_range}, n2 in {n2_range})"
        )
        return union_result

    # ---- cross-channel consistency: K(t) = A z1 - B z2 must be constant -----
    Ks = {A * z1[e] - B * z2[e] for e in epochs}
    if len(Ks) != 1:
        union_result.reason = (
            "cross-channel inconsistency: A*z1(t)-B*z2(t) varies over the window "
            "(channels cannot share one displacement with fixed integers)"
        )
        return union_result
    K = next(iter(Ks))

    # ---- integer-scaled Diophantine: a*n1 - b*n2 = m ------------------------
    L = _lcm(A.denominator, B.denominator)
    a = A * L
    b = B * L
    assert a.denominator == 1 and b.denominator == 1
    a = a.numerator
    b = b.numerator
    m = K * L
    if m.denominator != 1:
        union_result.reason = (
            "no integer solution: cleared K is not integral, K lies outside the "
            "lattice generated by the wavelengths"
        )
        return union_result
    m = m.numerator
    g = gcd(a, b)
    if m % g != 0:
        union_result.reason = (
            f"no integer solution: gcd({a},{b})={g} does not divide {m}"
        )
        return union_result

    ap, bp, mp = a // g, b // g, m // g
    # n1 == mp * inv(ap) (mod bp); bp == 1 -> trivial residue 0
    if bp == 1:
        n1_0 = 0
    else:
        inv = pow(ap % bp, -1, bp)
        n1_0 = (mp * inv) % bp
    n2_0 = (a * n1_0 - m) // b  # exact by construction
    # general solution: n1 = n1_0 + bp*s, n2 = n2_0 + ap*s

    # ---- displacement bound -> s interval -----------------------------------
    step = A * bp  # positive; displacement shift per unit s
    base = {e: A * (z1[e] - n1_0) for e in epochs}
    s_low = ceil_frac(max((base[e] - task.dhi) / step for e in epochs))
    s_high = floor_frac(min((base[e] - task.dlo) / step for e in epochs))

    if s_low > s_high:
        union_result.reason = (
            "no feasible integer pair inside the closed displacement bound "
            f"[{_s(task.dlo)}, {_s(task.dhi)}]"
        )
        return union_result

    count = s_high - s_low + 1

    # per-epoch extrema over the full (possibly huge) feasible set
    extremes: dict[int, tuple[Fraction, Fraction]] = {}
    for e in epochs:
        # d = base[e] - step*s, decreasing in s
        d_at_high = base[e] - step * s_high
        d_at_low = base[e] - step * s_low
        extremes[e] = (d_at_high, d_at_low)  # (min, max)

    # ---- materialise up to OUTPUT_CAP solutions, sorted by (n1, n2) ---------
    listed: list[DiffSolution] = []
    limit = min(count, OUTPUT_CAP)
    for i in range(limit):
        s = s_low + i
        n1 = n1_0 + bp * s
        n2 = n2_0 + ap * s
        disps = tuple((e, base[e] - step * s) for e in epochs)
        listed.append(DiffSolution(n1=n1, n2=n2, displacements=disps))
    listed.sort(key=lambda sol: (sol.n1, sol.n2))

    status = "UNIQUE" if count == 1 else "AMBIGUOUS"
    if count == 1:
        reason = "exactly one integer pair consistent with all epochs and the bound"
    else:
        reason = (
            f"{count} integer pairs consistent; displacement bound and cross-channel "
            "geometry do not pin a single ambiguity"
        )

    return DiffResult(
        status=status,
        reason=reason,
        task=task,
        solutions=listed,
        solution_count=count,
        truncated=count > OUTPUT_CAP,
        n1_range=n1_range,
        n2_range=n2_range,
        per_epoch_extremes=extremes,
        provenance=provenance,
    )


def _lcm(x: int, y: int) -> int:
    if x == 0 or y == 0:
        return 0
    return abs(x * y) // gcd(x, y)


class DifferentialWarehouse:
    """Consumes public arc products (persisted or external) and solves tasks.

    The warehouse never reads raw observations and never edits a phase.  Given
    product ids it loads immutable snapshots, so a later correction to a jump
    event cannot retroactively change an already-solved task: re-running with the
    old product ids reproduces the old answer, while re-running against the
    latest product reflects the correction.
    """

    def __init__(self, store=None, arc_service=None) -> None:
        from .arc import ArcService  # local import: avoid hard circular dep

        self.store = store
        self.arc = arc_service if arc_service is not None else (
            ArcService(store) if store is not None else None
        )

    # -- product resolution ---------------------------------------------------
    def resolve_products(
        self,
        task: DiffTask,
        *,
        product_ids: Optional[dict[tuple[str, int], int]] = None,
        external: Optional[dict[tuple[str, int], ArcProduct]] = None,
    ) -> dict[tuple[str, int], ArcProduct]:
        """Load the four series' products: explicit external > ids > latest.

        An id pins an immutable snapshot (so a later correction cannot change an
        old answer).  Without an id the latest persisted product is loaded; if
        none is persisted the arc service builds one in memory at gap ``G = 1``.
        """
        out: dict[tuple[str, int], ArcProduct] = {}
        for key in task.series():
            if external and key in external:
                out[key] = external[key]
                continue
            if self.arc is None:
                raise KeyError(f"no product source available for {key}")
            if product_ids and key in product_ids:
                out[key] = self.arc.load(key[0], key[1], product_id=product_ids[key])
            else:
                out[key] = self.arc.load(key[0], key[1])
        return out

    def solve(
        self,
        task: DiffTask,
        *,
        products: Optional[dict[tuple[str, int], ArcProduct]] = None,
        product_ids: Optional[dict[tuple[str, int], int]] = None,
        external: Optional[dict[tuple[str, int], ArcProduct]] = None,
        persist: bool = False,
        task_id: Optional[int] = None,
    ) -> DiffResult:
        prods = products or self.resolve_products(
            task, product_ids=product_ids, external=external
        )
        provenance = {
            "consumed": {
                f"{k[0]}/ch{k[1]}": {
                    "product_id": p.product_id,
                    "event_level": p.event_level,
                    "obs_stamp": p.obs_stamp,
                    "external": p.external,
                }
                for k, p in prods.items()
            }
        }
        result = solve_differential(task, prods, provenance=provenance)
        if persist and self.store is not None:
            tid = task_id
            if tid is None:
                tid = self.store.create_task(
                    rover=task.rover,
                    base=task.base,
                    channel1=task.channel1,
                    channel2=task.channel2,
                    lambda1=task.lambda1,
                    lambda2=task.lambda2,
                    win_lo=task.win_lo,
                    win_hi=task.win_hi,
                    dlo=task.dlo,
                    dhi=task.dhi,
                )
            detail = json.dumps(result.to_dict(), sort_keys=True)
            rid = self.store.save_task_result(
                task_id=tid,
                provenance=json.dumps(provenance, sort_keys=True),
                status=result.status,
                n1=result.solutions[0].n1 if result.solution_count == 1 else None,
                n2=result.solutions[0].n2 if result.solution_count == 1 else None,
                solution_count=result.solution_count,
                truncated=result.truncated,
                detail=detail,
            )
            result.result_id = rid
        return result
