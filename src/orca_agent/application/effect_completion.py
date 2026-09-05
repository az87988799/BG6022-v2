"""Atomic, fenced completion of an authorized outbox effect."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from orca_agent.application.errors import (
    EffectCompletionConflictError,
    EffectDispatchBlockedError,
    LeaseLostError,
    RevisionConflictError,
    StateIntegrityError,
    StorageError,
)
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    CommandId,
    EffectId,
    EventId,
    completion_command_id,
    new_id,
)
from orca_agent.domain.json_types import JsonObject, thaw_json
from orca_agent.domain.p3 import P3WorkflowState
from orca_agent.infrastructure.clock import Clock, SystemClock
from orca_agent.infrastructure.command_receipts import CommandBindingKind, CommandReceipt
from orca_agent.infrastructure.outbox import (
    DispatchPermit,
    OutboxStatus,
)
from orca_agent.infrastructure.sqlite import SQLiteConnectionFactory, resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.codes import HandlerErrorCode, handler_error_message
from orca_agent.orchestration.dispatch_policy import (
    DEFAULT_EFFECT_REGISTRY,
    DispatchDecision,
    EffectRegistry,
    evaluate_dispatch,
)
from orca_agent.orchestration.effect_receipts import (
    EffectSuccessReceiptV1,
    parse_effect_success_receipt,
    receipt_json,
)
from orca_agent.orchestration.effects import EffectSpec
from orca_agent.orchestration.events import EventType, KernelEvent
from orca_agent.orchestration.p3_kernel import (
    P3KernelEvent,
    expected_p3_application_result,
    reduce_p3_event,
)
from orca_agent.orchestration.reducer import reduce_event
from orca_agent.orchestration.result_contract import expected_application_result
from orca_agent.orchestration.state import RunStatus

from .results import ApplicationResult

if TYPE_CHECKING:
    from orca_agent.infrastructure.repositories import RunSnapshot


@dataclass(frozen=True)
class EffectCompletionReport:
    """The durable outcome returned to a worker after completion."""

    effect_id: EffectId
    outcome: str
    attempt_count: int
    result: ApplicationResult | None = None


@dataclass(frozen=True)
class _CompletionInput:
    success: bool
    receipt: EffectSuccessReceiptV1 | None
    error_code: HandlerErrorCode | None


class EffectCompletionService:
    """Own the single transaction that finishes an authorized effect."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Clock | None = None,
        registry: EffectRegistry = DEFAULT_EFFECT_REGISTRY,
        max_attempts: int = 5,
        connection_factory: SQLiteConnectionFactory | None = None,
        successor_effect_factory: Callable[..., tuple[EffectSpec, ...]] | None = None,
        completion_metadata_factory: Callable[..., JsonObject] | None = None,
        completion_hook: Callable[..., None] | None = None,
    ) -> None:
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.database_path = resolve_database_path(database_path)
        self.clock = clock or SystemClock()
        self.registry = registry
        self.max_attempts = max_attempts
        self.connection_factory = connection_factory
        self.successor_effect_factory = successor_effect_factory
        self.completion_metadata_factory = completion_metadata_factory
        self.completion_hook = completion_hook

    def complete(self, permit: DispatchPermit, result: object) -> EffectCompletionReport:
        """Persist success, retry, or dead-letter for one exact permit."""

        completion = _normalize_completion_input(result)
        terminal = completion.success or permit.generation >= self.max_attempts
        outcome = "succeeded" if completion.success else ("dead_letter" if terminal else "retry")
        now = self.clock.now_utc()
        with SQLiteUnitOfWork(
            self.database_path,
            clock=self.clock,
            connection_factory=self.connection_factory,
        ) as uow:
            if (
                uow.runs is None
                or uow.events is None
                or uow.interrupts is None
                or uow.outbox is None
                or uow.command_receipts is None
            ):
                raise StorageError("kernel repositories are unavailable")
            uow.begin()
            snapshot = uow.runs.get_verified(
                permit.effect.run_id,
                uow.events,
                interrupts=uow.interrupts,
                outbox=uow.outbox,
            )
            current = uow.outbox.get(permit.effect.effect_id)
            if current is None:
                raise StateIntegrityError("effect was not found")

            if terminal:
                command_id = completion_command_id(
                    permit.effect.effect_id,
                    permit.generation,
                    outcome,
                )
                command_hash = _completion_command_hash(
                    command_id=command_id,
                    permit=permit,
                    outcome=outcome,
                    receipt=completion.receipt,
                    error_code=completion.error_code,
                )
                existing = uow.command_receipts.get(command_id)
                if existing is not None:
                    report = self._return_existing_terminal(
                        uow=uow,
                        permit=permit,
                        snapshot=snapshot,
                        current=current,
                        command_id=command_id,
                        command_hash=command_hash,
                        outcome=outcome,
                        receipt=existing,
                    )
                    uow.commit()
                    return report

            self._verify_active_permit(
                uow=uow,
                snapshot=snapshot,
                permit=permit,
                current=current,
                now=now,
            )
            if not terminal:
                updated = uow.outbox.retry_dispatch_in_transaction(
                    permit=permit,
                    now=now,
                    error_code=completion.error_code or HandlerErrorCode.HANDLER_FAILED,
                )
                uow.commit()
                return EffectCompletionReport(
                    effect_id=updated.effect_id,
                    outcome="retry",
                    attempt_count=updated.attempt_count,
                )

            event_type = (
                EventType.EFFECT_SUCCEEDED if completion.success else EventType.EFFECT_DEAD_LETTERED
            )
            if completion.success:
                if completion.receipt is None:
                    raise StateIntegrityError("successful completion is missing its receipt")
                payload: JsonObject = {
                    "effect_id": str(permit.effect.effect_id),
                    "result_summary": receipt_json(completion.receipt),
                }
                command_type = "record_effect_succeeded"
            else:
                if completion.error_code is None:
                    raise StateIntegrityError("failed completion is missing its error code")
                payload = {
                    "effect_id": str(permit.effect.effect_id),
                    "error_code": completion.error_code.value,
                    "error_message": handler_error_message(completion.error_code),
                }
                command_type = "record_effect_failed"
            if isinstance(snapshot.state, P3WorkflowState):
                payload["p3_stage"] = _p3_stage_for_effect(permit.effect.effect_type)
            successors: tuple[EffectSpec, ...] = ()
            if completion.success and self.completion_metadata_factory is not None:
                metadata = self.completion_metadata_factory(
                    uow=uow,
                    permit=permit,
                    completion=completion,
                    snapshot=snapshot,
                )
                if not isinstance(metadata, dict):
                    raise StateIntegrityError("completion metadata must be a JSON object")
                payload.update(metadata)
            if completion.success and self.successor_effect_factory is not None:
                successors = tuple(
                    self.successor_effect_factory(
                        permit=permit,
                        completion=completion,
                        snapshot=snapshot,
                    )
                )
                if successors:
                    payload["effects"] = [item.model_dump(mode="json") for item in successors]
            event, transition, application_result = _build_completion_event(
                snapshot=snapshot,
                events=uow.events,
                event_type=event_type,
                command_id=command_id,
                command_hash=command_hash,
                command_type=command_type,
                payload=payload,
                occurred_at_utc=now,
            )
            uow.events.append(event, command_hash=command_hash)
            uow.outbox.complete_terminal_in_transaction(
                permit=permit,
                status=(OutboxStatus.SUCCEEDED if completion.success else OutboxStatus.DEAD_LETTER),
                now=now,
                audit_event_id=event.event_id,
                result_summary=completion.receipt,
                error_code=completion.error_code,
            )
            if self.completion_hook is not None:
                self.completion_hook(
                    uow=uow,
                    permit=permit,
                    completion=completion,
                    event=event,
                    successors=successors,
                )
            uow.outbox.register_effects(
                event=event,
                run_id=permit.effect.run_id,
                effects=transition.effects,
                available_at_utc=now,
                created_at_utc=now,
            )
            uow.interrupts.apply_operations(
                event=event,
                operations=transition.interrupt_operations,
            )
            if transition.next_state.status.is_terminal:
                uow.outbox.cancel_pending_for_run(
                    run_id=permit.effect.run_id,
                    now=now,
                )
            if not uow.runs.compare_and_swap(
                run_id=permit.effect.run_id,
                expected_revision=snapshot.revision,
                state=transition.next_state,
                event_id=event.event_id,
                updated_at_utc=now,
            ):
                latest = uow.runs.require(permit.effect.run_id)
                raise RevisionConflictError(
                    "expected revision was not current",
                    details={
                        "expected_revision": snapshot.revision,
                        "current_revision": latest.revision,
                    },
                )
            uow.command_receipts.append_event(event=event, recorded_at_utc=now)
            uow.commit()
            return EffectCompletionReport(
                effect_id=permit.effect.effect_id,
                outcome=outcome,
                attempt_count=permit.generation,
                result=application_result,
            )

    def _verify_active_permit(
        self,
        *,
        uow: SQLiteUnitOfWork,
        snapshot: RunSnapshot,
        permit: DispatchPermit,
        current: object,
        now: datetime,
    ) -> None:
        if uow.outbox is None:
            raise StorageError("outbox repository is unavailable")
        uow.outbox.validate_dispatch_permit(permit=permit, now=now)
        if snapshot.revision < permit.run_revision:
            raise LeaseLostError("dispatch permit run revision is stale")
        if permit.policy_version != self.registry.policy_version:
            raise EffectDispatchBlockedError("dispatch policy version changed")
        if evaluate_dispatch(snapshot.state, current, self.registry) is not DispatchDecision.ALLOW:
            raise EffectDispatchBlockedError("effect is no longer allowed by dispatch policy")

    def _return_existing_terminal(
        self,
        *,
        uow: SQLiteUnitOfWork,
        permit: DispatchPermit,
        snapshot: RunSnapshot,
        current: object,
        command_id: CommandId,
        command_hash: str,
        outcome: str,
        receipt: CommandReceipt,
    ) -> EffectCompletionReport:
        if (
            receipt.command_id != command_id
            or receipt.command_hash != command_hash
            or receipt.run_id != permit.effect.run_id
            or receipt.binding_kind is not CommandBindingKind.EVENT
        ):
            raise EffectCompletionConflictError("completion receipt conflicts with the permit")
        if (
            current.completed_by_worker_id != permit.worker_id
            or current.terminal_generation != permit.generation
            or current.status.value != outcome
        ):
            raise LeaseLostError("outbox completion belongs to another worker or generation")
        if uow.events is None:
            raise StorageError("event repository is unavailable")
        event = uow.events.get(receipt.result_event_id)
        if (
            event is None
            or event.run_id != permit.effect.run_id
            or event.event_type.value
            not in {EventType.EFFECT_SUCCEEDED.value, EventType.EFFECT_DEAD_LETTERED.value}
            or event.payload.get("effect_id") != str(permit.effect.effect_id)
        ):
            raise StateIntegrityError("completion receipt is not an authoritative effect event")
        try:
            result = ApplicationResult.model_validate_json(
                json.dumps(thaw_json(event.result), ensure_ascii=False)
            )
        except Exception as error:
            raise StateIntegrityError("stored completion result is invalid") from error
        return EffectCompletionReport(
            effect_id=permit.effect.effect_id,
            outcome=outcome,
            attempt_count=permit.generation,
            result=result,
        )


