"""Single source of truth for event-to-application-result semantics."""

from __future__ import annotations

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.ids import InterruptId

from .events import EventType, KernelEvent
from .state import KernelState
from .transitions import Transition


def _payload_interrupt_id(event: KernelEvent, key: str) -> InterruptId:
    value = event.payload.get(key)
    if not isinstance(value, str):
        raise StateIntegrityError("event result interrupt ID is missing")
    try:
        return InterruptId(value)
    except Exception as error:
        raise StateIntegrityError("event result interrupt ID is invalid") from error


def expected_application_result(
    *,
    prior_state: KernelState | None,
    event: KernelEvent,
    transition: Transition,
) -> ApplicationResult:
    """Derive every result field from the event and pure transition."""

    interrupt_id: InterruptId | None
    if event.event_type is EventType.RUN_CREATED:
        interrupt_id = None
    elif event.event_type is EventType.INTERRUPT_REQUESTED:
        interrupt_id = _payload_interrupt_id(event, "interrupt_id")
    elif event.event_type is EventType.INTERRUPT_REPLACED:
        interrupt_id = _payload_interrupt_id(event, "new_interrupt_id")
    elif event.event_type in (
        EventType.INTERRUPT_RESOLVED,
        EventType.INTERRUPT_EXPIRED,
    ):
        interrupt_id = _payload_interrupt_id(event, "interrupt_id")
    elif event.event_type is EventType.RUN_CANCELLED:
        interrupt_id = None if prior_state is None else prior_state.pending_interrupt_id
    elif event.event_type in (
        EventType.EFFECT_SUCCEEDED,
        EventType.EFFECT_DEAD_LETTERED,
    ):
        interrupt_id = None
    else:
        raise StateIntegrityError("event type has no result contract")
    return ApplicationResult(
        accepted=transition.outcome.accepted,
        code=transition.outcome.code,
        run_id=event.run_id,
        revision=event.new_revision,
        status=transition.next_status,
        event_id=event.event_id,
        interrupt_id=interrupt_id,
        details=transition.outcome.details,
    )


__all__ = ["expected_application_result"]
