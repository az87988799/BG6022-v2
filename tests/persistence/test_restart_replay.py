from datetime import UTC, datetime, timedelta

from orca_agent.application.service import KernelApplicationService
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CreateRun, RequestInterrupt


def test_close_reopen_preserves_replayable_run_and_interrupt(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create())
    requested = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="input",
            payload={"question": "continue?"},
            expires_at_utc=clock.now_utc() + timedelta(minutes=1),
        )
    )

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        snapshot = uow.runs.get_verified(created.run_id, uow.events)
        assert snapshot.revision == 2
        assert snapshot.state.pending_interrupt_id == requested.interrupt_id
        assert len(uow.events.list_for_run(created.run_id)) == 2
