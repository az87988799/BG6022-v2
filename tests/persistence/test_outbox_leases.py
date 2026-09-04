import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from orca_agent.application.errors import (
    EffectCompletionConflictError,
    LeaseLostError,
    StateIntegrityError,
    StorageBusyError,
)
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import WorkerId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import OutboxRepository, OutboxStatus, backoff_for_attempt
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.commands import CreateRun
from orca_agent.orchestration.effects import EffectClass, EffectSpec

BASE_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _seed(tmp_path, *, effects: tuple[EffectSpec, ...] | None = None):
    clock = FrozenClock(BASE_TIME)
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    result = service.execute(CreateRun.create(effects=effects or (_effect(0),)))
    assert result.accepted and result.event_id is not None
    return clock, result.run_id, result.event_id


def _effect(index: int, effect_class: EffectClass = EffectClass.EXTERNAL) -> EffectSpec:
    return EffectSpec(
        effect_index=index,
        effect_type="external.test",
        effect_class=effect_class,
        payload={"index": index},
    )


def test_claim_order_is_stable_and_two_workers_do_not_share_a_live_lease(tmp_path) -> None:
    clock, run_id, event_id = _seed(
        tmp_path,
        effects=(_effect(0), _effect(1, EffectClass.INTERNAL)),
    )
    first_worker = new_id(WorkerId)
    second_worker = new_id(WorkerId)
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        first = uow.outbox.claim_due(
            worker_id=first_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=10),
            limit=10,
        )
        second = uow.outbox.claim_due(
            worker_id=second_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=10),
            limit=10,
        )

    assert len(first) == 2
    assert second == ()
    assert {record.run_id for record in first} == {run_id}
    assert {record.source_event_id for record in first} == {event_id}
    assert [record.effect_id for record in first] == sorted(record.effect_id for record in first)


def test_two_workers_race_on_two_connections(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path)
    database_path = tmp_path / "state.sqlite3"
    worker_ids = (new_id(WorkerId), new_id(WorkerId))
    workers = tuple(
        OutboxWorker(
            database_path,
            lambda _effect: HandlerResult(success=True),
            clock=clock,
            worker_id=worker_id,
        )
        for worker_id in worker_ids
    )
    barrier = Barrier(2)

    def run(worker: OutboxWorker):
        barrier.wait(timeout=5)
        return worker.run_once(limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = tuple(pool.submit(run, worker) for worker in workers)
        reports = tuple(future.result() for future in futures)

    assert sorted(len(report) for report in reports) == [0, 1]
    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.outbox.count(status=OutboxStatus.SUCCEEDED) == 1


def test_expired_lease_is_reclaimed_with_same_effect_id_and_attempt_count(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path)
    first_worker = new_id(WorkerId)
    second_worker = new_id(WorkerId)
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        first = uow.outbox.claim_due(
            worker_id=first_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]
    clock.advance(timedelta(seconds=5))
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        with pytest.raises(LeaseLostError):
            uow.outbox.renew(
                effect_id=first.effect_id,
                worker_id=first_worker,
                expected_generation=first.attempt_count,
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=5),
            )
        reclaimed = uow.outbox.claim_due(
            worker_id=second_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]

    assert reclaimed.effect_id == first.effect_id
    assert reclaimed.lease_owner == second_worker
    assert reclaimed.attempt_count == 2


def test_renew_requires_owner_and_succeed_is_not_claimed_again(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path)
    owner = new_id(WorkerId)
    other = new_id(WorkerId)
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        effect = uow.outbox.claim_due(
            worker_id=owner,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=10),
            limit=1,
        )[0]
        with pytest.raises(LeaseLostError):
            uow.outbox.renew(
                effect_id=effect.effect_id,
                worker_id=other,
                expected_generation=effect.attempt_count,
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=10),
            )
        renewed = uow.outbox.renew(
            effect_id=effect.effect_id,
            worker_id=owner,
            expected_generation=effect.attempt_count,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=20),
        )
        assert renewed.lease_expires_at_utc == BASE_TIME + timedelta(seconds=20)
        assert uow.outbox.mark_succeeded(
            effect_id=effect.effect_id,
            worker_id=owner,
            expected_generation=effect.attempt_count,
            now=clock.now_utc(),
            result_summary={},
        )
        with pytest.raises(LeaseLostError):
            uow.outbox.mark_succeeded(
                effect_id=effect.effect_id,
                worker_id=other,
                expected_generation=effect.attempt_count,
                now=clock.now_utc(),
                result_summary={},
            )
        assert (
            uow.outbox.claim_due(
                worker_id=other,
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=10),
                limit=1,
            )
            == ()
        )


