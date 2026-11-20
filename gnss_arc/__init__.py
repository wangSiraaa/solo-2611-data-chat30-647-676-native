"""Independent continuous-arc service and two-station differential warehouse.

Public surface:

* :class:`gnss_arc.arc.ArcService` / :class:`~gnss_arc.arc.ArcProduct`
  -- versioned public arc products and independent range queries.
* :class:`gnss_arc.differential.DifferentialWarehouse` /
  :func:`~gnss_arc.differential.solve_differential`
  -- exact integer-ambiguity differential solving.
* :class:`gnss_arc.storage.Store` -- SQLite persistence.
* :func:`gnss_arc.rational.Q` -- exact ``Fraction`` conversion.
"""

from .rational import Q
from .storage import Store
from .arc import (
    ArcService,
    ArcProduct,
    ArcSegment,
    EpochState,
    RangeQuery,
    CUT_REASONS,
)
from .differential import (
    DiffTask,
    DiffSolution,
    DiffResult,
    DifferentialWarehouse,
    solve_differential,
    OUTPUT_CAP,
)

__all__ = [
    "Q",
    "Store",
    "ArcService",
    "ArcProduct",
    "ArcSegment",
    "EpochState",
    "RangeQuery",
    "CUT_REASONS",
    "DiffTask",
    "DiffSolution",
    "DiffResult",
    "DifferentialWarehouse",
    "solve_differential",
    "OUTPUT_CAP",
]
