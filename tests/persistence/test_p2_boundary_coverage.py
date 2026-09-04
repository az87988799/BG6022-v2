import json
from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.effect_completion import EffectCompletionService
from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import EffectId, InterruptId, RunId, WorkerId, new_id
from orca_agent.domain.json_types import thaw_json
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import OutboxStatus, backoff_for_attempt
from orca_agent.infrastructure.sqlite import SQLiteConnectionFactory
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.commands import (
    CancelRun,
    CreateRun,
    ReplaceInterrupt,
    RequestInterrupt,
    ResolveInterrupt,
)
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.state import RunStatus

BASE_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _receipt() -> dict[str, object]:
    return {
        "receipt_schema": "effect-success/v1",
        "outcome_code": "completed",
        "artifact_ids": [],
    }


def _effect(index: int = 0) -> EffectSpec:
    return EffectSpec(
        effect_index=index,
        effect_type="external.test",
        effect_class=EffectClass.EXTERNAL,
        payload={"index": index},
    )


def _seed(tmp_path, *, effects: tuple[EffectSpec, ...] = (_effect(),)):
    clock = FrozenClock(BASE_TIME)
    database_path = tmp_path / "state.sqlite3"
    service = KernelApplicationService(database_path, clock=clock)
    created = service.execute(CreateRun.create(effects=effects, requested_at_utc=BASE_TIME))
    assert created.accepted
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        effect_ids = tuple(
            EffectId(str(row[0]))
            for row in uow.connection.execute(
                "SELECT effect_id FROM outbox ORDER BY effect_index"
            ).fetchall()
        )
    return clock, database_path, service, created, effect_ids


