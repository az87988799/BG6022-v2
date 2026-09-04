"""Stable application and storage errors for the durable kernel."""

from __future__ import annotations

from typing import ClassVar

from orca_agent.domain.errors import OrcaAgentError
from orca_agent.domain.json_types import JsonValue


class ApplicationError(OrcaAgentError):
    """Base for safe errors that can cross the application boundary."""

    code: ClassVar[str] = "application_error"


class StorageError(ApplicationError):
    code = "storage_error"


class RunNotFoundError(ApplicationError):
    code = "run_not_found"


class RunAlreadyExistsError(ApplicationError):
    code = "run_already_exists"


class EffectNotFoundError(ApplicationError):
    code = "effect_not_found"


class EffectRunMismatchError(ApplicationError):
    code = "effect_run_mismatch"


class EffectStatusError(ApplicationError):
    code = "effect_status_invalid"


class RevisionConflictError(ApplicationError):
    code = "revision_conflict"


class DuplicateCommandConflictError(ApplicationError):
    code = "duplicate_command_conflict"


class InvalidTransitionError(ApplicationError):
    code = "invalid_transition"


class InterruptAlreadyPendingError(ApplicationError):
    code = "interrupt_already_pending"


class InterruptNotPendingError(ApplicationError):
    code = "interrupt_not_pending"


class InterruptExpiredError(ApplicationError):
    code = "interrupt_expired"


class InterruptNotExpiredError(ApplicationError):
    code = "interrupt_not_expired"


class InvalidInterruptExpiryError(ApplicationError):
    code = "invalid_interrupt_expiry"


class LeaseLostError(ApplicationError):
    code = "lease_lost"


class MigrationDriftError(StorageError):
    code = "migration_drift"


class MigrationVersionError(StorageError):
    code = "migration_version_error"


class StateIntegrityError(StorageError):
    code = "state_integrity_error"


class StorageBusyError(StorageError):
    code = "storage_busy"


def safe_error_details(error: ApplicationError) -> dict[str, JsonValue]:
    """Copy only the structured details exposed by an application error."""

    return dict(error.details)


__all__ = [
    "ApplicationError",
    "DuplicateCommandConflictError",
    "EffectNotFoundError",
    "EffectRunMismatchError",
    "EffectStatusError",
    "InterruptAlreadyPendingError",
    "InterruptExpiredError",
    "InvalidInterruptExpiryError",
    "InterruptNotExpiredError",
    "InterruptNotPendingError",
    "InvalidTransitionError",
    "LeaseLostError",
    "MigrationDriftError",
    "MigrationVersionError",
    "RevisionConflictError",
    "RunAlreadyExistsError",
    "RunNotFoundError",
    "StateIntegrityError",
    "StorageBusyError",
    "StorageError",
    "safe_error_details",
]