def test_renew_rejects_non_positive_or_shortening_duration(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path)
    owner = new_id(WorkerId)
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        effect = uow.outbox.claim_due(
            worker_id=owner,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=10),
            limit=1,
        )[0]
        with pytest.raises(ValueError):
            uow.outbox.renew(
                effect_id=effect.effect_id,
                worker_id=owner,
                expected_generation=effect.attempt_count,
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=-1),
            )
        with pytest.raises(ValueError):
            uow.outbox.renew(
                effect_id=effect.effect_id,
                worker_id=owner,
                expected_generation=effect.attempt_count,
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=5),
            )
        with pytest.raises(ValueError):
            uow.outbox.renew(
                effect_id=effect.effect_id,
                worker_id=owner,
                expected_generation=effect.attempt_count,
                now=clock.now_utc(),
                lease_duration=timedelta(0),
            )
        unchanged = uow.outbox.get(effect.effect_id)
        assert unchanged.lease_expires_at_utc == BASE_TIME + timedelta(seconds=10)


def test_failure_uses_one_two_four_backoff_then_dead_letters(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path)
    owner = new_id(WorkerId)
    effect_id = None
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        for attempt in range(1, 6):
            claimed = uow.outbox.claim_due(
                worker_id=owner,
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=30),
                limit=1,
            )[0]
            effect_id = claimed.effect_id
            updated = uow.outbox.mark_failed(
                effect_id=claimed.effect_id,
                worker_id=owner,
                expected_generation=claimed.attempt_count,
                now=clock.now_utc(),
                error_code="handler_failed",
                error_message="controlled test failure",
                max_attempts=5,
            )
            if attempt < 5:
                assert updated.status is OutboxStatus.PENDING
                assert updated.available_at_utc == clock.now_utc() + backoff_for_attempt(attempt)
                clock.advance(backoff_for_attempt(attempt))
            else:
                assert updated.status is OutboxStatus.DEAD_LETTER
                assert updated.completed_at_utc == clock.now_utc()

        assert (
            uow.outbox.claim_due(
                worker_id=owner,
                now=clock.now_utc() + timedelta(hours=1),
                lease_duration=timedelta(seconds=10),
                limit=1,
            )
            == ()
        )
    assert effect_id is not None


def test_successful_handler_and_exception_handler_have_safe_reports(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path)
    seen: list[str] = []

    def successful(effect) -> HandlerResult:
        seen.append(str(effect.effect_id))
        return HandlerResult(success=True)

    worker = OutboxWorker(tmp_path / "state.sqlite3", successful, clock=clock)
    report = worker.run_once(limit=1)
    assert report[0].outcome == "succeeded"
    assert seen == [str(report[0].effect_id)]

    clock, _, _ = _seed(tmp_path / "failure")

    def crashing(_effect):
        raise RuntimeError("do not persist this exception")

    failing_worker = OutboxWorker(tmp_path / "failure" / "state.sqlite3", crashing, clock=clock)
    failure_report = failing_worker.run_once(limit=1)
    assert failure_report[0].outcome == "retry"
    with SQLiteUnitOfWork(tmp_path / "failure" / "state.sqlite3") as uow:
        row = uow.outbox.get(failure_report[0].effect_id)
        assert row.last_error_code == "handler_exception"
        assert row.last_error_message == "injected handler raised an exception"
        assert "do not persist" not in (row.last_error_message or "")


def test_non_typed_handler_result_fails_closed(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path / "invalid")
    worker = OutboxWorker(
        tmp_path / "invalid" / "state.sqlite3",
        lambda _effect: "failure",
        clock=clock,
    )

    report = worker.run_once(limit=1)

    assert report[0].outcome == "retry"
    with SQLiteUnitOfWork(tmp_path / "invalid" / "state.sqlite3") as uow:
        row = uow.outbox.get(report[0].effect_id)
        assert row.status is OutboxStatus.PENDING
        assert row.last_error_code == "invalid_handler_result"
        assert row.last_error_message == "injected handler returned a non-HandlerResult"


def test_handler_success_before_mark_is_at_least_once_after_lease_expiry(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path)
    first_worker = new_id(WorkerId)
    second_worker = new_id(WorkerId)
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        first = uow.outbox.claim_due(
            worker_id=first_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]
    clock.advance(timedelta(seconds=5))
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        second = uow.outbox.claim_due(
            worker_id=second_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]
        assert second.effect_id == first.effect_id
        uow.outbox.mark_succeeded(
            effect_id=second.effect_id,
            worker_id=second_worker,
            expected_generation=second.attempt_count,
            now=clock.now_utc(),
            result_summary={},
        )


def test_outbox_payload_tamper_fails_closed(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path)
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        effect = uow.outbox.claim_due(
            worker_id=new_id(WorkerId),
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=10),
            limit=1,
        )[0]
        uow.connection.execute(
            "UPDATE outbox SET payload_json = ? WHERE effect_id = ?",
            ('{"index":999}', str(effect.effect_id)),
        )
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        with pytest.raises(StateIntegrityError):
            uow.outbox.get(effect.effect_id)


