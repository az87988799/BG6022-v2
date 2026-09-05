from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orca_agent.application.results import ApplicationResult
from orca_agent.domain.ids import (
    CommandId,
    EventId,
    InterruptId,
    RunId,
    effect_id_for,
    new_id,
)
from orca_agent.orchestration.commands import CommandType, CreateRun
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.events import EventType, KernelEvent
from orca_agent.orchestration.state import KernelState, RunStatus
from orca_agent.orchestration.transitions import (
    ApplicationOutcome,
    InterruptProjectionOp,
    InterruptProjectionOperation,
    InterruptStatus,
    Transition,
)


def test_p2_ids_and_effect_ids_are_stable() -> None:
    event_id = new_id(EventId)
    first = effect_id_for(event_id, 0)

    assert first == effect_id_for(EventId(str(event_id)), 0)
    assert first != effect_id_for(event_id, 1)


def test_create_run_factory_builds_explicit_command_envelope() -> None:
    command = CreateRun.create(
        run_id=new_id(RunId),
        command_id=new_id(CommandId),
        requested_at_utc=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert command.command_type is CommandType.CREATE_RUN
    assert command.expected_revision is None
    assert command.schema_version == 1
    assert command.event_payload()["run_id"] == str(command.run_id)


def test_event_requires_content_hashes_to_match() -> None:
    command = CreateRun.create(requested_at_utc=datetime(2026, 9, 4, tzinfo=UTC))
    result = ApplicationResult.accepted_result(
        code="run_created",
        run_id=command.run_id,
        revision=1,
        status=RunStatus.CREATED,
        event_id=None,
    )
    event = KernelEvent.create(
        command_id=command.command_id,
        command_type=command.command_type,
        run_id=command.run_id,
        sequence_no=1,
        expected_revision=0,
        event_type=EventType.RUN_CREATED,
        payload=command.event_payload(),
        result=result,
        occurred_at_utc=command.requested_at_utc,
        command_hash=command.command_hash(),
    )
    assert KernelEvent.model_validate_json(event.model_dump_json()) == event

    tampered = event.model_dump(mode="json")
    tampered["payload"]["run_id"] = str(new_id(RunId))
    with pytest.raises(ValidationError):
        KernelEvent.model_validate(tampered)


def test_transition_rejects_two_external_effects() -> None:
    run_id = new_id(RunId)
    state = KernelState(
        run_id=run_id,
        status=RunStatus.CREATED,
        pending_interrupt_id=None,
        last_outcome_code=None,
        cancel_reason_code=None,
    )
    effects = tuple(
        EffectSpec(
            effect_index=index,
            effect_type="test.external",
            effect_class=EffectClass.EXTERNAL,
            payload={"index": index},
        )
        for index in (0, 1)
    )
    with pytest.raises(ValidationError):
        Transition(
            next_status=RunStatus.CREATED,
            next_state=state,
            effects=effects,
            interrupt_operations=(),
            outcome=ApplicationOutcome(accepted=True, code="ok", details={}),
        )


def test_transition_links_pending_state_to_projection_operation() -> None:
    run_id = new_id(RunId)
    interrupt_id = new_id(InterruptId)
    state = KernelState(
        run_id=run_id,
        status=RunStatus.WAITING_FOR_INPUT,
        pending_interrupt_id=interrupt_id,
        last_outcome_code="interrupt_requested",
        cancel_reason_code=None,
    )
    operation = InterruptProjectionOp(
        operation=InterruptProjectionOperation.INSERT_PENDING,
        run_id=run_id,
        interrupt_id=interrupt_id,
        status=InterruptStatus.PENDING,
        kind="approval",
        payload={"question": "continue?"},
        expires_at_utc=datetime(2026, 9, 5, tzinfo=UTC),
        response=None,
        superseded_by=None,
    )

    transition = Transition(
        next_status=RunStatus.WAITING_FOR_INPUT,
        next_state=state,
        effects=(),
        interrupt_operations=(operation,),
        outcome=ApplicationOutcome(accepted=True, code="interrupt_requested", details={}),
    )
    assert transition.next_state.pending_interrupt_id == interrupt_id
