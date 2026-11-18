"""票联服务与运输收入事实仓（虚构规则，整数分，无真实航空/支付系统连接）。"""
from .allocation import allocate, allocate_values, AllocItem
from .reconcile import reconcile, ReconciliationError
from .revenue import (
    RevenueWarehouse,
    RevenueError,
    InvalidSnapshot,
    EventConflict,
    evaluate_facet,
    compute_transport_bases,
    DECISION_RECOGNIZED,
    DECISION_PENDING_OPEN,
    DECISION_TERMINAL_NOT_USED,
    DIAG_MISSING_EVIDENCE,
    DIAG_SEGMENT_MISMATCH,
    DIAG_DUPLICATE_CONFLICT,
    DIAG_UNMATCHED_EVIDENCE,
    DIAG_EVIDENCE_ON_INACTIVE,
)
from .ticketing import (
    TicketingService,
    TicketingError,
    RequestConflict,
    InvalidOperation,
    LimitExceeded,
    replay_journal,
)

__all__ = [
    "allocate", "allocate_values", "AllocItem",
    "TicketingService", "TicketingError", "RequestConflict",
    "InvalidOperation", "LimitExceeded", "replay_journal",
    "RevenueWarehouse", "RevenueError", "InvalidSnapshot", "EventConflict",
    "evaluate_facet", "compute_transport_bases",
    "DECISION_RECOGNIZED", "DECISION_PENDING_OPEN", "DECISION_TERMINAL_NOT_USED",
    "DIAG_MISSING_EVIDENCE", "DIAG_SEGMENT_MISMATCH",
    "DIAG_DUPLICATE_CONFLICT", "DIAG_UNMATCHED_EVIDENCE",
    "DIAG_EVIDENCE_ON_INACTIVE",
    "reconcile", "ReconciliationError",
]
