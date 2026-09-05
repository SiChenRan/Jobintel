"""Typed errors raised by JobIntel application and persistence boundaries."""

from __future__ import annotations


class JobIntelError(Exception):
    """Base class for expected JobIntel failures."""


class SchemaError(JobIntelError):
    """Base class for migration history or schema compatibility failures."""


class SchemaNotReadyError(SchemaError):
    """Raised when an operation requires migrations that are not applied."""


class MigrationHistoryError(SchemaError):
    """Raised when recorded migrations are unknown, edited, or out of order."""


class RepositoryError(JobIntelError):
    """Base class for repository validation and persistence failures."""


class EntityNotFoundError(RepositoryError):
    """Raised when a requested versioned domain entity does not exist."""


class PersistenceValidationError(RepositoryError):
    """Raised when an aggregate references data outside its persisted scope."""


class IdempotencyConflictError(RepositoryError):
    """Raised when a run ID is reused with a different analysis payload."""


class SeedDataError(JobIntelError):
    """Raised when JobIntel fixtures are internally inconsistent."""


class EvidenceSearchError(JobIntelError, ValueError):
    """Raised when an evidence search request has an invalid scope or query."""


class RadarCooldownError(JobIntelError):
    """Raised when a live radar check is requested before its safe interval."""