def _normalize_completion_input(value: object) -> _CompletionInput:
    from orca_agent.infrastructure.worker import HandlerResult

    if not isinstance(value, HandlerResult):
        return _CompletionInput(False, None, HandlerErrorCode.INVALID_HANDLER_RESULT)
    success = getattr(value, "success", None)
    if type(success) is not bool:
        return _CompletionInput(False, None, HandlerErrorCode.INVALID_HANDLER_RESULT)
    if success:
        raw_receipt = getattr(value, "result_summary", None)
        try:
            receipt = parse_effect_success_receipt(
                {"receipt_schema": "effect-success/v1", "outcome_code": "completed"}
                if raw_receipt is None
                else raw_receipt
            )
        except ValueError:
            return _CompletionInput(False, None, HandlerErrorCode.INVALID_HANDLER_RESULT)
        return _CompletionInput(True, receipt, None)
    raw_code = getattr(value, "error_code", None)
    if raw_code is None:
        code = HandlerErrorCode.HANDLER_FAILED
    elif isinstance(raw_code, HandlerErrorCode):
        code = raw_code
    else:
        code = HandlerErrorCode.INVALID_HANDLER_RESULT
    return _CompletionInput(False, None, code)


def _completion_command_hash(
    *,
    command_id: CommandId,
    permit: DispatchPermit,
    outcome: str,
    receipt: EffectSuccessReceiptV1 | None,
    error_code: HandlerErrorCode | None,
) -> str:
    return sha256_hex(
        {
            "command_id": str(command_id),
            "command_type": (
                "record_effect_succeeded" if outcome == "succeeded" else "record_effect_failed"
            ),
            "effect_id": str(permit.effect.effect_id),
            "error_code": None if error_code is None else error_code.value,
            "generation": permit.generation,
            "receipt": None if receipt is None else receipt_json(receipt),
            "run_id": str(permit.effect.run_id),
            "run_revision": permit.run_revision,
        }
    )


