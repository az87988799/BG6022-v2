import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from orca_agent.application.effect_completion import EffectCompletionService
from orca_agent.application.errors import (
    EffectCompletionConflictError,
    LeaseLostError,
    StateIntegrityError,
    StorageBusyError,
)
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import WorkerId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import OutboxRepository, OutboxStatus
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.codes import HandlerErrorCode
from orca_agent.orchestration.commands import CreateRun
from orca_agent.orchestration.effects import EffectClass, EffectSpec

BASE_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _seed(tmp_path, *, effects: tuple[EffectSpec, ...] | None = None):
    clock = FrozenClock(BASE_TIME)
    database_path = tmp_path / "state.sqlite3"
    service = KernelApplicationService(database_path, clock=clock)
    result = service.execute(CreateRun.create(effects=effects or (_effect(0),)))
    assert result.accepted and result.event_id is not None
    return clock, database_path, result.run_id, result.event_id


def _effect(index: int, effect_class: EffectClass = EffectClass.EXTERNAL) -> EffectSpec:
    return EffectSpec(
        effect_index=index,
        effect_type="internal.test" if effect_class is EffectClass.INTERNAL else "external.test",
        effect_class=effect_class,
        payload={"index": index},
    )


def _authorize(path, clock, worker_id, *, lease_duration=timedelta(seconds=10)):
    with SQLiteUnitOfWork(path, clock=clock) as uow:
        claimed = uow.outbox.claim_due(
            worker_id=worker_id,
            now=clock.now_utc(),
            lease_duration=lease_duration,
            limit=1,
        )[0]
        permit = uow.outbox.authorize_dispatch(
            runs=uow.runs,
            events=uow.events,
            interrupts=uow.interrupts,
            effect_id=claimed.effect_id,
            worker_id=worker_id,
            expected_generation=claimed.attempt_count,
            now=clock.now_utc(),
        )
    assert permit is not None
    return permit


def _complete(path, clock, worker_id, *, success=True, max_attempts=5):
    permit = _authorize(path, clock, worker_id)
    result = HandlerResult(success=success)
    report = EffectCompletionService(path, clock=clock, max_attempts=max_attempts).complete(
        permit, result
    )
    return permit, report


def test_claim_order_is_stable_and_two_workers_do_not_share_a_live_lease(tmp_path) -> None:
    clock, database_path, run_id, event_id = _seed(
        tmp_path,
        effects=(_effect(0), _effect(1, EffectClass.INTERNAL)),
    )
    first_worker = new_id(WorkerId)
    second_worker = new_id(WorkerId)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
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


def test_two_real_connections_race_for_one_claim(tmp_path) -> None:
    clock, database_path, _, _ = _seed(tmp_path)
    barrier = Barrier(2)
    worker_ids = (new_id(WorkerId), new_id(WorkerId))

    def claim(worker_id: WorkerId):
        with SQLiteUnitOfWork(database_path, clock=clock) as uow:
            barrier.wait(timeout=5)
            return uow.outbox.claim_due(
                worker_id=worker_id,
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=10),
                limit=1,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.submit(claim, worker_id) for worker_id in worker_ids)
        claimed = tuple(future.result() for future in results)

    assert sorted(len(items) for items in claimed) == [0, 1]


def test_expired_leased_and_dispatching_generations_are_reclaimed(tmp_path) -> None:
    clock, database_path, _, _ = _seed(tmp_path)
    first_worker = new_id(WorkerId)
    second_worker = new_id(WorkerId)
    permit = _authorize(
        database_path,
        clock,
        first_worker,
        lease_duration=timedelta(seconds=5),
    )
    clock.advance(timedelta(seconds=5))
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        with pytest.raises(LeaseLostError):
            uow.outbox.renew(
                effect_id=permit.effect.effect_id,
                worker_id=first_worker,
                expected_generation=permit.generation,
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=5),
            )
        reclaimed = uow.outbox.claim_due(
            worker_id=second_worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]

    assert reclaimed.effect_id == permit.effect.effect_id
    assert reclaimed.lease_owner == second_worker
    assert reclaimed.attempt_count == 2


def test_renew_requires_owner_positive_and_strictly_longer_duration(tmp_path) -> None:
    clock, database_path, _, _ = _seed(tmp_path)
    owner = new_id(WorkerId)
    other = new_id(WorkerId)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
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
        renewed = uow.outbox.renew(
            effect_id=effect.effect_id,
            worker_id=owner,
            expected_generation=effect.attempt_count,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=20),
        )
        assert renewed.lease_expires_at_utc == BASE_TIME + timedelta(seconds=20)


def test_failure_uses_backoff_then_dead_letters_without_extra_events(tmp_path) -> None:
    clock, database_path, run_id, _ = _seed(tmp_path)
    worker = OutboxWorker(
        database_path,
        lambda _permit: HandlerResult(success=False),
        clock=clock,
        worker_id=new_id(WorkerId),
        max_attempts=3,
    )
    for expected_outcome, delay in (("retry", 1), ("retry", 2), ("dead_letter", 0)):
        report = worker.run_once(limit=1)[0]
        assert report.outcome == expected_outcome
        if delay:
            clock.advance(timedelta(seconds=delay))
    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.outbox.count(status=OutboxStatus.DEAD_LETTER) == 1
        assert len(uow.events.list_for_run(run_id)) == 2


