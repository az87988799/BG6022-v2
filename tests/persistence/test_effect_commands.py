from datetime import UTC, datetime, timedelta

from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import EffectId, WorkerId
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import OutboxStatus
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.commands import (
    CreateRun,
    RecordEffectFailed,
    RecordEffectSucceeded,
    RequestInterrupt,
)
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.state import RunStatus


def _effect() -> EffectSpec:
    return EffectSpec(
        effect_index=0,
        effect_type="internal.audit",
        effect_class=EffectClass.INTERNAL,
        payload={"source": "test"},
    )


def _receipt() -> dict[str, object]:
    return {
        "receipt_schema": "effect-success/v1",
        "outcome_code": "completed",
        "artifact_ids": [],
    }


def _external_effect(index: int) -> EffectSpec:
    return EffectSpec(
        effect_index=index,
        effect_type="external.audit",
        effect_class=EffectClass.EXTERNAL,
        payload={"index": index},
    )


def _complete_effect(
    path,
    *,
    effect_id: EffectId,
    clock: FrozenClock,
    terminal: str,
) -> EffectId:
    worker_id = WorkerId("worker_00000000000000000000000000000000")
    result = HandlerResult(success=terminal == "succeeded")
    worker = OutboxWorker(
        path,
        lambda _permit: result,
        clock=clock,
        worker_id=worker_id,
        max_attempts=1,
    )
    report = worker.run_once(limit=1)
    assert report and report[0].effect_id == effect_id
    assert report[0].outcome in {"succeeded", "dead_letter"}
    return effect_id


def test_effect_audit_commands_validate_outbox_identity_and_status(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create(effects=(_effect(),)))
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        row = uow.connection.execute("SELECT effect_id FROM outbox").fetchone()
        stored_effect_id = EffectId(str(row[0]))

    pending = service.execute(
        RecordEffectSucceeded.create(
            run_id=created.run_id,
            expected_revision=1,
            effect_id=stored_effect_id,
            result_summary=_receipt(),
        )
    )
    assert pending.accepted is False
    assert pending.code == "effect_status_invalid"

    unknown = service.execute(
        RecordEffectFailed.create(
            run_id=created.run_id,
            expected_revision=1,
            effect_id=EffectId("effect_00000000000000000000000000000000"),
            error_code="handler_failed",
        )
    )
    assert unknown.accepted is False
    assert unknown.code == "effect_not_found"

    other = service.execute(CreateRun.create())
    wrong_run = service.execute(
        RecordEffectSucceeded.create(
            run_id=other.run_id,
            expected_revision=1,
            effect_id=stored_effect_id,
            result_summary=_receipt(),
        )
    )
    assert wrong_run.accepted is False
    assert wrong_run.code == "effect_run_mismatch"

    _complete_effect(
        tmp_path / "state.sqlite3",
        effect_id=stored_effect_id,
        clock=clock,
        terminal="succeeded",
    )
    succeeded = service.execute(
        RecordEffectSucceeded.create(
            run_id=created.run_id,
            expected_revision=1,
            effect_id=stored_effect_id,
            result_summary=_receipt(),
        )
    )
    assert succeeded.accepted

    failure_service = KernelApplicationService(tmp_path / "failure.sqlite3", clock=clock)
    failed_run = failure_service.execute(CreateRun.create(effects=(_effect(),)))
    with SQLiteUnitOfWork(tmp_path / "failure.sqlite3") as uow:
        row = uow.connection.execute("SELECT effect_id FROM outbox").fetchone()
        failed_effect_id = EffectId(str(row[0]))
    _complete_effect(
        tmp_path / "failure.sqlite3",
        effect_id=failed_effect_id,
        clock=clock,
        terminal="dead_letter",
    )
    failed = failure_service.execute(
        RecordEffectFailed.create(
            run_id=failed_run.run_id,
            expected_revision=1,
            effect_id=failed_effect_id,
            error_code="handler_failed",
        )
    )

    assert succeeded.accepted
    assert failed.accepted
    assert failed.status is RunStatus.FAILED
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert len(uow.events.list_for_run(created.run_id)) == 2
        assert uow.outbox.get(stored_effect_id).status is OutboxStatus.SUCCEEDED
    with SQLiteUnitOfWork(tmp_path / "failure.sqlite3") as uow:
        assert len(uow.events.list_for_run(failed_run.run_id)) == 2
        assert uow.outbox.get(failed_effect_id).status is OutboxStatus.DEAD_LETTER


def test_invalid_transition_returns_a_safe_typed_result(tmp_path) -> None:
    service = KernelApplicationService(
        tmp_path / "state.sqlite3",
        clock=FrozenClock(datetime(2026, 9, 4, tzinfo=UTC)),
    )

    result = service.execute(CreateRun.create(effects=(_external_effect(0), _external_effect(1))))

    assert result.accepted is False
    assert result.code == "invalid_transition"
    assert result.details["event_type"] == "run_created"
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.runs.get(result.run_id) is None
        assert uow.events.count_for_run(result.run_id) == 0


def test_effect_success_preserves_pending_interrupt_state(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create(effects=(_effect(),)))
    requested = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="approval",
            payload={"question": "continue?"},
            expires_at_utc=clock.now_utc() + timedelta(minutes=1),
        )
    )
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        row = uow.connection.execute("SELECT effect_id FROM outbox").fetchone()
        effect_id = EffectId(str(row[0]))
    _complete_effect(
        tmp_path / "state.sqlite3",
        effect_id=effect_id,
        clock=clock,
        terminal="succeeded",
    )

    result = service.execute(
        RecordEffectSucceeded.create(
            run_id=created.run_id,
            expected_revision=2,
            effect_id=effect_id,
            result_summary=_receipt(),
        )
    )

    assert result.accepted
    assert result.status is RunStatus.WAITING_FOR_INPUT
    assert result.interrupt_id is None
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        pending = uow.interrupts.get_pending_for_run(created.run_id)
        assert pending is not None
        assert pending.interrupt_id == requested.interrupt_id
