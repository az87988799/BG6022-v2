from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.errors import LeaseLostError, StateIntegrityError
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import WorkerId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import OutboxStatus, backoff_for_attempt
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
                now=clock.now_utc(),
                lease_duration=timedelta(seconds=10),
            )
        renewed = uow.outbox.renew(
            effect_id=effect.effect_id,
            worker_id=owner,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=20),
        )
        assert renewed.lease_expires_at_utc == BASE_TIME + timedelta(seconds=20)
        assert uow.outbox.mark_succeeded(
            effect_id=effect.effect_id,
            worker_id=owner,
            now=clock.now_utc(),
        )
        assert uow.outbox.mark_succeeded(
            effect_id=effect.effect_id,
            worker_id=other,
            now=clock.now_utc(),
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
            now=clock.now_utc(),
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
