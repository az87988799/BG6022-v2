import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.effect_completion import EffectCompletionService
from orca_agent.application.errors import (
    EffectAuditConflictError,
    LeaseLostError,
    StateIntegrityError,
    StorageBusyError,
)
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import EffectId, WorkerId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import OutboxStatus
from orca_agent.infrastructure.sqlite import SQLiteConnectionFactory
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.commands import (
    CancelRun,
    CreateRun,
    ExpireInterrupt,
    RecordEffectSucceeded,
    RequestInterrupt,
)
from orca_agent.orchestration.effects import EffectClass, EffectSpec

BASE_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _effect(index: int) -> EffectSpec:
    return EffectSpec(
        effect_index=index,
        effect_type="internal.test",
        effect_class=EffectClass.INTERNAL,
        payload={"index": index},
    )


def _seed(tmp_path, *, count: int = 1):
    clock = FrozenClock(BASE_TIME)
    database_path = tmp_path / "state.sqlite3"
    service = KernelApplicationService(database_path, clock=clock)
    created = service.execute(
        CreateRun.create(
            requested_at_utc=BASE_TIME,
            effects=tuple(_effect(index) for index in range(count)),
        )
    )
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        effect_ids = tuple(
            EffectId(str(row[0]))
            for row in uow.connection.execute(
                "SELECT effect_id FROM outbox ORDER BY effect_index"
            ).fetchall()
        )
    return clock, database_path, service, created, effect_ids


def test_worker_validates_terminal_run_before_handler(tmp_path) -> None:
    clock, database_path, service, created, effect_ids = _seed(tmp_path, count=2)
    cancelled = service.execute(
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=1,
            reason_code="user_cancelled",
            requested_at_utc=clock.now_utc(),
        )
    )
    assert cancelled.accepted
    seen: list[EffectId] = []

    def handler(permit):
        seen.append(permit.effect.effect_id)
        raise AssertionError("terminal-run effect must not be dispatched")

    worker = OutboxWorker(database_path, handler, clock=clock)
    assert worker.run_once(limit=2) == ()
    assert seen == []
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        records = uow.outbox.list_for_run(created.run_id)
        assert {record.effect_id for record in records} == set(effect_ids)
        assert all(record.status is OutboxStatus.CANCELLED for record in records)


def test_terminal_transition_cancels_preclaimed_sibling(tmp_path) -> None:
    clock, database_path, service, created, effect_ids = _seed(tmp_path, count=2)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        claimed = uow.outbox.claim_due(
            worker_id=new_id(WorkerId),
            now=clock.now_utc(),
            lease_duration=timedelta(minutes=5),
            limit=1,
        )
        assert len(claimed) == 1

    cancelled = service.execute(
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=1,
            reason_code="user_cancelled",
            requested_at_utc=clock.now_utc(),
        )
    )
    assert cancelled.accepted
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        records = uow.outbox.list_for_run(created.run_id)
        assert {record.effect_id for record in records} == set(effect_ids)
        assert all(record.status is OutboxStatus.CANCELLED for record in records)

    seen: list[EffectId] = []
    worker = OutboxWorker(
        database_path,
        lambda permit: (seen.append(permit.effect.effect_id), HandlerResult(success=True))[1],
        clock=clock,
    )
    assert worker.run_once(limit=2) == ()
    assert seen == []


def test_worker_fails_closed_before_handler_on_corrupt_history(tmp_path) -> None:
    clock, database_path, _, created, _ = _seed(tmp_path)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        uow.connection.execute(
            "UPDATE events SET event_hash = ? WHERE run_id = ?",
            ("f" * 64, str(created.run_id)),
        )
    seen: list[EffectId] = []
    worker = OutboxWorker(
        database_path,
        lambda permit: (seen.append(permit.effect.effect_id), HandlerResult(success=True))[1],
        clock=clock,
    )
    with pytest.raises(StateIntegrityError):
        worker.run_once(limit=1)
    assert seen == []


