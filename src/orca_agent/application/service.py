"""Application service that atomically turns typed commands into events."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from orca_agent.application.errors import (
    ApplicationError,
    DuplicateCommandConflictError,
    InvalidTransitionError,
    RunAlreadyExistsError,
    StorageError,
)
from orca_agent.domain.ids import EventId, new_id
from orca_agent.domain.json_types import thaw_json
from orca_agent.infrastructure.clock import Clock, SystemClock
from orca_agent.infrastructure.repositories import RunSnapshot, StoredEvent
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import Command, CreateRun
from orca_agent.orchestration.events import EventType, KernelEvent
from orca_agent.orchestration.reducer import reduce_event
from orca_agent.orchestration.replay import state_hash
from orca_agent.orchestration.state import RunStatus

from .results import ApplicationResult


class KernelApplicationService:
    """Handle one command per transaction and expose only typed results."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.database_path = database_path
        self.clock = clock or SystemClock()

    def execute(self, command: Command) -> ApplicationResult:
        """Execute a command, converting expected failures to safe results."""

        snapshot: RunSnapshot | None = None
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                uow.begin()
                snapshot = uow.runs.get(command.run_id) if uow.runs is not None else None
                result = self._execute_in_transaction(uow, command)
                uow.commit()
                return result
        except ApplicationError as error:
            return self._rejected(command, error, snapshot=snapshot)
        except sqlite3.IntegrityError:
            storage_error = StorageError("database invariant rejected the operation")
            return self._rejected(command, storage_error, snapshot=snapshot)
        except sqlite3.OperationalError as error:
            if "locked" in str(error).casefold() or "busy" in str(error).casefold():
                storage_error = StorageError("database is busy")
            else:
                storage_error = StorageError("database operation failed")
            return self._rejected(command, storage_error, snapshot=snapshot)

    handle = execute

    def _execute_in_transaction(
        self,
        uow: SQLiteUnitOfWork,
        command: Command,
    ) -> ApplicationResult:
        if uow.runs is None or uow.events is None or uow.outbox is None:
            raise StorageError("unit of work repositories are unavailable")
        command_hash = command.command_hash()
        stored = uow.events.get_by_command_id(command.command_id)
        if stored is not None:
            return self._retry_or_conflict(uow, command, command_hash, stored)
        if isinstance(command, CreateRun):
            return self._create_run(uow, command, command_hash)
        raise InvalidTransitionError(
            "command is not enabled in the current kernel stage",
            details={"command_type": command.command_type.value},
        )

    def _retry_or_conflict(
        self,
        uow: SQLiteUnitOfWork,
        command: Command,
        command_hash: str,
        stored: StoredEvent,
    ) -> ApplicationResult:
        if (
            stored.command_hash != command_hash
            or stored.event.run_id != command.run_id
            or stored.event.command_type is not command.command_type
        ):
            raise DuplicateCommandConflictError(
                "command ID is already bound to a different command",
                details={"command_id": str(command.command_id)},
            )
        try:
            return ApplicationResult.model_validate_json(
                json.dumps(thaw_json(stored.event.result), ensure_ascii=False)
            )
        except Exception as error:
            raise StorageError("stored application result is invalid") from error

    def _create_run(
        self,
        uow: SQLiteUnitOfWork,
        command: CreateRun,
        command_hash: str,
    ) -> ApplicationResult:
        if uow.runs is None or uow.events is None or uow.outbox is None:
            raise StorageError("unit of work repositories are unavailable")
        existing = uow.runs.get(command.run_id)
        if existing is not None:
            raise RunAlreadyExistsError(
                "run ID is already registered",
                details={"run_id": str(command.run_id)},
            )

        now = self.clock.now_utc()
        event_id = new_id(EventId)
        result = ApplicationResult.accepted_result(
            code="run_created",
            run_id=command.run_id,
            revision=1,
            status=RunStatus.CREATED,
            event_id=event_id,
        )
        event = KernelEvent.create(
            event_id=event_id,
            command_id=command.command_id,
            command_type=command.command_type,
            run_id=command.run_id,
            sequence_no=1,
            expected_revision=0,
            event_type=EventType.RUN_CREATED,
            payload=command.event_payload(),
            result=result,
            occurred_at_utc=now,
        )
        transition = reduce_event(None, event)
        snapshot = RunSnapshot(
            run_id=command.run_id,
            schema_version=event.schema_version,
            engine_version=event.engine_version,
            revision=1,
            state=transition.next_state,
            state_hash=state_hash(transition.next_state),
            last_event_id=event.event_id,
            created_at_utc=now,
            updated_at_utc=now,
        )
        uow.runs.insert(snapshot)
        uow.events.append(event, command_hash=command_hash)
        uow.outbox.register_effects(
            event=event,
            run_id=command.run_id,
            effects=transition.effects,
            available_at_utc=now,
            created_at_utc=now,
        )
        return result

    def _rejected(
        self,
        command: Command,
        error: ApplicationError,
        *,
        snapshot: RunSnapshot | None,
    ) -> ApplicationResult:
        status = snapshot.state.status if snapshot is not None else RunStatus.CREATED
        revision = snapshot.revision if snapshot is not None else 0
        return ApplicationResult.rejected_result(
            code=error.code,
            run_id=command.run_id,
            revision=revision,
            status=status,
            details=dict(error.details),
        )


ApplicationService = KernelApplicationService
KernelService = KernelApplicationService

__all__ = ["ApplicationService", "KernelApplicationService", "KernelService"]
