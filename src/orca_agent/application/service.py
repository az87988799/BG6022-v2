"""Application service that atomically turns typed commands into events."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from orca_agent.application.errors import (
    ApplicationError,
    DuplicateCommandConflictError,
    InterruptAlreadyPendingError,
    InterruptNotExpiredError,
    InterruptNotPendingError,
    InvalidInterruptExpiryError,
    InvalidTransitionError,
    RevisionConflictError,
    RunAlreadyExistsError,
    StorageError,
)
from orca_agent.domain.ids import EventId, InterruptId, new_id
from orca_agent.domain.json_types import JsonObject, thaw_json
from orca_agent.infrastructure.clock import Clock, SystemClock, format_utc
from orca_agent.infrastructure.repositories import RunSnapshot, StoredEvent
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import (
    CancelRun,
    Command,
    CreateRun,
    ExpireInterrupt,
    RecordEffectFailed,
    RecordEffectSucceeded,
    ReplaceInterrupt,
    RequestInterrupt,
    ResolveInterrupt,
)
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

    def expire_due(self, *, limit: int = 100) -> tuple[InterruptId, ...]:
        """Persist expiry for due interrupts without starting a background scheduler."""

        if limit < 1:
            return ()
        now = self.clock.now_utc()
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            if uow.interrupts is None:
                raise StorageError("interrupt repository is unavailable")
            due = uow.interrupts.due_ids(now=now, limit=limit)
            uow.commit()

        expired: list[InterruptId] = []
        for interrupt_id in due:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                uow.begin()
                if uow.runs is None or uow.interrupts is None:
                    uow.rollback()
                    raise StorageError("interrupt repositories are unavailable")
                record = uow.interrupts.get(interrupt_id)
                if record is None or record.status.value != "pending":
                    uow.commit()
                    continue
                snapshot = uow.runs.get(record.run_id)
                uow.commit()
            if snapshot is None or snapshot.state.pending_interrupt_id != interrupt_id:
                continue
            result = self.execute(
                ExpireInterrupt.create(
                    run_id=record.run_id,
                    expected_revision=snapshot.revision,
                    interrupt_id=interrupt_id,
                    requested_at_utc=now,
                )
            )
            if result.code == "interrupt_expired" and result.event_id is not None:
                expired.append(interrupt_id)
        return tuple(expired)

    def _execute_in_transaction(
        self,
        uow: SQLiteUnitOfWork,
        command: Command,
    ) -> ApplicationResult:
        if uow.runs is None or uow.events is None or uow.interrupts is None or uow.outbox is None:
            raise StorageError("unit of work repositories are unavailable")
        command_hash = command.command_hash()
        stored = uow.events.get_by_command_id(command.command_id)
        if stored is not None:
            return self._retry_or_conflict(uow, command, command_hash, stored)
        if isinstance(command, CreateRun):
            return self._create_run(uow, command, command_hash)
        current = uow.runs.require(command.run_id)
        if command.expected_revision != current.revision:
            raise RevisionConflictError(
                "expected revision does not match current revision",
                details={
                    "expected_revision": command.expected_revision,
                    "current_revision": current.revision,
                },
            )
        return self._update_run(uow, command, command_hash, current)

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

    def _update_run(
        self,
        uow: SQLiteUnitOfWork,
        command: Command,
        command_hash: str,
        current: RunSnapshot,
    ) -> ApplicationResult:
        if uow.runs is None or uow.events is None or uow.interrupts is None or uow.outbox is None:
            raise StorageError("unit of work repositories are unavailable")
        now = self.clock.now_utc()
        event_type: EventType
        payload: JsonObject
        accepted = True
        interrupt_id = None

        if isinstance(command, RequestInterrupt):
            if current.state.pending_interrupt_id is not None:
                raise InterruptAlreadyPendingError(
                    "run already has a pending interrupt",
                    details={"run_id": str(command.run_id)},
                )
            if current.state.status not in (RunStatus.CREATED, RunStatus.READY):
                raise InvalidTransitionError(
                    "interrupt request requires a created or ready run",
                )
            if command.expires_at_utc <= now:
                raise InvalidInterruptExpiryError("interrupt expiry must be in the future")
            event_type = EventType.INTERRUPT_REQUESTED
            payload = command.event_payload()
            interrupt_id = command.interrupt_id
        elif isinstance(command, ReplaceInterrupt):
            self._require_pending(current, command.old_interrupt_id)
            if uow.interrupts.get(command.old_interrupt_id) is None:
                raise InterruptNotPendingError(
                    "interrupt is not pending",
                    details={"interrupt_id": str(command.old_interrupt_id)},
                )
            if command.expires_at_utc <= now:
                raise InvalidInterruptExpiryError("interrupt expiry must be in the future")
            event_type = EventType.INTERRUPT_REPLACED
            payload = command.event_payload()
            interrupt_id = command.new_interrupt_id
        elif isinstance(command, ResolveInterrupt):
            self._require_pending(current, command.interrupt_id)
            record = uow.interrupts.get(command.interrupt_id)
            if record is None or record.status.value != "pending":
                raise InterruptNotPendingError(
                    "interrupt is not pending",
                    details={"interrupt_id": str(command.interrupt_id)},
                )
            interrupt_id = command.interrupt_id
            if now >= record.expires_at_utc:
                accepted = False
                event_type = EventType.INTERRUPT_EXPIRED
                payload = {
                    "interrupt_id": str(command.interrupt_id),
                    "expires_at_utc": format_utc(record.expires_at_utc),
                }
            else:
                event_type = EventType.INTERRUPT_RESOLVED
                payload = command.event_payload()
        elif isinstance(command, ExpireInterrupt):
            self._require_pending(current, command.interrupt_id)
            record = uow.interrupts.get(command.interrupt_id)
            if record is None or record.status.value != "pending":
                raise InterruptNotPendingError(
                    "interrupt is not pending",
                    details={"interrupt_id": str(command.interrupt_id)},
                )
            if now < record.expires_at_utc:
                raise InterruptNotExpiredError(
                    "interrupt deadline has not been reached",
                    details={"interrupt_id": str(command.interrupt_id)},
                )
            event_type = EventType.INTERRUPT_EXPIRED
            payload = {
                "interrupt_id": str(command.interrupt_id),
                "expires_at_utc": format_utc(record.expires_at_utc),
            }
            interrupt_id = command.interrupt_id
        elif isinstance(command, CancelRun):
            event_type = EventType.RUN_CANCELLED
            payload = command.event_payload()
            interrupt_id = current.state.pending_interrupt_id
        elif isinstance(command, RecordEffectSucceeded):
            event_type = EventType.EFFECT_SUCCEEDED
            payload = command.event_payload()
        elif isinstance(command, RecordEffectFailed):
            event_type = EventType.EFFECT_DEAD_LETTERED
            payload = command.event_payload()
        else:
            raise InvalidTransitionError(
                "command is not enabled in the current kernel stage",
                details={"command_type": command.command_type.value},
            )

        return self._persist_transition(
            uow=uow,
            command=command,
            command_hash=command_hash,
            current=current,
            event_type=event_type,
            payload=payload,
            accepted=accepted,
            interrupt_id=interrupt_id,
            occurred_at_utc=now,
        )

    def _require_pending(self, current: RunSnapshot, interrupt_id: object) -> None:
        if current.state.status is not RunStatus.WAITING_FOR_INPUT:
            raise InterruptNotPendingError(
                "run has no pending interrupt",
                details={"run_id": str(current.run_id)},
            )
        if current.state.pending_interrupt_id != interrupt_id:
            raise InterruptNotPendingError(
                "interrupt ID is not the run's pending interrupt",
                details={"interrupt_id": str(interrupt_id)},
            )

    def _persist_transition(
        self,
        *,
        uow: SQLiteUnitOfWork,
        command: Command,
        command_hash: str,
        current: RunSnapshot,
        event_type: EventType,
        payload: JsonObject,
        accepted: bool,
        interrupt_id: object | None,
        occurred_at_utc: datetime,
    ) -> ApplicationResult:
        if uow.runs is None or uow.events is None or uow.interrupts is None or uow.outbox is None:
            raise StorageError("unit of work repositories are unavailable")
        event_id = new_id(EventId)
        placeholder = ApplicationResult.accepted_result(
            code=event_type.value,
            run_id=command.run_id,
            revision=current.revision + 1,
            status=current.state.status,
            event_id=event_id,
        )
        event = KernelEvent.create(
            event_id=event_id,
            command_id=command.command_id,
            command_type=command.command_type,
            run_id=command.run_id,
            sequence_no=current.revision + 1,
            expected_revision=current.revision,
            event_type=event_type,
            payload=payload,
            result=placeholder,
            occurred_at_utc=occurred_at_utc,
        )
        transition = reduce_event(current.state, event)
        result = ApplicationResult(
            accepted=accepted,
            code=transition.outcome.code,
            run_id=command.run_id,
            revision=current.revision + 1,
            status=transition.next_status,
            event_id=event_id,
            interrupt_id=interrupt_id,
            details=transition.outcome.details,
        )
        event = KernelEvent.create(
            event_id=event.event_id,
            command_id=event.command_id,
            command_type=event.command_type,
            run_id=event.run_id,
            sequence_no=event.sequence_no,
            expected_revision=event.expected_revision,
            event_type=event.event_type,
            payload=payload,
            result=result,
            occurred_at_utc=event.occurred_at_utc,
            recorded_at_utc=event.recorded_at_utc,
            engine_version=event.engine_version,
            schema_version=event.schema_version,
        )
        if not uow.runs.compare_and_swap(
            run_id=command.run_id,
            expected_revision=current.revision,
            state=transition.next_state,
            event_id=event_id,
            updated_at_utc=occurred_at_utc,
        ):
            latest = uow.runs.require(command.run_id)
            raise RevisionConflictError(
                "expected revision was not current",
                details={
                    "expected_revision": current.revision,
                    "current_revision": latest.revision,
                },
            )
        uow.events.append(event, command_hash=command_hash)
        uow.interrupts.apply_operations(event=event, operations=transition.interrupt_operations)
        uow.outbox.register_effects(
            event=event,
            run_id=command.run_id,
            effects=transition.effects,
            available_at_utc=occurred_at_utc,
            created_at_utc=occurred_at_utc,
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