def _p3_stage_for_effect(effect_type: str) -> str:
    try:
        return {
            "external.p3.dispatch_fake": "dispatch",
            "internal.p3.assess": "assess",
            "internal.p3.render_report": "render",
        }[effect_type]
    except KeyError as error:
        raise StateIntegrityError("unknown P3 completion effect type") from error


def _build_completion_event(
    *,
    snapshot: RunSnapshot,
    events: object,
    event_type: EventType,
    command_id: CommandId,
    command_hash: str,
    command_type: str,
    payload: JsonObject,
    occurred_at_utc: datetime,
) -> tuple[KernelEvent | P3KernelEvent, object, ApplicationResult]:
    if isinstance(snapshot.state, P3WorkflowState):
        return _build_p3_completion_event(
            snapshot=snapshot,
            events=events,
            event_type=event_type,
            command_id=command_id,
            command_hash=command_hash,
            command_type=command_type,
            payload=payload,
            occurred_at_utc=occurred_at_utc,
        )
    from orca_agent.orchestration.commands import CommandType

    if not hasattr(events, "get"):
        raise StorageError("event repository is unavailable")
    previous_event = events.get(snapshot.last_event_id)
    if previous_event is None:
        raise StateIntegrityError("previous event was not found")
    event_id = new_id(EventId)
    typed_command_type = CommandType(command_type)
    placeholder = ApplicationResult.accepted_result(
        code=event_type.value,
        run_id=snapshot.run_id,
        revision=snapshot.revision + 1,
        status=RunStatus(snapshot.state.status.value),
        event_id=event_id,
    )
    candidate = KernelEvent.create(
        event_id=event_id,
        command_id=command_id,
        command_type=typed_command_type,
        run_id=snapshot.run_id,
        sequence_no=snapshot.revision + 1,
        expected_revision=snapshot.revision,
        event_type=event_type,
        payload=payload,
        result=placeholder,
        occurred_at_utc=occurred_at_utc,
        command_hash=command_hash,
        previous_event_hash=previous_event.event_hash,
    )
    transition = reduce_event(snapshot.state, candidate)
    result = expected_application_result(
        prior_state=snapshot.state,
        event=candidate,
        transition=transition,
    )
    event = KernelEvent.create(
        event_id=candidate.event_id,
        command_id=candidate.command_id,
        command_type=candidate.command_type,
        run_id=candidate.run_id,
        sequence_no=candidate.sequence_no,
        expected_revision=candidate.expected_revision,
        event_type=candidate.event_type,
        payload=payload,
        result=result,
        occurred_at_utc=candidate.occurred_at_utc,
        recorded_at_utc=candidate.recorded_at_utc,
        engine_version=candidate.engine_version,
        schema_version=candidate.schema_version,
        command_hash=command_hash,
        previous_event_hash=candidate.previous_event_hash,
    )
    return event, transition, result


