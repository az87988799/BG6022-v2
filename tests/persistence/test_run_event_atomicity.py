from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import CommandId, RunId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CreateRun
from orca_agent.orchestration.effects import EffectClass, EffectSpec

BASE_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _service(tmp_path):
    return KernelApplicationService(tmp_path / "state.sqlite3", clock=FrozenClock(BASE_TIME))


def test_create_run_atomically_persists_snapshot_event_and_effect(tmp_path) -> None:
    service = _service(tmp_path)
    command = CreateRun.create(
        run_id=new_id(RunId),
        effects=(
            EffectSpec(
                effect_index=0,
                effect_type="internal.audit",
                effect_class=EffectClass.INTERNAL,
                payload={"kind": "created"},
            ),
        ),
        requested_at_utc=BASE_TIME,
    )

    result = service.execute(command)

    assert result.accepted is True
    assert result.revision == 1
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.runs.require(command.run_id).revision == 1
        assert uow.events.count_for_run(command.run_id) == 1
        assert uow.outbox.count_for_event(result.event_id) == 1


def test_same_command_retry_returns_original_result_without_duplicate_rows(tmp_path) -> None:
    service = _service(tmp_path)
    command = CreateRun.create(requested_at_utc=BASE_TIME)

    first = service.execute(command)
    second = service.execute(command)

    assert second == first
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.events.count_for_run(command.run_id) == 1
        assert uow.outbox.count_for_event(first.event_id) == 0


def test_same_command_retry_does_not_duplicate_registered_effect(tmp_path) -> None:
    service = _service(tmp_path)
    command = CreateRun.create(
        requested_at_utc=BASE_TIME,
        effects=(
            EffectSpec(
                effect_index=0,
                effect_type="internal.audit",
                effect_class=EffectClass.INTERNAL,
                payload={"kind": "created"},
            ),
        ),
    )

    first = service.execute(command)
    assert service.execute(command) == first
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.outbox.count_for_event(first.event_id) == 1


def test_same_command_id_with_different_payload_is_conflict(tmp_path) -> None:
    service = _service(tmp_path)
    command_id = new_id(CommandId)
    first = CreateRun.create(command_id=command_id, requested_at_utc=BASE_TIME)
    second = CreateRun.create(
        command_id=command_id, requested_at_utc=BASE_TIME, run_id=new_id(RunId)
    )

    assert service.execute(first).accepted is True
    result = service.execute(second)

    assert result.accepted is False
    assert result.code == "duplicate_command_conflict"


def test_run_id_collision_is_rejected_without_new_event(tmp_path) -> None:
    service = _service(tmp_path)
    run_id = new_id(RunId)
    first = CreateRun.create(run_id=run_id, requested_at_utc=BASE_TIME)
    second = CreateRun.create(run_id=run_id, requested_at_utc=BASE_TIME)

    assert service.execute(first).accepted is True
    result = service.execute(second)

    assert result.accepted is False
    assert result.code == "run_already_exists"
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.events.count_for_run(run_id) == 1


def test_exception_after_snapshot_insert_rolls_back_everything(tmp_path) -> None:
    service = _service(tmp_path)
    command = CreateRun.create(requested_at_utc=BASE_TIME)
    failure = Mock(side_effect=RuntimeError("test crash point"))

    with pytest.raises(RuntimeError):
        with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=service.clock) as uow:
            uow.begin()
            assert uow.events is not None
            uow.events.append = failure  # type: ignore[method-assign]
            service._create_run(uow, command, command.command_hash())

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.runs.get(command.run_id) is None
        assert uow.events.count_for_run(command.run_id) == 0
        assert uow.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_reopen_replays_snapshot_and_detects_event_tampering(tmp_path) -> None:
    service = _service(tmp_path)
    command = CreateRun.create(requested_at_utc=BASE_TIME)
    result = service.execute(command)

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        snapshot = uow.runs.get_verified(command.run_id, uow.events)
        assert snapshot.last_event_id == result.event_id
        uow.connection.execute(
            "UPDATE events SET payload_hash = ? WHERE event_id = ?",
            ("0" * 64, str(result.event_id)),
        )

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        with pytest.raises(StateIntegrityError) as error:
            uow.runs.get_verified(command.run_id, uow.events)
        assert getattr(error.value, "code", None) == "state_integrity_error"
