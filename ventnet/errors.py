"""Exception hierarchy for VentNet."""


class VentNetError(Exception):
    """Base class for all VentNet errors."""


class ValidationError(VentNetError):
    """Input is structurally or semantically invalid."""


class BalanceError(ValidationError):
    """A room violates conservation of volumetric flow."""


class ConflictError(ValidationError):
    """Person movement records conflict (e.g. simultaneous rooms)."""


class NotFoundError(VentNetError):
    """A requested catalog/experiment/record does not exist."""


class LimitError(ValidationError):
    """A hard product limit (rooms, events, visits, duration) is exceeded."""