def _build_p3_completion_event(
    *,
    snapshot: RunSnapshot,
    events: object,
    event_type: EventType,
    command_id: CommandId,
    command_hash: str,
    command_type: str,
    payload: JsonObject,
    occurred_at_utc: datetime,
) -> tuple[P3KernelEvent, object, ApplicationResult]:
    from orca_agent.orchestration.commands import CommandType

    if not hasattr(events, "get") or not isinstance(snapshot.state, P3WorkflowState):
        raise StorageError("P3 event repository or state is unavailable")
    previous_event = events.get(snapshot.last_event_id)
    if not isinstance(previous_event, P3KernelEvent):
        raise StateIntegrityError("P3 previous event was not found")
    event_id = new_id(EventId)
    typed_command_type = CommandType(command_type)
    placeholder = ApplicationResult.accepted_result(
        code=event_type.value,
        run_id=snapshot.run_id,
        revision=snapshot.revision + 1,
        status=RunStatus(snapshot.state.status.value),
        event_id=event_id,
    )
    candidate = P3KernelEvent.create(
        event_id=event_id,
        command_id=command_id,
        command_type=typed_command_type,
        run_id=snapshot.run_id,
        sequence_no=snapshot.revision + 1,
        expected_revision=snapshot.revision,
        event_type=event_type,
        payload=payload,
        result=placeholder,
        occurred_at_utc=occurred_at_utc,
        command_hash=command_hash,
        previous_event_hash=previous_event.event_hash,
    )
    transition = reduce_p3_event(snapshot.state, candidate)
    result = expected_p3_application_result(
        prior_state=snapshot.state,
        event=candidate,
        transition=transition,
    )
    event = P3KernelEvent.create(
        event_id=candidate.event_id,
        command_id=candidate.command_id,
        command_type=candidate.command_type,
        run_id=candidate.run_id,
        sequence_no=candidate.sequence_no,
        expected_revision=candidate.expected_revision,
        event_type=candidate.event_type,
        payload=payload,
        result=result,
        occurred_at_utc=candidate.occurred_at_utc,
        recorded_at_utc=candidate.recorded_at_utc,
        command_hash=candidate.command_hash,
        previous_event_hash=candidate.previous_event_hash,
    )
    return event, transition, result


__all__ = ["EffectCompletionReport", "EffectCompletionService"]
