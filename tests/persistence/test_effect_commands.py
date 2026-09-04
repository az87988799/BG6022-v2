from datetime import UTC, datetime

from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import EffectId
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import OutboxStatus
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import (
    CreateRun,
    RecordEffectFailed,
    RecordEffectSucceeded,
)
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.state import RunStatus


def test_effect_audit_commands_are_versioned_events(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(
        CreateRun.create(
            effects=(
                EffectSpec(
                    effect_index=0,
                    effect_type="internal.audit",
                    effect_class=EffectClass.INTERNAL,
                    payload={"source": "test"},
                ),
            )
        )
    )
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        row = uow.connection.execute("SELECT effect_id FROM outbox").fetchone()
        stored_effect_id = EffectId(str(row[0]))

    succeeded = service.execute(
        RecordEffectSucceeded.create(
            run_id=created.run_id,
            expected_revision=1,
            effect_id=stored_effect_id,
            result_summary={"accepted": True},
        )
    )
    failed = service.execute(
        RecordEffectFailed.create(
            run_id=created.run_id,
            expected_revision=2,
            effect_id=stored_effect_id,
            error_code="terminal_failure",
            error_message="controlled failure",
        )
    )

    assert succeeded.accepted
    assert failed.accepted
    assert failed.status is RunStatus.FAILED
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert len(uow.events.list_for_run(created.run_id)) == 3
        assert uow.outbox.get(stored_effect_id).status is OutboxStatus.PENDING
