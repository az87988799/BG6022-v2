from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import EventId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CancelRun, CreateRun
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


def test_two_application_commands_race_on_two_connections(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    database_path = tmp_path / "state.sqlite3"
    seed = KernelApplicationService(database_path, clock=clock)
    created = seed.execute(CreateRun.create(requested_at_utc=clock.now_utc()))
    first_service = KernelApplicationService(database_path, clock=clock)
    second_service = KernelApplicationService(database_path, clock=clock)
    commands = (
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=1,
            reason_code="first_cancel",
            requested_at_utc=clock.now_utc(),
        ),
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=1,
            reason_code="second_cancel",
            requested_at_utc=clock.now_utc(),
        ),
    )
    barrier = Barrier(2)

    def run(service, command):
        barrier.wait(timeout=5)
        return service.execute(command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(run, first_service, commands[0]),
            pool.submit(run, second_service, commands[1]),
        )
        results = tuple(future.result() for future in futures)

    assert sum(result.accepted for result in results) == 1
    assert sum(result.code == "revision_conflict" for result in results) == 1
    with SQLiteUnitOfWork(database_path) as uow:
        snapshot = uow.runs.get_verified(created.run_id, uow.events)
        assert snapshot.revision == 2
        assert snapshot.state.status is RunStatus.CANCELLED
        assert uow.events.count_for_run(created.run_id) == 2
