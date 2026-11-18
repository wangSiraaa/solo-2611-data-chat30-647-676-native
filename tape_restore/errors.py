"""Typed failures for the tape device, catalog and restore layers.

Every failure carries a stable ``code`` so the CLI and the tests can assert on
machine-readable outcomes instead of matching prose.
"""

from __future__ import annotations


class TapeRestoreError(Exception):
    code = "error"

    def __init__(self, message: str, **detail: object) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


class CatalogError(TapeRestoreError):
    code = "catalog_invalid"


class MediaError(TapeRestoreError):
    """Synthetic tape image is truncated, malformed or unreadable."""

    code = "media_invalid"


class DeviceError(TapeRestoreError):
    code = "device_error"


class DriveEmpty(DeviceError):
    code = "drive_empty"


class DriveOccupied(DeviceError):
    code = "drive_occupied"


class NoSuchDrive(DeviceError):
    code = "no_such_drive"


class NoSuchVolume(DeviceError):
    code = "no_such_volume"


class VolumeInUse(DeviceError):
    code = "volume_in_use"


class VolumeNotMounted(DeviceError):
    code = "volume_not_mounted"


class EndOfTape(DeviceError):
    code = "end_of_tape"


class BlockOutOfRange(DeviceError):
    code = "block_out_of_range"


class StagingError(TapeRestoreError):
    code = "staging_error"


class QuotaExceeded(StagingError):
    code = "quota_exceeded"


class RestoreError(TapeRestoreError):
    code = "restore_error"


class UnsupportedCatalogVersion(RestoreError):
    code = "unsupported_catalog_version"


class InvalidState(RestoreError):
    code = "invalid_state"


class StateConflict(RestoreError):
    code = "state_conflict"


class FragmentValidationError(RestoreError):
    code = "fragment_failed"
