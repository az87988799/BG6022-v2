from datetime import UTC, datetime, timedelta

from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import EventId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CreateRun
from orca_agent.orchestration.state import KernelState, RunStatus


def test_compare_and_swap_rejects_stale_revision(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create())

    with (
        SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as first,
        SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as second,
    ):
        first_snapshot = first.runs.require(created.run_id)
        second_snapshot = second.runs.require(created.run_id)
        first.begin()
        first_state = KernelState(
            run_id=created.run_id,
            status=RunStatus.READY,
            pending_interrupt_id=None,
            last_outcome_code="first",
            cancel_reason_code=None,
        )
        assert first.runs.compare_and_swap(
            run_id=created.run_id,
            expected_revision=first_snapshot.revision,
            state=first_state,
            event_id=new_id(EventId),
            updated_at_utc=clock.now_utc(),
        )
        first.commit()

        second.begin()
        second_state = KernelState(
            run_id=created.run_id,
            status=RunStatus.READY,
            pending_interrupt_id=None,
            last_outcome_code="stale",
            cancel_reason_code=None,
        )
        assert not second.runs.compare_and_swap(
            run_id=created.run_id,
            expected_revision=second_snapshot.revision,
            state=second_state,
            event_id=new_id(EventId),
            updated_at_utc=clock.now_utc() + timedelta(seconds=1),
        )
        second.rollback()

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.runs.require(created.run_id).revision == 2
        assert uow.runs.require(created.run_id).state.last_outcome_code == "first"