def test_worker_fails_closed_when_a_sibling_outbox_row_is_missing(tmp_path) -> None:
    clock, database_path, _, created, effect_ids = _seed(tmp_path, count=2)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        uow.connection.execute("DROP TRIGGER outbox_no_delete")
        uow.connection.execute("DELETE FROM outbox WHERE effect_id = ?", (str(effect_ids[1]),))
        uow.connection.commit()
    seen: list[EffectId] = []
    worker = OutboxWorker(
        database_path,
        lambda permit: (seen.append(permit.effect.effect_id), HandlerResult(success=True))[1],
        clock=clock,
    )
    with pytest.raises(StateIntegrityError):
        worker.run_once(limit=1)
    assert seen == []
    assert created.run_id is not None


def test_same_worker_old_generation_cannot_complete_after_reclaim(tmp_path) -> None:
    clock, database_path, _, _, _ = _seed(tmp_path)
    worker_id = new_id(WorkerId)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        first_claimed = uow.outbox.claim_due(
            worker_id=worker_id,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]
        first = uow.outbox.authorize_dispatch(
            runs=uow.runs,
            events=uow.events,
            interrupts=uow.interrupts,
            effect_id=first_claimed.effect_id,
            worker_id=worker_id,
            expected_generation=first_claimed.attempt_count,
            now=clock.now_utc(),
        )
    assert first is not None
    clock.advance(timedelta(seconds=5))
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        second_claimed = uow.outbox.claim_due(
            worker_id=worker_id,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=5),
            limit=1,
        )[0]
        second = uow.outbox.authorize_dispatch(
            runs=uow.runs,
            events=uow.events,
            interrupts=uow.interrupts,
            effect_id=second_claimed.effect_id,
            worker_id=worker_id,
            expected_generation=second_claimed.attempt_count,
            now=clock.now_utc(),
        )
    assert second is not None
    assert second.generation == first.generation + 1
    assert (
        EffectCompletionService(database_path, clock=clock)
        .complete(second, HandlerResult(success=True))
        .outcome
        == "succeeded"
    )
    with pytest.raises(LeaseLostError):
        EffectCompletionService(database_path, clock=clock).complete(
            first, HandlerResult(success=True)
        )


def test_worker_revalidates_each_effect_after_handler_clock_advance(tmp_path) -> None:
    clock, database_path, _, _, _ = _seed(tmp_path, count=2)
    seen: list[tuple[EffectId, datetime, datetime]] = []

    def handler(permit):
        effect = permit.effect
        seen.append((effect.effect_id, effect.lease_expires_at_utc, clock.now_utc()))
        if len(seen) == 1:
            clock.advance(timedelta(seconds=10))
        return HandlerResult(success=True)

    reports = OutboxWorker(
        database_path,
        handler,
        clock=clock,
        lease_duration=timedelta(seconds=5),
    ).run_once(limit=2)

    assert [report.outcome for report in reports] == ["lease_lost", "succeeded"]
    assert len(seen) == 2
    assert seen[1][1] > seen[1][2]


def test_terminal_receipt_conflict_and_no_delete_trigger(tmp_path) -> None:
    clock, database_path, service, created, effect_ids = _seed(tmp_path)
    worker = OutboxWorker(
        database_path,
        lambda _permit: HandlerResult(success=True),
        clock=clock,
    )
    assert worker.run_once(limit=1)[0].outcome == "succeeded"
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        with pytest.raises(sqlite3.IntegrityError):
            uow.connection.execute("DELETE FROM outbox WHERE effect_id = ?", (str(effect_ids[0]),))
        with pytest.raises(sqlite3.IntegrityError):
            uow.connection.execute(
                "UPDATE outbox SET result_summary_json = ? WHERE effect_id = ?",
                ('{"ok":false}', str(effect_ids[0])),
            )

    conflict = service.execute(
        RecordEffectSucceeded.create(
            run_id=created.run_id,
            expected_revision=1,
            effect_id=effect_ids[0],
            result_summary={
                "receipt_schema": "effect-success/v1",
                "outcome_code": "completed",
                "artifact_ids": ["artifact_00000000000000000000000000000001"],
            },
            requested_at_utc=clock.now_utc(),
        )
    )
    assert conflict.code == EffectAuditConflictError.code


