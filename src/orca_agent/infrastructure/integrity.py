"""Cross-table integrity checks for the durable kernel projections."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.errors import DomainError
from orca_agent.domain.hashing import effect_spec_hash, sha256_hex
from orca_agent.domain.ids import EffectId, EventId, InterruptId
from orca_agent.orchestration.events import KernelEvent
from orca_agent.orchestration.reducer import reduce_event
from orca_agent.orchestration.state import KernelState, RunStatus
from orca_agent.orchestration.transitions import InterruptProjectionOperation, InterruptStatus

from .outbox import OutboxStatus

if TYPE_CHECKING:
    from .interrupts import InterruptRecord
    from .outbox import OutboxRecord
    from .repositories import RunSnapshot


@dataclass(frozen=True)
class _ExpectedInterrupt:
    interrupt_id: InterruptId
    run_id: object
    kind: str
    status: InterruptStatus
    schema_version: int
    engine_version: str
    request_event_id: EventId
    terminal_event_id: EventId | None
    payload: object
    payload_hash: str
    response: object | None
    response_hash: str | None
    created_at_utc: object
    expires_at_utc: object
    terminal_at_utc: object | None
    superseded_by: InterruptId | None


def verify_run_projections(
    *,
    snapshot: RunSnapshot,
    events: Sequence[KernelEvent],
    interrupts: Sequence[InterruptRecord],
    outbox: Sequence[OutboxRecord],
) -> None:
    """Require interrupt and outbox projections to be derivable from events."""

    _verify_interrupts(snapshot=snapshot, events=events, actual=interrupts)
    _verify_outbox(snapshot=snapshot, events=events, actual=outbox)


def _verify_interrupts(
    *,
    snapshot: RunSnapshot,
    events: Sequence[KernelEvent],
    actual: Sequence[InterruptRecord],
) -> None:
    expected = _expected_interrupts(events)
    actual_by_id: dict[InterruptId, InterruptRecord] = {}
    for record in actual:
        if record.interrupt_id in actual_by_id:
            raise StateIntegrityError("interrupt projection contains duplicate IDs")
        actual_by_id[record.interrupt_id] = record
    if set(actual_by_id) != set(expected):
        raise StateIntegrityError("interrupt projection does not match event history")
    for interrupt_id, expected_record in expected.items():
        actual_record = actual_by_id[interrupt_id]
        fields = (
            (actual_record.run_id, expected_record.run_id),
            (actual_record.kind, expected_record.kind),
            (actual_record.status, expected_record.status),
            (actual_record.schema_version, expected_record.schema_version),
            (actual_record.engine_version, expected_record.engine_version),
            (actual_record.request_event_id, expected_record.request_event_id),
            (actual_record.terminal_event_id, expected_record.terminal_event_id),
            (actual_record.payload, expected_record.payload),
            (actual_record.payload_hash, expected_record.payload_hash),
            (actual_record.response, expected_record.response),
            (actual_record.response_hash, expected_record.response_hash),
            (actual_record.created_at_utc, expected_record.created_at_utc),
            (actual_record.expires_at_utc, expected_record.expires_at_utc),
            (actual_record.terminal_at_utc, expected_record.terminal_at_utc),
            (actual_record.superseded_by, expected_record.superseded_by),
        )
        if any(left != right for left, right in fields):
            raise StateIntegrityError("interrupt projection does not match event history")

    pending = tuple(record for record in actual if record.status is InterruptStatus.PENDING)
    if snapshot.state.status is RunStatus.WAITING_FOR_INPUT:
        if (
            len(pending) != 1
            or snapshot.state.pending_interrupt_id is None
            or pending[0].interrupt_id != snapshot.state.pending_interrupt_id
        ):
            raise StateIntegrityError("waiting run does not have its matching pending interrupt")
    elif pending or snapshot.state.pending_interrupt_id is not None:
        raise StateIntegrityError("non-waiting run has a pending interrupt projection")


def _expected_interrupts(events: Sequence[KernelEvent]) -> dict[InterruptId, _ExpectedInterrupt]:
    expected: dict[InterruptId, _ExpectedInterrupt] = {}
    state: KernelState | None = None
    for event in events:
        transition = reduce_event(state, event)
        for operation in transition.interrupt_operations:
            if operation.operation is InterruptProjectionOperation.INSERT_PENDING:
                if (
                    operation.kind is None
                    or operation.payload is None
                    or operation.expires_at_utc is None
                ):
                    raise StateIntegrityError("pending interrupt projection is incomplete")
                if operation.interrupt_id in expected:
                    raise StateIntegrityError("interrupt projection is inserted twice")
                expected[operation.interrupt_id] = _ExpectedInterrupt(
                    interrupt_id=operation.interrupt_id,
                    run_id=operation.run_id,
                    kind=operation.kind,
                    status=InterruptStatus.PENDING,
                    schema_version=event.schema_version,
                    engine_version=event.engine_version,
                    request_event_id=event.event_id,
                    terminal_event_id=None,
                    payload=operation.payload,
                    payload_hash=sha256_hex(operation.payload),
                    response=None,
                    response_hash=None,
                    created_at_utc=event.occurred_at_utc,
                    expires_at_utc=operation.expires_at_utc,
                    terminal_at_utc=None,
                    superseded_by=None,
                )
                continue

            current = expected.get(operation.interrupt_id)
            if current is None or current.status is not InterruptStatus.PENDING:
                raise StateIntegrityError("interrupt terminal projection has no pending source")
            response_hash = None if operation.response is None else sha256_hex(operation.response)
            expected[operation.interrupt_id] = replace(
                current,
                status=operation.status,
                terminal_event_id=event.event_id,
                response=operation.response,
                response_hash=response_hash,
                terminal_at_utc=event.occurred_at_utc,
                superseded_by=operation.superseded_by,
            )
        state = transition.next_state
    return expected


def _verify_outbox(
    *,
    snapshot: RunSnapshot,
    events: Sequence[KernelEvent],
    actual: Sequence[OutboxRecord],
) -> None:
    expected: dict[EffectId, tuple[KernelEvent, object]] = {}
    state: KernelState | None = None
    for event in events:
        transition = reduce_event(state, event)
        for effect in transition.effects:
            effect_id = effect.effect_id(event.event_id)
            if effect_id in expected:
                raise StateIntegrityError("outbox effect ID is emitted twice")
            expected[effect_id] = (event, effect)
        state = transition.next_state

    actual_by_id: dict[EffectId, OutboxRecord] = {}
    for record in actual:
        if record.effect_id in actual_by_id:
            raise StateIntegrityError("outbox projection contains duplicate IDs")
        actual_by_id[record.effect_id] = record
    if set(actual_by_id) != set(expected):
        raise StateIntegrityError("outbox projection does not match event history")

    audit_events: dict[EffectId, KernelEvent] = {}
    for event in events:
        if event.event_type.value not in {"effect_succeeded", "effect_dead_lettered"}:
            continue
        raw_effect_id = event.payload.get("effect_id")
        try:
            if not isinstance(raw_effect_id, str):
                raise StateIntegrityError("effect audit event is missing effect ID")
            effect_id = EffectId(raw_effect_id)
        except (DomainError, TypeError, ValueError) as error:
            raise StateIntegrityError("effect audit event has an invalid effect ID") from error
        if effect_id in audit_events:
            raise StateIntegrityError("effect has more than one terminal audit event")
        audit_events[effect_id] = event
    unknown_audits = set(audit_events) - set(expected)
    if unknown_audits:
        raise StateIntegrityError("effect audit event does not reference an emitted effect")

    for effect_id, (event, effect) in expected.items():
        record = actual_by_id[effect_id]
        payload_hash = sha256_hex(effect.payload)
        expected_spec_hash = effect_spec_hash(
            effect_id=str(effect_id),
            run_id=str(snapshot.run_id),
            source_event_id=str(event.event_id),
            effect_index=effect.effect_index,
            effect_type=effect.effect_type,
            effect_class=effect.effect_class.value,
            schema_version=event.schema_version,
            engine_version=event.engine_version,
            payload=effect.payload,
            payload_hash=payload_hash,
        )
        fields = (
            (record.run_id, snapshot.run_id),
            (record.source_event_id, event.event_id),
            (record.effect_index, effect.effect_index),
            (record.effect_type, effect.effect_type),
            (record.effect_class, effect.effect_class),
            (record.schema_version, event.schema_version),
            (record.engine_version, event.engine_version),
            (record.payload, effect.payload),
            (record.payload_hash, payload_hash),
            (record.spec_hash, expected_spec_hash),
        )
        if any(left != right for left, right in fields):
            raise StateIntegrityError("outbox projection does not match event history")
        if record.status is OutboxStatus.CANCELLED:
            if not snapshot.state.status.is_terminal:
                raise StateIntegrityError("cancelled effect belongs to a non-terminal run")
            if record.audit_event_id is not None:
                raise StateIntegrityError("cancelled effect has an audit event")
        elif snapshot.state.status.is_terminal and record.status in (
            OutboxStatus.PENDING,
            OutboxStatus.LEASED,
            OutboxStatus.DISPATCHING,
        ):
            raise StateIntegrityError("terminal run retains an active outbox effect")
        elif record.status is OutboxStatus.DISPATCHING:
            if (
                record.audit_event_id is not None
                or record.dispatch_authorized_at_utc is None
                or record.dispatch_run_revision > snapshot.revision
                or record.dispatch_policy_version is None
            ):
                raise StateIntegrityError("dispatching effect has an invalid permit projection")
        audit_event = audit_events.get(effect_id)
        if audit_event is not None and record.audit_event_id != audit_event.event_id:
            raise StateIntegrityError("effect audit event is not bound to its outbox receipt")
        if record.audit_event_id is not None and audit_event is None:
            raise StateIntegrityError("outbox receipt references a missing audit event")


__all__ = ["verify_run_projections"]
