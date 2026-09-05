import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orca_agent.application.results import ApplicationResult
from orca_agent.domain.ids import EventId, RunId, new_id
from orca_agent.orchestration.commands import CommandType, CreateRun
from orca_agent.orchestration.events import EventType, KernelEvent
from orca_agent.orchestration.state import RunStatus


def _event() -> KernelEvent:
    command = CreateRun.create(requested_at_utc=datetime(2026, 9, 4, tzinfo=UTC))
    result = ApplicationResult.accepted_result(
        code="run_created",
        run_id=command.run_id,
        revision=1,
        status=RunStatus.CREATED,
        event_id=new_id(EventId),
    )
    return KernelEvent.create(
        command_id=command.command_id,
        command_type=CommandType.CREATE_RUN,
        run_id=command.run_id,
        sequence_no=1,
        expected_revision=0,
        event_type=EventType.RUN_CREATED,
        payload={"run_id": str(command.run_id), "effects": []},
        result=result,
        occurred_at_utc=datetime(2026, 9, 4, tzinfo=UTC),
        command_hash=command.command_hash(),
    )


def test_boundary_records_require_explicit_ids_and_schema_version() -> None:
    command = CreateRun.create(requested_at_utc=datetime(2026, 9, 4, tzinfo=UTC))
    data = command.model_dump(mode="json")
    data.pop("command_id")
    with pytest.raises(ValidationError):
        CreateRun.model_validate(data)

    data = command.model_dump(mode="json")
    data.pop("schema_version")
    with pytest.raises(ValidationError):
        CreateRun.model_validate(data)

    data = _event().model_dump(mode="json")
    data.pop("event_id")
    with pytest.raises(ValidationError):
        KernelEvent.model_validate(data)


def test_event_payload_and_result_hashes_are_verified_on_json_load() -> None:
    event = _event()
    data = event.model_dump(mode="json")
    data["payload"]["run_id"] = str(new_id(RunId))
    with pytest.raises(ValidationError):
        KernelEvent.model_validate_json(json.dumps(data))

    data = event.model_dump(mode="json")
    data["result"]["code"] = "tampered"
    with pytest.raises(ValidationError):
        KernelEvent.model_validate_json(json.dumps(data))
