"""Domain-specific exceptions."""

from __future__ import annotations


class GcpOptError(Exception):
    """Base class for all package errors."""


class InfeasibleTargetError(GcpOptError, ValueError):
    """A requested performance target cannot be met by any disk size."""

    def __init__(self, message: str, *, target: str, limit_kind: str) -> None:
        super().__init__(message)
        self.target = target
        self.limit_kind = limit_kind


class SnapshotIntegrityError(GcpOptError):
    """A snapshot file's payload does not match its recorded digest."""


class SnapshotNotFoundError(GcpOptError, FileNotFoundError):
    """A required snapshot file is missing."""


class ApiError(GcpOptError):
    """A Google Cloud REST call failed."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class ApiAuthError(ApiError):
    """A Google Cloud REST call failed authentication or authorization."""


class UnknownMachineTypeError(GcpOptError, KeyError):
    """A machine type is absent from the loaded catalog."""


class PriceUnavailableError(GcpOptError, LookupError):
    """No price is available for the requested region/disk combination."""


class UnmodeledDiskKindError(GcpOptError, ValueError):
    """A disk kind has no verified performance model in this package."""
