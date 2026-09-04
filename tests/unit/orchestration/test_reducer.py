from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.errors import InvalidTransitionError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.hashing import GENESIS_EVENT_HASH
from orca_agent.domain.ids import CommandId, EventId, InterruptId, RunId, new_id
from orca_agent.orchestration.commands import CommandType
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.events import EventType, KernelEvent
from orca_agent.orchestration.reducer import reduce_event
from orca_agent.orchestration.replay import replay, state_hash, verify_snapshot
from orca_agent.orchestration.result_contract import expected_application_result
from orca_agent.orchestration.state import KernelState, RunStatus

BASE_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _event(
    *,
    run_id: RunId,
    sequence: int,
    event_type: EventType,
    payload: dict[str, object],
    command_type: CommandType = CommandType.CREATE_RUN,
    occurred_at: datetime = BASE_TIME,
    expected_revision: int | None = None,
    previous_event_hash: str = GENESIS_EVENT_HASH,
    prior_state: KernelState | None = None,
) -> KernelEvent:
    event_id = new_id(EventId)
    status = {
        EventType.RUN_CREATED: RunStatus.CREATED,
        EventType.INTERRUPT_REQUESTED: RunStatus.WAITING_FOR_INPUT,
        EventType.INTERRUPT_REPLACED: RunStatus.WAITING_FOR_INPUT,
        EventType.INTERRUPT_RESOLVED: RunStatus.READY,
        EventType.INTERRUPT_EXPIRED: RunStatus.READY,
        EventType.RUN_CANCELLED: RunStatus.CANCELLED,
        EventType.EFFECT_SUCCEEDED: RunStatus.CREATED,
        EventType.EFFECT_DEAD_LETTERED: RunStatus.FAILED,
    }[event_type]
    result = ApplicationResult.accepted_result(
        code=event_type.value,
        run_id=run_id,
        revision=sequence,
        status=status,
        event_id=event_id,
    )
    event = KernelEvent.create(
        event_id=event_id,
        command_id=new_id(CommandId),
        command_type=command_type,
        run_id=run_id,
        sequence_no=sequence,
        expected_revision=sequence - 1 if expected_revision is None else expected_revision,
        event_type=event_type,
        payload=payload,
        result=result,
        occurred_at_utc=occurred_at,
        command_hash="0" * 64,
        previous_event_hash=previous_event_hash,
    )
    if prior_state is None:
        return event
    transition = reduce_event(prior_state, event)
    return KernelEvent.create(
        event_id=event.event_id,
        command_id=event.command_id,
        command_type=event.command_type,
        run_id=event.run_id,
        sequence_no=event.sequence_no,
        expected_revision=event.expected_revision,
        event_type=event.event_type,
        payload=event.payload,
        result=expected_application_result(
            prior_state=prior_state,
            event=event,
            transition=transition,
        ),
        occurred_at_utc=event.occurred_at_utc,
        recorded_at_utc=event.recorded_at_utc,
        engine_version=event.engine_version,
        schema_version=event.schema_version,
        command_hash=event.command_hash,
        previous_event_hash=event.previous_event_hash,
    )


def _created(run_id: RunId) -> KernelEvent:
    return _event(
        run_id=run_id,
        sequence=1,
        event_type=EventType.RUN_CREATED,
        payload={"run_id": str(run_id), "effects": []},
    )


def test_reducer_transition_table_and_replay() -> None:
    run_id = new_id(RunId)
    interrupt_id = new_id(InterruptId)
    created = _created(run_id)
    first = reduce_event(None, created).next_state
    requested = _event(
        run_id=run_id,
        sequence=2,
        event_type=EventType.INTERRUPT_REQUESTED,
        command_type=CommandType.REQUEST_INTERRUPT,
        payload={
            "interrupt_id": str(interrupt_id),
            "kind": "approval",
            "payload": {"question": "continue?"},
            "expires_at_utc": "2026-09-04T12:05:00.000000Z",
            "effects": [],
        },
        previous_event_hash=created.event_hash,
        prior_state=first,
    )
    waiting = reduce_event(first, requested).next_state
    resolved = _event(
        run_id=run_id,
        sequence=3,
        event_type=EventType.INTERRUPT_RESOLVED,
        command_type=CommandType.RESOLVE_INTERRUPT,
        payload={"interrupt_id": str(interrupt_id), "response": {"approved": True}, "effects": []},
        previous_event_hash=requested.event_hash,
        prior_state=waiting,
    )
    events = (created, requested, resolved)

    ready = reduce_event(waiting, events[2]).next_state

    assert first.status is RunStatus.CREATED
    assert waiting.status is RunStatus.WAITING_FOR_INPUT
    assert waiting.pending_interrupt_id == interrupt_id
    assert ready == replay(events)
    assert ready.status is RunStatus.READY
    assert ready.pending_interrupt_id is None