def test_stale_owner_cannot_complete_after_reclaim(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path)
    first_worker = new_id(WorkerId)
    second_worker = new_id(WorkerId)
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        first = uow.outbox.claim_due(
            worker_id=first_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]
    clock.advance(timedelta(seconds=5))
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        second = uow.outbox.claim_due(
            worker_id=second_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]
        uow.outbox.mark_succeeded(
            effect_id=second.effect_id,
            worker_id=second_worker,
            expected_generation=second.attempt_count,
            now=clock.now_utc(),
            result_summary={},
        )
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        with pytest.raises(LeaseLostError):
            uow.outbox.mark_succeeded(
                effect_id=first.effect_id,
                worker_id=first_worker,
                expected_generation=first.attempt_count,
                now=clock.now_utc(),
                result_summary={},
            )
        assert uow.outbox.mark_succeeded(
            effect_id=first.effect_id,
            worker_id=second_worker,
            expected_generation=second.attempt_count,
            now=clock.now_utc(),
            result_summary={},
        )
        stored = uow.outbox.get(first.effect_id)
        assert stored.completed_by_worker_id == second_worker


def test_dead_letter_completion_is_idempotent_only_for_original_owner(tmp_path) -> None:
    clock, _, _ = _seed(tmp_path)
    owner = new_id(WorkerId)
    other = new_id(WorkerId)
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        effect = uow.outbox.claim_due(
            worker_id=owner,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=10),
            limit=1,
        )[0]
        terminal = uow.outbox.mark_failed(
            effect_id=effect.effect_id,
            worker_id=owner,
            expected_generation=effect.attempt_count,
            now=clock.now_utc(),
            error_code="terminal_failure",
            error_message="controlled failure",
            max_attempts=1,
        )
        assert terminal.status is OutboxStatus.DEAD_LETTER
        with pytest.raises(EffectCompletionConflictError):
            uow.outbox.mark_failed(
                effect_id=effect.effect_id,
                worker_id=owner,
                expected_generation=effect.attempt_count,
                now=clock.now_utc(),
                error_code="different_message_is_ignored",
                error_message="different message is ignored",
                max_attempts=1,
            )
        assert (
            uow.outbox.mark_failed(
                effect_id=effect.effect_id,
                worker_id=owner,
                expected_generation=effect.attempt_count,
                now=clock.now_utc(),
                error_code="terminal_failure",
                error_message="controlled failure",
                max_attempts=1,
            )
            == terminal
        )
        with pytest.raises(LeaseLostError):
            uow.outbox.mark_failed(
                effect_id=effect.effect_id,
                worker_id=other,
                expected_generation=effect.attempt_count,
                now=clock.now_utc(),
                error_code="late_failure",
                error_message="late failure",
                max_attempts=1,
            )


@pytest.mark.parametrize(
    ("column", "value"),
    (("effect_type", "external.tampered"), ("effect_class", "internal")),
)
def test_complete_effect_spec_tamper_fails_closed(tmp_path, column: str, value: str) -> None:
    clock, _, _ = _seed(tmp_path)
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        effect = uow.outbox.claim_due(
            worker_id=new_id(WorkerId),
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=10),
            limit=1,
        )[0]
        uow.connection.execute(
            f"UPDATE outbox SET {column} = ? WHERE effect_id = ?",  # noqa: S608 - test column allowlist
            (value, str(effect.effect_id)),
        )
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        with pytest.raises(StateIntegrityError):
            uow.outbox.get(effect.effect_id)


def test_cross_run_effect_tamper_fails_closed(tmp_path) -> None:
    clock, run_id, _ = _seed(tmp_path)
    other = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock).execute(
        CreateRun.create()
    )
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=clock) as uow:
        row = uow.connection.execute("SELECT effect_id FROM outbox").fetchone()
        effect_id = row[0]
        uow.connection.execute("PRAGMA foreign_keys = OFF")
        uow.connection.execute(
            "UPDATE outbox SET run_id = ? WHERE effect_id = ?",
            (str(other.run_id), effect_id),
        )
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        with pytest.raises(StateIntegrityError):
            uow.outbox.get(effect_id)
    assert run_id != other.run_id


class _BusyConnection:
    in_transaction = False

    def execute(self, _sql):
        raise sqlite3.OperationalError("database is locked")


def test_outbox_begin_immediate_maps_busy_to_typed_error() -> None:
    with pytest.raises(StorageBusyError):
        OutboxRepository(_BusyConnection()).claim_due(
            worker_id=WorkerId("worker_00000000000000000000000000000000"),
            now=BASE_TIME,
            lease_duration=timedelta(seconds=5),
            limit=1,
        )
