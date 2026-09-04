"""Pure event reducer for the small P2 kernel state machine."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import ValidationError

from orca_agent.application.errors import InvalidTransitionError
from orca_agent.domain.ids import InterruptId, RunId

from .effects import EffectSpec
from .events import EventType, KernelEvent
from .state import KernelState, RunStatus
from .transitions import (
    ApplicationOutcome,
    InterruptProjectionOp,
    InterruptProjectionOperation,
    InterruptStatus,
    Transition,
)


def _invalid(reason: str, *, event: KernelEvent) -> InvalidTransitionError:
    return InvalidTransitionError(
        reason,
        details={
            "event_type": event.event_type.value,
            "run_id": str(event.run_id),
            "sequence_no": event.sequence_no,
        },
    )


def _value(payload: Mapping[str, object], key: str, *, event: KernelEvent) -> object:
    if key not in payload:
        raise _invalid(f"event payload is missing {key}", event=event)
    return payload[key]


def _text(payload: Mapping[str, object], key: str, *, event: KernelEvent) -> str:
    value = _value(payload, key, event=event)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"event payload field {key} is invalid", event=event)
    return value.strip()


def _run_id(payload: Mapping[str, object], key: str, *, event: KernelEvent) -> RunId:
    try:
        value = RunId(str(_value(payload, key, event=event)))
    except (TypeError, ValueError) as error:
        raise _invalid(f"event payload field {key} is invalid", event=event) from error
    return value


def _interrupt_id(payload: Mapping[str, object], key: str, *, event: KernelEvent) -> InterruptId:
    try:
        value = InterruptId(str(_value(payload, key, event=event)))
    except (TypeError, ValueError) as error:
        raise _invalid(f"event payload field {key} is invalid", event=event) from error
    return value


def _utc_timestamp(payload: Mapping[str, object], key: str, *, event: KernelEvent) -> datetime:
    value = _value(payload, key, event=event)
    if not isinstance(value, str):
        raise _invalid(f"event payload field {key} is invalid", event=event)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _invalid(f"event payload field {key} is invalid", event=event) from error
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise _invalid(f"event payload field {key} is not UTC", event=event)
    return parsed


def _effects(payload: Mapping[str, object], *, event: KernelEvent) -> tuple[EffectSpec, ...]:
    raw = payload.get("effects", [])
    if not isinstance(raw, (list, tuple)):
        raise _invalid("event effects must be a JSON array", event=event)
    try:
        effects = tuple(
            EffectSpec.model_validate(dict(item), strict=False)
            if isinstance(item, Mapping)
            else EffectSpec.model_validate(item)
            for item in raw
        )
    except (TypeError, ValidationError) as error:
        raise _invalid("event contains an invalid effect", event=event) from error
    return effects


def _state(
    *,
    run_id: RunId,
    status: RunStatus,
    pending_interrupt_id: InterruptId | None,
    last_outcome_code: str | None,
    cancel_reason_code: str | None,
) -> KernelState:
    return KernelState(
        run_id=run_id,
        status=status,
        pending_interrupt_id=pending_interrupt_id,
        last_outcome_code=last_outcome_code,
        cancel_reason_code=cancel_reason_code,
    )


def _outcome(code: str, *, accepted: bool = True) -> ApplicationOutcome:
    return ApplicationOutcome(accepted=accepted, code=code, details={})


def _pending_insert(
    *,
    event: KernelEvent,
    interrupt_id: InterruptId,
    kind: str,
    payload: Mapping[str, object],
    expires_at_utc: datetime,
) -> InterruptProjectionOp:
    return InterruptProjectionOp(
        operation=InterruptProjectionOperation.INSERT_PENDING,
        run_id=event.run_id,
        interrupt_id=interrupt_id,
        status=InterruptStatus.PENDING,
        kind=kind,
        payload=payload,
        expires_at_utc=expires_at_utc,
        response=None,
        superseded_by=None,
    )


def _finalize(
    *,
    event: KernelEvent,
    interrupt_id: InterruptId,
    status: InterruptStatus,
    response: object | None = None,
    superseded_by: InterruptId | None = None,
) -> InterruptProjectionOp:
    return InterruptProjectionOp(
        operation=(
            InterruptProjectionOperation.SUPERSEDE
            if status is InterruptStatus.SUPERSEDED
            else InterruptProjectionOperation.FINALIZE
        ),
        run_id=event.run_id,
        interrupt_id=interrupt_id,
        status=status,
        kind=None,
        payload=None,
        expires_at_utc=None,
        response=response,
        superseded_by=superseded_by,
    )


def _ensure_run_matches(current: KernelState | None, event: KernelEvent) -> None:
    if current is not None and current.run_id != event.run_id:
        raise _invalid("event run_id does not match current state", event=event)


def reduce_event(current: KernelState | None, event: KernelEvent) -> Transition:
    """Apply one event without reading time, generating IDs, or touching I/O."""

    _ensure_run_matches(current, event)
    payload = event.payload
    effects = _effects(payload, event=event)

    if event.event_type is EventType.RUN_CREATED:
        if current is not None:
            raise _invalid("RunCreated cannot be applied twice", event=event)
        run_id = _run_id(payload, "run_id", event=event)
        if run_id != event.run_id:
            raise _invalid("RunCreated payload run_id does not match event", event=event)
        state = _state(
            run_id=run_id,
            status=RunStatus.CREATED,
            pending_interrupt_id=None,
            last_outcome_code="run_created",
            cancel_reason_code=None,
        )
        return Transition(
            next_status=RunStatus.CREATED,
            next_state=state,
            effects=effects,
            interrupt_operations=(),
            outcome=_outcome("run_created"),
        )

    if current is None:
        raise _invalid("only RunCreated can initialize a run", event=event)
    if current.status.is_terminal:
        raise _invalid("terminal runs cannot accept further events", event=event)

    if event.event_type is EventType.INTERRUPT_REQUESTED:
        if current.status not in (RunStatus.CREATED, RunStatus.READY):
            raise _invalid("interrupt request requires created or ready run", event=event)
        interrupt_id = _interrupt_id(payload, "interrupt_id", event=event)
        kind = _text(payload, "kind", event=event)
        interrupt_payload = _value(payload, "payload", event=event)
        if not isinstance(interrupt_payload, Mapping):
            raise _invalid("interrupt payload must be an object", event=event)
        expiry = _utc_timestamp(payload, "expires_at_utc", event=event)
        if expiry <= event.occurred_at_utc:
            raise _invalid("interrupt expiry must be after event time", event=event)
        state = _state(
            run_id=current.run_id,
            status=RunStatus.WAITING_FOR_INPUT,
            pending_interrupt_id=interrupt_id,
            last_outcome_code="interrupt_requested",
            cancel_reason_code=current.cancel_reason_code,
        )
        return Transition(
            next_status=RunStatus.WAITING_FOR_INPUT,
            next_state=state,
            effects=effects,
            interrupt_operations=(
                _pending_insert(
                    event=event,
                    interrupt_id=interrupt_id,
                    kind=kind,
                    payload=interrupt_payload,
                    expires_at_utc=expiry,
                ),
            ),
            outcome=_outcome("interrupt_requested"),
        )

    if event.event_type is EventType.INTERRUPT_REPLACED:
        if (
            current.status is not RunStatus.WAITING_FOR_INPUT
            or current.pending_interrupt_id is None
        ):
            raise _invalid("interrupt replacement requires a pending interrupt", event=event)
        old_id = _interrupt_id(payload, "old_interrupt_id", event=event)
        new_id = _interrupt_id(payload, "new_interrupt_id", event=event)
        if old_id != current.pending_interrupt_id or old_id == new_id:
            raise _invalid("interrupt replacement IDs do not match state", event=event)
        kind = _text(payload, "kind", event=event)
        interrupt_payload = _value(payload, "payload", event=event)
        if not isinstance(interrupt_payload, Mapping):
            raise _invalid("interrupt payload must be an object", event=event)
        expiry = _utc_timestamp(payload, "expires_at_utc", event=event)
        if expiry <= event.occurred_at_utc:
            raise _invalid("interrupt expiry must be after event time", event=event)
        state = _state(
            run_id=current.run_id,
            status=RunStatus.WAITING_FOR_INPUT,
            pending_interrupt_id=new_id,
            last_outcome_code="interrupt_replaced",
            cancel_reason_code=current.cancel_reason_code,
        )
        return Transition(
            next_status=RunStatus.WAITING_FOR_INPUT,
            next_state=state,
            effects=effects,
            interrupt_operations=(
                _finalize(
                    event=event,
                    interrupt_id=old_id,
                    status=InterruptStatus.SUPERSEDED,
                    superseded_by=new_id,
                ),
                _pending_insert(
                    event=event,
                    interrupt_id=new_id,
                    kind=kind,
                    payload=interrupt_payload,
                    expires_at_utc=expiry,
                ),
            ),
            outcome=_outcome("interrupt_replaced"),
        )

    if event.event_type is EventType.INTERRUPT_RESOLVED:
        if (
            current.status is not RunStatus.WAITING_FOR_INPUT
            or current.pending_interrupt_id is None
        ):
            raise _invalid("interrupt resolution requires a pending interrupt", event=event)
        interrupt_id = _interrupt_id(payload, "interrupt_id", event=event)
        if interrupt_id != current.pending_interrupt_id:
            raise _invalid("resolved interrupt ID does not match state", event=event)
        response = _value(payload, "response", event=event)
        if not isinstance(response, Mapping):
            raise _invalid("interrupt response must be an object", event=event)
        state = _state(
            run_id=current.run_id,
            status=RunStatus.READY,
            pending_interrupt_id=None,
            last_outcome_code="interrupt_resolved",
            cancel_reason_code=current.cancel_reason_code,
        )
        return Transition(
            next_status=RunStatus.READY,
            next_state=state,
            effects=effects,
            interrupt_operations=(
                _finalize(
                    event=event,
                    interrupt_id=interrupt_id,
                    status=InterruptStatus.RESOLVED,
                    response=response,
                ),
            ),
            outcome=_outcome("interrupt_resolved"),
        )

    if event.event_type is EventType.INTERRUPT_EXPIRED:
        if (
            current.status is not RunStatus.WAITING_FOR_INPUT
            or current.pending_interrupt_id is None
        ):
            raise _invalid("interrupt expiry requires a pending interrupt", event=event)
        interrupt_id = _interrupt_id(payload, "interrupt_id", event=event)
        if interrupt_id != current.pending_interrupt_id:
            raise _invalid("expired interrupt ID does not match state", event=event)
        expires_at = _utc_timestamp(payload, "expires_at_utc", event=event)
        if event.occurred_at_utc < expires_at:
            raise _invalid("interrupt cannot expire before its deadline", event=event)
        state = _state(
            run_id=current.run_id,
            status=RunStatus.READY,
            pending_interrupt_id=None,
            last_outcome_code="interrupt_expired",
            cancel_reason_code=current.cancel_reason_code,
        )
        return Transition(
            next_status=RunStatus.READY,
            next_state=state,
            effects=effects,
            interrupt_operations=(
                _finalize(
                    event=event,
                    interrupt_id=interrupt_id,
                    status=InterruptStatus.EXPIRED,
                ),
            ),
            outcome=_outcome("interrupt_expired", accepted=False),
        )

    if event.event_type is EventType.RUN_CANCELLED:
        reason = _text(payload, "reason_code", event=event)
        operations: tuple[InterruptProjectionOp, ...] = ()
        if current.pending_interrupt_id is not None:
            operations = (
                _finalize(
                    event=event,
                    interrupt_id=current.pending_interrupt_id,
                    status=InterruptStatus.CANCELLED,
                ),
            )
        state = _state(
            run_id=current.run_id,
            status=RunStatus.CANCELLED,
            pending_interrupt_id=None,
            last_outcome_code="run_cancelled",
            cancel_reason_code=reason,
        )
        return Transition(
            next_status=RunStatus.CANCELLED,
            next_state=state,
            effects=effects,
            interrupt_operations=operations,
            outcome=_outcome("run_cancelled"),
        )

    if event.event_type is EventType.EFFECT_SUCCEEDED:
        _text(payload, "effect_id", event=event)
        state = _state(
            run_id=current.run_id,
            status=current.status,
            pending_interrupt_id=current.pending_interrupt_id,
            last_outcome_code="effect_succeeded",
            cancel_reason_code=current.cancel_reason_code,
        )
        return Transition(
            next_status=current.status,
            next_state=state,
            effects=effects,
            interrupt_operations=(),
            outcome=_outcome("effect_succeeded"),
        )

    if event.event_type is EventType.EFFECT_DEAD_LETTERED:
        _text(payload, "effect_id", event=event)
        _text(payload, "error_code", event=event)
        state = _state(
            run_id=current.run_id,
            status=RunStatus.FAILED,
            pending_interrupt_id=None,
            last_outcome_code="effect_dead_lettered",
            cancel_reason_code=current.cancel_reason_code,
        )
        return Transition(
            next_status=RunStatus.FAILED,
            next_state=state,
            effects=effects,
            interrupt_operations=(),
            outcome=_outcome("effect_dead_lettered"),
        )

    raise _invalid("unsupported event type", event=event)


__all__ = ["reduce_event"]
