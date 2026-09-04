from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.effect_completion import EffectCompletionService
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import EffectId, WorkerId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import OutboxStatus, backoff_for_attempt
from orca_agent.infrastructure.sqlite import SQLiteConnectionFactory
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.commands import CreateRun
from orca_agent.orchestration.effects import EffectClass, EffectSpec

BASE_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _FailNextCommitConnection:
    def __init__(self, connection) -> None:
        self._connection = connection
        self.fail_next_commit = False

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise RuntimeError("simulated crash before commit")
        self._connection.commit()


class _FailNextCommitFactory:
    def __init__(self, database_path) -> None:
        self._factory = SQLiteConnectionFactory(database_path)

    def connect(self):
        return _FailNextCommitConnection(self._factory.connect())


class _FailNthCommitConnection:
    def __init__(self, connection, fail_on: int) -> None:
        self._connection = connection
        self._fail_on = fail_on
        self._commit_count = 0

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self) -> None:
        self._commit_count += 1
        if self._commit_count == self._fail_on:
            raise RuntimeError("simulated crash before commit")
        self._connection.commit()


class _FailNthCommitFactory:
    def __init__(self, database_path, fail_on: int) -> None:
        self._factory = SQLiteConnectionFactory(database_path)
        self._fail_on = fail_on

    def connect(self):
        return _FailNthCommitConnection(self._factory.connect(), self._fail_on)


def _effect() -> EffectSpec:
    return EffectSpec(
        effect_index=0,
        effect_type="external.test",
        effect_class=EffectClass.EXTERNAL,
        payload={"source": "crash-test"},
    )


def _seed(tmp_path):
    clock = FrozenClock(BASE_TIME)
    database_path = tmp_path / "state.sqlite3"
    service = KernelApplicationService(database_path, clock=clock)
    created = service.execute(CreateRun.create(effects=(_effect(),)))
    with SQLiteUnitOfWork(database_path) as uow:
        row = uow.connection.execute("SELECT effect_id FROM outbox").fetchone()
        effect_id = EffectId(str(row[0]))
    return clock, database_path, created.run_id, effect_id


def test_command_commit_then_restart_retry_is_idempotent(tmp_path) -> None:
    clock = FrozenClock(BASE_TIME)
    database_path = tmp_path / "state.sqlite3"
    command = CreateRun.create(requested_at_utc=BASE_TIME)
    first = KernelApplicationService(database_path, clock=clock).execute(command)
    second = KernelApplicationService(database_path, clock=clock).execute(command)

    assert second == first
    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.events.count_for_run(command.run_id) == 1


def test_claim_write_before_commit_rolls_back_and_remains_claimable(tmp_path) -> None:
    clock, database_path, _, effect_id = _seed(tmp_path)
    worker_id = new_id(WorkerId)
    factory = _FailNextCommitFactory(database_path)

    with SQLiteUnitOfWork(database_path, clock=clock, connection_factory=factory) as uow:
        uow.connection.fail_next_commit = True
        with pytest.raises(RuntimeError):
            uow.outbox.claim_due(
                worker_id=worker_id,
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=5),
                limit=1,
            )

    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        claimed = uow.outbox.claim_due(
            worker_id=worker_id,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )
    assert claimed[0].effect_id == effect_id
    assert claimed[0].attempt_count == 1


def test_claim_commit_then_restart_before_handler_reclaims_after_expiry(tmp_path) -> None:
    clock, database_path, _, effect_id = _seed(tmp_path)
    first_worker = new_id(WorkerId)
    second_worker = new_id(WorkerId)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        first = uow.outbox.claim_due(
            worker_id=first_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]
    assert first.effect_id == effect_id

    clock.advance(timedelta(seconds=5))
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        reclaimed = uow.outbox.claim_due(
            worker_id=second_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]
    assert reclaimed.effect_id == effect_id
    assert reclaimed.attempt_count == 2


def test_handler_failure_retry_commit_then_restart_can_deliver_again(tmp_path) -> None:
    clock, database_path, _, effect_id = _seed(tmp_path)
    failing = OutboxWorker(
        database_path,
        lambda _effect: HandlerResult(
            success=False,
            error_code=None,
        ),
        clock=clock,
    )
    assert failing.run_once(limit=1)[0].outcome == "retry"
    clock.advance(backoff_for_attempt(1))

    succeeding = OutboxWorker(
        database_path,
        lambda _effect: HandlerResult(success=True),
        clock=clock,
    )
    report = succeeding.run_once(limit=1)

    assert report[0].effect_id == effect_id
    assert report[0].outcome == "succeeded"


def test_handler_failure_retry_write_before_commit_rolls_back_and_reclaims(tmp_path) -> None:
    clock, database_path, _, effect_id = _seed(tmp_path)
    first_worker = new_id(WorkerId)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        claimed = uow.outbox.claim_due(
            worker_id=first_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]

    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        permit = uow.outbox.authorize_dispatch(
            runs=uow.runs,
            events=uow.events,
            interrupts=uow.interrupts,
            effect_id=claimed.effect_id,
            worker_id=first_worker,
            expected_generation=claimed.attempt_count,
            now=clock.now_utc(),
        )
    assert permit is not None
    with pytest.raises(RuntimeError):
        EffectCompletionService(
            database_path,
            clock=clock,
            connection_factory=_FailNthCommitFactory(database_path, fail_on=2),
        ).complete(permit, HandlerResult(success=False))

    clock.advance(timedelta(seconds=5))
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        reclaimed = uow.outbox.claim_due(
            worker_id=new_id(WorkerId),
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]

    assert reclaimed.effect_id == effect_id
    assert reclaimed.attempt_count == 2


def test_success_write_before_commit_rolls_back_and_redelivers(tmp_path) -> None:
    clock, database_path, _, effect_id = _seed(tmp_path)
    first_worker = new_id(WorkerId)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        claimed = uow.outbox.claim_due(
            worker_id=first_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]

    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        permit = uow.outbox.authorize_dispatch(
            runs=uow.runs,
            events=uow.events,
            interrupts=uow.interrupts,
            effect_id=claimed.effect_id,
            worker_id=first_worker,
            expected_generation=claimed.attempt_count,
            now=clock.now_utc(),
        )
    assert permit is not None
    with pytest.raises(RuntimeError):
        EffectCompletionService(
            database_path,
            clock=clock,
            connection_factory=_FailNthCommitFactory(database_path, fail_on=2),
        ).complete(permit, HandlerResult(success=True))

    clock.advance(timedelta(seconds=5))
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        reclaimed = uow.outbox.claim_due(
            worker_id=new_id(WorkerId),
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]
    assert reclaimed.effect_id == effect_id
    assert reclaimed.attempt_count == 2


def test_success_commit_then_restart_keeps_effect_terminal(tmp_path) -> None:
    clock, database_path, _, effect_id = _seed(tmp_path)
    worker = OutboxWorker(
        database_path,
        lambda _effect: HandlerResult(success=True),
        clock=clock,
    )
    assert worker.run_once(limit=1)[0].outcome == "succeeded"
    clock.advance(timedelta(hours=1))

    restarted = OutboxWorker(
        database_path,
        lambda _effect: HandlerResult(success=True),
        clock=clock,
    )
    assert restarted.run_once(limit=1) == ()
    with SQLiteUnitOfWork(database_path) as uow:
        effect = uow.outbox.get(effect_id)
        assert effect.status is OutboxStatus.SUCCEEDED
