from datetime import UTC, datetime, timedelta

from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import WorkerId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import OutboxStatus
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CreateRun, RequestInterrupt, ResolveInterrupt
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.state import RunStatus


def test_typed_command_event_reducer_snapshot_and_outbox_slice(tmp_path) -> None:
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now)
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(
        CreateRun.create(
            effects=(
                EffectSpec(
                    effect_index=0,
                    effect_type="internal.audit",
                    effect_class=EffectClass.INTERNAL,
                    payload={"source": "kernel"},
                ),
            )
        )
    )
    requested = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="input",
            payload={"question": "continue?"},
            expires_at_utc=now + timedelta(minutes=1),
        )
    )
    resolved = service.execute(
        ResolveInterrupt.create(
            run_id=created.run_id,
            expected_revision=2,
            interrupt_id=requested.interrupt_id,
            response={"approved": True},
        )
    )

    assert resolved.status is RunStatus.READY
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        snapshot = uow.runs.get_verified(created.run_id, uow.events)
        assert snapshot.state.status is RunStatus.READY
        assert uow.outbox.count(status=OutboxStatus.PENDING) == 1
        assert uow.outbox.claim_due(
            worker_id=new_id(WorkerId),
            now=now,
            lease_duration=timedelta(seconds=10),
            limit=1,
        )[0].effect_id
