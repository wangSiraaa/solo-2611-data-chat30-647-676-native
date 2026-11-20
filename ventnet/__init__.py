"""VentNet: multi-zone ventilation catalog + tracer observation chamber.

Synthetic, educational data-engineering implementation.  Not for health,
safety, or building-compliance decisions.
"""

from .errors import (
    BalanceError,
    ConflictError,
    LimitError,
    NotFoundError,
    ValidationError,
    VentNetError,
)
from .rational import OUT, parse_rational, rational_str
from .catalog import Catalog, Edge, catalog_identity
from .solver import Timeline, build_augmented
from .experiment import Experiment, SourceEvent, Visit, run_experiment
from .transfer import TransferResponse, transfer_response
from . import db as db

__all__ = [
    "BalanceError",
    "ConflictError",
    "LimitError",
    "NotFoundError",
    "ValidationError",
    "VentNetError",
    "OUT",
    "parse_rational",
    "rational_str",
    "Catalog",
    "Edge",
    "catalog_identity",
    "Timeline",
    "build_augmented",
    "Experiment",
    "SourceEvent",
    "Visit",
    "run_experiment",
    "TransferResponse",
    "transfer_response",
    "db",
]