def test_worker_receives_permit_and_persists_typed_success(tmp_path) -> None:
    clock, database_path, run_id, _ = _seed(tmp_path)
    seen = []

    def handler(permit):
        seen.append(permit)
        return HandlerResult(success=True)

    report = OutboxWorker(database_path, handler, clock=clock).run_once(limit=1)[0]
    assert report.outcome == "succeeded"
    assert seen and seen[0].effect.effect_id == report.effect_id
    with SQLiteUnitOfWork(database_path) as uow:
        effect = uow.outbox.get(report.effect_id)
        assert effect is not None
        assert effect.result_summary is not None
        assert effect.audit_event_id is not None
        assert len(uow.events.list_for_run(run_id)) == 2


def test_handler_exception_and_non_typed_return_are_safe_fixed_retries(tmp_path) -> None:
    clock, database_path, _, _ = _seed(tmp_path)
    reports = OutboxWorker(
        database_path,
        lambda _permit: "failure",
        clock=clock,
    ).run_once(limit=1)
    assert reports[0].outcome == "retry"
    with SQLiteUnitOfWork(database_path) as uow:
        record = uow.outbox.get(reports[0].effect_id)
        assert record.last_error_code == HandlerErrorCode.INVALID_HANDLER_RESULT.value
        assert record.last_error_message == "The handler returned an invalid result."


def test_stale_owner_cannot_complete_after_reclaim(tmp_path) -> None:
    clock, database_path, _, _ = _seed(tmp_path)
    first_worker = new_id(WorkerId)
    second_worker = new_id(WorkerId)
    first = _authorize(
        database_path,
        clock,
        first_worker,
        lease_duration=timedelta(seconds=5),
    )
    clock.advance(timedelta(seconds=5))
    second = _authorize(
        database_path,
        clock,
        second_worker,
        lease_duration=timedelta(seconds=10),
    )
    completed = EffectCompletionService(database_path, clock=clock).complete(
        second, HandlerResult(success=True)
    )
    assert completed.outcome == "succeeded"
    with pytest.raises(LeaseLostError):
        EffectCompletionService(database_path, clock=clock).complete(
            first, HandlerResult(success=True)
        )


def test_same_worker_same_generation_completion_is_idempotent(tmp_path) -> None:
    clock, database_path, _, _ = _seed(tmp_path)
    worker = new_id(WorkerId)
    permit = _authorize(database_path, clock, worker)
    service = EffectCompletionService(database_path, clock=clock)
    first = service.complete(permit, HandlerResult(success=True))
    second = service.complete(permit, HandlerResult(success=True))
    assert first == second


def test_direct_pre_permit_completion_is_disabled(tmp_path) -> None:
    clock, database_path, _, _ = _seed(tmp_path)
    worker = new_id(WorkerId)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        claimed = uow.outbox.claim_due(
            worker_id=worker,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=10),
            limit=1,
        )[0]
        with pytest.raises(EffectCompletionConflictError):
            uow.outbox.mark_succeeded(
                effect_id=claimed.effect_id,
                worker_id=worker,
                expected_generation=claimed.attempt_count,
                now=clock.now_utc(),
                result_summary={"secret": "must-not-persist"},
            )


def test_outbox_payload_and_complete_spec_tamper_fail_closed(tmp_path) -> None:
    clock, database_path, _, _ = _seed(tmp_path)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        effect = uow.outbox.claim_due(
            worker_id=new_id(WorkerId),
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=10),
            limit=1,
        )[0]
        with pytest.raises(sqlite3.IntegrityError):
            uow.connection.execute(
                "UPDATE outbox SET payload_json = ? WHERE effect_id = ?",
                ('{"index":999}', str(effect.effect_id)),
            )
        uow.connection.execute("DROP TRIGGER outbox_spec_immutable")
        uow.connection.execute(
            "UPDATE outbox SET payload_json = ? WHERE effect_id = ?",
            ('{"index":999}', str(effect.effect_id)),
        )
    with SQLiteUnitOfWork(database_path) as uow:
        with pytest.raises(StateIntegrityError):
            uow.outbox.get(effect.effect_id)


@pytest.mark.parametrize(
    ("column", "value"),
    (("effect_type", "external.tampered"), ("effect_class", "internal")),
)
def test_effect_spec_tamper_fails_closed(tmp_path, column: str, value: str) -> None:
    clock, database_path, _, _ = _seed(tmp_path)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        effect = uow.outbox.claim_due(
            worker_id=new_id(WorkerId),
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=10),
            limit=1,
        )[0]
        with pytest.raises(sqlite3.IntegrityError):
            uow.connection.execute(
                f"UPDATE outbox SET {column} = ? WHERE effect_id = ?",  # noqa: S608 - test allowlist
                (value, str(effect.effect_id)),
            )
        uow.connection.execute("DROP TRIGGER outbox_spec_immutable")
        uow.connection.execute(
            f"UPDATE outbox SET {column} = ? WHERE effect_id = ?",  # noqa: S608 - test allowlist
            (value, str(effect.effect_id)),
        )
    with SQLiteUnitOfWork(database_path) as uow:
        with pytest.raises(StateIntegrityError):
            uow.outbox.get(effect.effect_id)


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