def _authorize(path, clock, worker_id: WorkerId):
    with SQLiteUnitOfWork(path, clock=clock) as uow:
        claimed = uow.outbox.claim_due(
            worker_id=worker_id,
            now=clock.now_utc(),
            lease_duration=timedelta(seconds=30),
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


def test_constructor_boundaries_are_explicit(tmp_path) -> None:
    def handler(_permit):
        return HandlerResult(success=True)

    with pytest.raises(ValueError, match="database_path or state_root"):
        KernelApplicationService()
    with pytest.raises(ValueError, match="mutually exclusive"):
        KernelApplicationService(tmp_path, state_root=tmp_path)
    with pytest.raises(ValueError, match="database_path or state_root"):
        SQLiteConnectionFactory()
    with pytest.raises(ValueError, match="mutually exclusive"):
        SQLiteConnectionFactory(tmp_path, state_root=tmp_path)
    with pytest.raises(ValueError, match="database_path or state_root"):
        OutboxWorker(handler=handler)
    with pytest.raises(ValueError, match="mutually exclusive"):
        OutboxWorker(tmp_path, handler, state_root=tmp_path)
    with pytest.raises(ValueError, match="handler is required"):
        OutboxWorker(tmp_path)
    with pytest.raises(ValueError, match="lease_duration"):
        OutboxWorker(tmp_path, handler, lease_duration=timedelta(0))
    with pytest.raises(ValueError, match="max_attempts"):
        OutboxWorker(tmp_path, handler, max_attempts=0)
    with pytest.raises(ValueError, match="max_attempts"):
        EffectCompletionService(tmp_path, max_attempts=0)


def test_state_root_and_invalid_worker_limits_are_supported(tmp_path) -> None:
    clock = FrozenClock(BASE_TIME)
    service = KernelApplicationService(state_root=tmp_path / "root", clock=clock)
    created = service.execute(CreateRun.create(requested_at_utc=BASE_TIME))
    assert created.accepted
    worker = OutboxWorker(
        state_root=tmp_path / "root",
        handler=lambda _permit: HandlerResult(success=True),
        clock=clock,
    )
    assert worker.run_once(limit=0) == ()
    assert worker.run_once(limit=True) == ()


@pytest.mark.parametrize("mode", ("exception", "malformed_success", "bad_failure_code"))
def test_worker_handler_boundary_is_fail_closed(tmp_path, mode: str) -> None:
    clock, database_path, _, _, effect_ids = _seed(tmp_path)

    def handler(_permit):
        if mode == "exception":
            raise RuntimeError("private traceback must not cross the boundary")
        if mode == "malformed_success":
            return HandlerResult(
                success=True,
                result_summary={
                    "receipt_schema": "effect-success/v1",
                    "outcome_code": "not-allowlisted",
                },
            )
        return HandlerResult(success=False, error_code="not-allowlisted")

    report = OutboxWorker(
        database_path,
        handler,
        clock=clock,
        max_attempts=1,
    ).run_once(limit=1)

    assert report[0].outcome == "dead_letter"
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        record = uow.outbox.get(effect_ids[0])
        assert (
            record.last_error_code
            == {
                "exception": "handler_exception",
                "malformed_success": "invalid_handler_result",
                "bad_failure_code": "invalid_handler_result",
            }[mode]
        )
        assert record.last_error_message is not None
        assert "traceback" not in record.last_error_message.casefold()


@pytest.mark.parametrize(
    "raw_result", (object(), HandlerResult(success=True, result_summary={"bad": True}))
)
def test_completion_service_rejects_untrusted_raw_handler_results(
    tmp_path, raw_result: object
) -> None:
    clock, database_path, _, _, _ = _seed(tmp_path)
    permit = _authorize(database_path, clock, new_id(WorkerId))

    report = EffectCompletionService(
        database_path,
        clock=clock,
        max_attempts=1,
    ).complete(permit, raw_result)

    assert report.outcome == "dead_letter"
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        record = uow.outbox.get(permit.effect.effect_id)
        assert record.last_error_code == "invalid_handler_result"


def test_service_command_boundaries_return_typed_results(tmp_path) -> None:
    clock, _, service, created, _ = _seed(tmp_path, effects=())
    duplicate = service.execute(CreateRun.create(run_id=created.run_id, requested_at_utc=BASE_TIME))
    unknown = service.execute(
        CancelRun.create(
            run_id=new_id(RunId),
            expected_revision=1,
            reason_code="user_cancelled",
            requested_at_utc=BASE_TIME,
        )
    )

    assert duplicate.accepted is False
    assert duplicate.code == "run_already_exists"
    assert unknown.accepted is False
    assert unknown.code == "run_not_found"
    assert unknown.revision == 0
    assert unknown.status is RunStatus.CREATED


def test_service_interrupt_deadline_and_identity_errors_are_typed(tmp_path) -> None:
    clock, _, service, created, _ = _seed(tmp_path, effects=())
    invalid_expiry = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="approval",
            payload={"question": "continue?"},
            expires_at_utc=BASE_TIME,
            requested_at_utc=BASE_TIME,
        )
    )
    missing_pending = service.execute(
        ResolveInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            interrupt_id=new_id(InterruptId),
            response={"approved": True},
            requested_at_utc=BASE_TIME,
        )
    )
    requested = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="approval",
            payload={"question": "continue?"},
            expires_at_utc=BASE_TIME + timedelta(minutes=1),
            requested_at_utc=BASE_TIME,
        )
    )
    replacement = service.execute(
        ReplaceInterrupt.create(
            run_id=created.run_id,
            expected_revision=2,
            old_interrupt_id=new_id(InterruptId),
            new_interrupt_id=new_id(InterruptId),
            kind="approval",
            payload={"question": "replacement"},
            expires_at_utc=BASE_TIME + timedelta(minutes=2),
            requested_at_utc=BASE_TIME,
        )
    )

    assert invalid_expiry.code == "invalid_interrupt_expiry"
    assert missing_pending.code == "interrupt_not_pending"
    assert requested.accepted
    assert replacement.code == "interrupt_not_pending"
    assert service.expire_due(limit=0) == ()


