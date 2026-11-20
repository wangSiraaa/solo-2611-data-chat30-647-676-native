"""polcal -- independent channel calibration service and observation warehouse.

Only the limited synthetic model is implemented:

    s = (I + Q*cos(2*theta) + U*sin(2*theta)) / 2
    y = g*s + b,   g > 0

This package does not connect to instruments, makes no real physical
measurement, and its numbers carry no medical or astronomical meaning.
"""

from .model import (
    DEFAULT_CONFIG,
    DISCLAIMER,
    MODEL_STATEMENT,
    MODEL_VERSION,
    ValidationError,
    apply_channel_correction,
    canonical_theta,
    ideal_s,
    validate_payload,
)
from .calibration import CalibrationStore
from .warehouse import ObservationWarehouse

__all__ = [
    "DEFAULT_CONFIG",
    "DISCLAIMER",
    "MODEL_STATEMENT",
    "MODEL_VERSION",
    "ValidationError",
    "apply_channel_correction",
    "canonical_theta",
    "ideal_s",
    "validate_payload",
    "CalibrationStore",
    "ObservationWarehouse",
]

__version__ = "0.1.0"
