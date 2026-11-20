"""Domain errors for the cold-chain packing / transport services."""


class ColdChainError(Exception):
    """Base class for all domain errors."""


class PackingInfeasibleError(ColdChainError):
    """No feasible packing exists for the given items and box types."""

    def __init__(self, message: str, unmet: list[str] | None = None):
        super().__init__(message)
        self.unmet = unmet or []


class PlanValidationError(ColdChainError):
    """An explicit packing plan failed validation at sealing time."""

    def __init__(self, message: str, violations: list[str] | None = None):
        super().__init__(message)
        self.violations = violations or []


class StalePlanError(ColdChainError):
    """The plan references item versions that no longer exist (dims changed)."""


class NotFoundError(ColdChainError):
    """Referenced entity does not exist."""


class TransportInfeasibleError(ColdChainError):
    """No feasible vehicle assignment; carries a structured report."""

    def __init__(self, message: str, report: "object"):
        super().__init__(message)
        self.report = report


class VersionConflictError(ColdChainError):
    """Optimistic-concurrency check failed (expected version mismatch)."""


class InvalidStateError(ColdChainError):
    """Operation not allowed in the current lifecycle state."""


class ReservationConflictError(ColdChainError):
    """Another active plan already reserves some of the boxes or shifts."""

    def __init__(self, message: str, boxes: list[str] | None = None,
                 shifts: list[str] | None = None):
        super().__init__(message)
        self.boxes = boxes or []
        self.shifts = shifts or []
