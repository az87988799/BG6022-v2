import json
import uuid

import pytest
from test_p2_hardening_repair import _claim, _seed

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.ids import CommandId, WorkerId, completion_command_id, new_id
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CreateRun


def test_new_external_command_cannot_occupy_completion_id(tmp_path):
    _, path, service, _, effects = _seed(tmp_path)
    result = service.execute(
        CreateRun.create(
            command_id=completion_command_id(effects[0], 1, "succeeded"),
        )
    )
    assert not result.accepted
    with SQLiteUnitOfWork(path) as u:
        assert len(u.runs.list_ids()) == 1
        assert u.connection.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 1


def test_historical_uuid5_retry_before_new_write_restriction(tmp_path, monkeypatch):
    clock, path, service, _, _ = _seed(tmp_path)
    command = CreateRun.create(
        command_id=CommandId(f"command_{uuid.uuid5(uuid.NAMESPACE_URL, 'old').hex}")
    )
    # Simulate a published writer: only bypass the new-write rule, then restore it.
    with monkeypatch.context() as patch:
        patch.setattr("orca_agent.application.service.is_new_external_command_id", lambda _: True)
        original = service.execute(command)
    assert original.accepted
    assert service.execute(command) == original
    different = command.model_copy(update={"requested_at_utc": clock.now_utc()})
    assert different.command_hash() != command.command_hash()
    assert service.execute(different).code == "duplicate_command_conflict"


def test_historical_collision_blocks_dispatch_without_overwriting(tmp_path, monkeypatch):
    clock, path, service, _, effects = _seed(tmp_path)
    collision = CreateRun.create(command_id=completion_command_id(effects[0], 1, "succeeded"))
    with monkeypatch.context() as patch:
        patch.setattr("orca_agent.application.service.is_new_external_command_id", lambda _: True)
        assert service.execute(collision).accepted
    with SQLiteUnitOfWork(path) as u:
        before = tuple(u.connection.iterdump())
    with pytest.raises(StateIntegrityError) as error:
        _claim(path, clock, new_id(WorkerId))
    assert effects[0] in json.dumps(dict(error.value.details))
    with SQLiteUnitOfWork(path) as u:
        assert tuple(u.connection.iterdump()) == before