def test_reducer_is_deterministic_for_identical_inputs() -> None:
    run_id = new_id(RunId)
    event = _created(run_id)
    assert reduce_event(None, event) == reduce_event(None, event)


def test_reducer_rejects_invalid_transitions() -> None:
    run_id = new_id(RunId)
    created = _created(run_id)
    second_create = _event(
        run_id=run_id,
        sequence=2,
        event_type=EventType.RUN_CREATED,
        payload={"run_id": str(run_id), "effects": []},
    )
    with pytest.raises(InvalidTransitionError) as error:
        reduce_event(reduce_event(None, created).next_state, second_create)
    assert getattr(error.value, "code", None) == "invalid_transition"

    resolve_without_pending = _event(
        run_id=run_id,
        sequence=2,
        event_type=EventType.INTERRUPT_RESOLVED,
        command_type=CommandType.RESOLVE_INTERRUPT,
        payload={"interrupt_id": str(new_id(InterruptId)), "response": {}},
    )
    with pytest.raises(InvalidTransitionError) as error:
        reduce_event(reduce_event(None, created).next_state, resolve_without_pending)
    assert getattr(error.value, "code", None) == "invalid_transition"


def test_expiry_uses_inclusive_deadline_and_cancel_projects_pending() -> None:
    run_id = new_id(RunId)
    interrupt_id = new_id(InterruptId)
    created_event = _created(run_id)
    created = reduce_event(None, created_event).next_state
    waiting_event = _event(
        run_id=run_id,
        sequence=2,
        event_type=EventType.INTERRUPT_REQUESTED,
        command_type=CommandType.REQUEST_INTERRUPT,
        payload={
            "interrupt_id": str(interrupt_id),
            "kind": "input",
            "payload": {},
            "expires_at_utc": "2026-09-04T12:05:00.000000Z",
        },
        previous_event_hash=created_event.event_hash,
        prior_state=created,
    )
    waiting = reduce_event(created, waiting_event).next_state
    expired = _event(
        run_id=run_id,
        sequence=3,
        event_type=EventType.INTERRUPT_EXPIRED,
        command_type=CommandType.EXPIRE_INTERRUPT,
        occurred_at=BASE_TIME + timedelta(minutes=5),
        payload={
            "interrupt_id": str(interrupt_id),
            "expires_at_utc": "2026-09-04T12:05:00.000000Z",
        },
        previous_event_hash=waiting_event.event_hash,
        prior_state=waiting,
    )
    assert reduce_event(waiting, expired).next_state.status is RunStatus.READY

    waiting = reduce_event(created, waiting_event).next_state
    cancel = _event(
        run_id=run_id,
        sequence=3,
        event_type=EventType.RUN_CANCELLED,
        command_type=CommandType.CANCEL_RUN,
        payload={"reason_code": "user_cancelled"},
    )
    transition = reduce_event(waiting, cancel)
    assert transition.next_state.status is RunStatus.CANCELLED
    assert transition.next_state.pending_interrupt_id is None
    assert transition.interrupt_operations[0].status.value == "cancelled"


def test_transition_effect_limit_is_checked_by_reducer() -> None:
    run_id = new_id(RunId)
    effects = [
        EffectSpec(
            effect_index=index,
            effect_type="external.test",
            effect_class=EffectClass.EXTERNAL,
            payload={"index": index},
        ).model_dump(mode="json")
        for index in (0, 1)
    ]
    event = _created(run_id)
    payload = {"run_id": str(run_id), "effects": effects}
    event = KernelEvent.create(
        command_id=event.command_id,
        command_type=event.command_type,
        run_id=run_id,
        sequence_no=1,
        expected_revision=0,
        event_type=EventType.RUN_CREATED,
        payload=payload,
        result=event.result,
        occurred_at_utc=BASE_TIME,
        command_hash=event.command_hash,
    )
    with pytest.raises(InvalidTransitionError) as error:
        reduce_event(None, event)
    assert error.value.code == "invalid_transition"


def test_snapshot_integrity_compares_replay_and_metadata() -> None:
    run_id = new_id(RunId)
    events = (_created(run_id),)
    snapshot = replay(events)
    assert (
        verify_snapshot(
            snapshot=snapshot,
            stored_state_hash=state_hash(snapshot),
            stored_revision=1,
            stored_last_event_id=events[-1].event_id,
            events=events,
        )
        == snapshot
    )