def test_outbox_duplicate_registration_and_compatibility_wrappers(tmp_path) -> None:
    clock, database_path, _, created, effect_ids = _seed(tmp_path)
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        event = uow.events.get(created.event_id)
        assert event is not None
        effects = tuple(
            EffectSpec.model_validate_json(json.dumps(thaw_json(item)))
            for item in event.payload["effects"]
        )
        assert (
            uow.outbox.register_effects(
                event=event,
                run_id=created.run_id,
                effects=effects,
                available_at_utc=BASE_TIME,
            )
            == effect_ids
        )
        with pytest.raises(StateIntegrityError):
            uow.outbox.register_effects(
                event=event,
                run_id=new_id(RunId),
                effects=(),
                available_at_utc=BASE_TIME,
            )
        with pytest.raises(StateIntegrityError):
            uow.outbox.register_effects(
                event=event,
                run_id=created.run_id,
                effects=(_effect(1),),
                available_at_utc=BASE_TIME,
            )
        claimed = uow.outbox.claim_due(
            worker_id=new_id(WorkerId),
            now=BASE_TIME,
            lease_duration=timedelta(seconds=30),
            limit=1,
        )[0]
        prepared = uow.outbox.prepare_dispatch(
            runs=uow.runs,
            events=uow.events,
            interrupts=uow.interrupts,
            effect_id=claimed.effect_id,
            worker_id=claimed.lease_owner,
            expected_generation=claimed.attempt_count,
            now=BASE_TIME,
        )
        assert prepared is not None
        assert prepared.status is OutboxStatus.DISPATCHING
        with pytest.raises(StateIntegrityError):
            uow.outbox.get_required_run_id(new_id(EffectId))
        with pytest.raises(StateIntegrityError):
            uow.outbox.get_required(new_id(EffectId))


@pytest.mark.parametrize(
    "case",
    (
        "bad_status",
        "bad_audit_id",
        "success_without_receipt",
        "success_with_failure_code",
        "failure_with_receipt",
        "failure_with_unknown_code",
    ),
)
def test_terminal_writer_rejects_malformed_inputs(tmp_path, case: str) -> None:
    clock, database_path, _, created, _ = _seed(tmp_path)
    permit = _authorize(database_path, clock, new_id(WorkerId))
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        if case == "bad_status":
            expected = ValueError
            kwargs = {
                "status": OutboxStatus.PENDING,
                "audit_event_id": created.event_id,
            }
        elif case == "bad_audit_id":
            expected = StateIntegrityError
            kwargs = {
                "status": OutboxStatus.SUCCEEDED,
                "audit_event_id": "event_invalid",
            }
        elif case == "success_without_receipt":
            expected = ValueError
            kwargs = {
                "status": OutboxStatus.SUCCEEDED,
                "audit_event_id": created.event_id,
            }
        elif case == "success_with_failure_code":
            expected = ValueError
            kwargs = {
                "status": OutboxStatus.SUCCEEDED,
                "audit_event_id": created.event_id,
                "result_summary": _receipt(),
                "error_code": "handler_failed",
            }
        elif case == "failure_with_receipt":
            expected = ValueError
            kwargs = {
                "status": OutboxStatus.DEAD_LETTER,
                "audit_event_id": created.event_id,
                "result_summary": _receipt(),
                "error_code": "handler_failed",
            }
        else:
            expected = ValueError
            kwargs = {
                "status": OutboxStatus.DEAD_LETTER,
                "audit_event_id": created.event_id,
                "error_code": "not-allowlisted",
            }
        with pytest.raises(expected):
            uow.outbox.complete_terminal_in_transaction(permit=permit, now=BASE_TIME, **kwargs)


@pytest.mark.parametrize(
    "attempt_count, initial_seconds",
    ((0, 1), (1, 0), (1.0, 1)),
)
def test_backoff_rejects_invalid_arguments(attempt_count: object, initial_seconds: object) -> None:
    with pytest.raises(ValueError):
        backoff_for_attempt(attempt_count, initial_seconds=initial_seconds)
    assert backoff_for_attempt(10, initial_seconds=10).total_seconds() == 60