def test_event_envelope_hash_tamper_is_rejected(tmp_path) -> None:
    clock, database_path, service, created, _ = _seed(tmp_path)
    cancelled = service.execute(
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=1,
            reason_code="user_cancelled",
            requested_at_utc=clock.now_utc(),
        )
    )
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        uow.connection.execute(
            "UPDATE events SET previous_event_hash = ? WHERE event_id = ?",
            ("0" * 64, str(cancelled.event_id)),
        )
        with pytest.raises(StateIntegrityError):
            uow.events.get(cancelled.event_id)


def test_duplicate_command_retry_rechecks_all_projections(tmp_path) -> None:
    clock = FrozenClock(BASE_TIME)
    database_path = tmp_path / "state.sqlite3"
    service = KernelApplicationService(database_path, clock=clock)
    command = CreateRun.create(requested_at_utc=BASE_TIME)
    first = service.execute(command)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        uow.connection.execute(
            "UPDATE runs SET revision = 2 WHERE run_id = ?", (str(command.run_id),)
        )
    retry = service.execute(command)
    assert first.accepted
    assert retry.accepted is False
    assert retry.code == StateIntegrityError.code


def test_direct_expiry_is_an_explicit_rejection_result(tmp_path) -> None:
    clock = FrozenClock(BASE_TIME)
    database_path = tmp_path / "state.sqlite3"
    service = KernelApplicationService(database_path, clock=clock)
    created = service.execute(CreateRun.create(requested_at_utc=BASE_TIME))
    requested = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="input",
            payload={"question": "continue?"},
            expires_at_utc=BASE_TIME + timedelta(seconds=5),
            requested_at_utc=BASE_TIME,
        )
    )
    clock.advance(timedelta(seconds=5))
    expired = service.execute(
        ExpireInterrupt.create(
            run_id=created.run_id,
            expected_revision=2,
            interrupt_id=requested.interrupt_id,
            requested_at_utc=clock.now_utc(),
        )
    )
    assert expired.accepted is False
    assert expired.code == "interrupt_expired"


def test_worker_never_persists_arbitrary_failure_diagnostics(tmp_path) -> None:
    clock, database_path, _, _, effect_ids = _seed(tmp_path)
    worker = OutboxWorker(
        database_path,
        lambda _permit: HandlerResult(
            success=False,
            error_code="secret_token",
            error_message="traceback: token=do-not-store /secret/path",
        ),
        clock=clock,
        max_attempts=1,
    )
    assert worker.run_once(limit=1)[0].outcome == "dead_letter"
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        record = uow.outbox.get(effect_ids[0])
        assert record.last_error_code == "invalid_handler_result"
        assert record.last_error_message == "The handler returned an invalid result."


def test_real_second_connection_lock_is_typed(tmp_path) -> None:
    clock, database_path, _, _, _ = _seed(tmp_path)
    first = SQLiteConnectionFactory(database_path).connect()
    second = SQLiteConnectionFactory(database_path).connect()
    try:
        first.execute("BEGIN IMMEDIATE")
        second.execute("PRAGMA busy_timeout = 0")
        with pytest.raises(StorageBusyError):
            from orca_agent.infrastructure.outbox import OutboxRepository

            OutboxRepository(second).claim_due(
                worker_id=new_id(WorkerId),
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=5),
                limit=1,
            )
    finally:
        if first.in_transaction:
            first.rollback()
        first.close()
        second.close()
