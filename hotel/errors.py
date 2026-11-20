"""Domain exceptions shared by both modules."""

from __future__ import annotations


class HotelError(Exception):
    """Base class for expected domain failures."""


class ValidationError(HotelError, ValueError):
    pass


class NotFound(HotelError, KeyError):
    pass


class AlreadyExists(HotelError):
    pass


class StaleRevision(HotelError):
    """A revision older than the current head was submitted."""


class RevisionConflict(HotelError):
    """The same revision was resubmitted with different content."""


class RecordRevoked(HotelError):
    """A terminal (cancelled/revoked) record cannot be brought back."""


class ImmutableProduct(HotelError):
    pass


class InfeasiblePlan(HotelError):
    pass


class ProductMismatch(HotelError):
    """An assignment does not belong to the product it claims to consume."""
