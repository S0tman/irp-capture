"""Exception types for IRP integrity operations."""
from __future__ import annotations


class IntegrityError(Exception):
    """Base class for all integrity errors."""


class IntegrityDependencyError(IntegrityError):
    """A required optional dependency for integrity features is missing."""


class LedgerIntegrityError(IntegrityError):
    """The ledger could not be snapshotted because it is malformed."""


class SnapshotFormatError(IntegrityError):
    """A snapshot file is missing required fields or uses an unknown schema."""
