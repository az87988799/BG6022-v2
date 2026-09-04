from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from orca_agent.application.service import KernelApplicationService
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CreateRun, RequestInterrupt
from orca_agent.orchestration.effects import EffectClass, EffectSpec

BASE_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def test_failure_after_event_append_and_projection_rolls_back_everything(tmp_path) -> None:
    clock = FrozenClock(BASE_TIME)
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create())
    command = RequestInterrupt.create(
        run_id=created.run_id,
        expected_revision=1,
        kind="input",
        payload={"question": "continue?"},
        expires_at_utc=BASE_TIME + timedelta(minutes=1),
    )

    with pytest.raises(RuntimeError):
        with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
            uow.begin()
            assert uow.interrupts is not None
            uow.interrupts.apply_operations = Mock(side_effect=RuntimeError("projection crash"))
            service._update_run(
                uow, command, command.command_hash(), uow.runs.require(created.run_id)
            )

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.runs.require(created.run_id).revision == 1
        assert uow.events.count_for_run(created.run_id) == 1
        assert uow.interrupts.count_for_run(created.run_id) == 0


def test_failure_after_event_append_and_outbox_registration_rolls_back_everything(tmp_path) -> None:
    clock = FrozenClock(BASE_TIME)
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    command = CreateRun.create(
        effects=(
            EffectSpec(
                effect_index=0,
                effect_type="internal.audit",
                effect_class=EffectClass.INTERNAL,
                payload={"source": "test"},
            ),
        )
    )

    with pytest.raises(RuntimeError):
        with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
            uow.begin()
            assert uow.outbox is not None
            uow.outbox.register_effects = Mock(side_effect=RuntimeError("outbox crash"))
            service._create_run(uow, command, command.command_hash())

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.runs.get(command.run_id) is None
        assert uow.events.count_for_run(command.run_id) == 0
        assert uow.outbox.count() == 0
