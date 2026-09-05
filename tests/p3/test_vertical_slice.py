import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.p3_service import P3ApplicationService
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.p3 import LedgerState, WorkflowPhase
from orca_agent.execution.commands import ApproveAction, CancelWaterRun, StartWaterRun
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.p3_records import P3RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.dispatch_policy import P3_EFFECT_REGISTRY


def _approval_command(service: P3ApplicationService, started, view=None):
    view = view or service.inspect(started.run_id)
    action = view.action
    return ApproveAction.create(
        run_id=started.run_id,
        conversation_id=started.conversation_id,
        interrupt_id=view.state.approval_interrupt_id,
        action_id=action.action_id,
        action_hash=action.action_hash,
        envelope_hash=sha256_hex(action.execution_envelope),
        budget_hash=sha256_hex(action.budget),
        expected_revision=view.revision,
        requested_at_utc=service.clock.now_utc(),
    )


def test_p3_water_fake_vertical_slice_is_approval_gated_and_idempotent(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    assert started.accepted
    waiting = service.inspect(started.run_id)
    assert waiting.state.phase is WorkflowPhase.AWAITING_APPROVAL
    assert waiting.kernel_status.value == "waiting_for_input"
    assert waiting.outbox == ()
    assert service.create_worker().run_once(limit=1) == ()
    assert service.backend.execution_count() == 0

    approval_command = _approval_command(service, started, waiting)
    approved = service.approve(approval_command)
    assert approved.accepted
    approved_again = service.approve(approval_command)
    assert approved_again.accepted
    assert approved_again.revision == approved.revision

    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        for table in ("runs", "events", "interrupts", "outbox"):
            rows = uow.connection.execute(
                f"SELECT DISTINCT schema_version, engine_version FROM {table}"
            ).fetchall()
            assert {(row[0], row[1]) for row in rows} == {(2, "p3-water-v1")}
        event_count = uow.connection.execute("SELECT count(*) FROM events").fetchone()[0]
        assert event_count == 3
        uow.commit()

    reports = service.create_worker().run_once(limit=3)
    assert [item.outcome for item in reports] == ["succeeded", "succeeded", "succeeded"]
    view = service.inspect(started.run_id)
    assert view.state.phase is WorkflowPhase.COMPLETED
    assert view.ledger_state.value == "succeeded"
    assert len(view.state.accepted_artifact_ids) == 4
    assert service.backend.execution_count() == 1
    assert service.create_worker().run_once(limit=1) == ()


def test_p3_handler_failure_dead_letters_and_fails_the_native_workflow(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock, max_attempts=1)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    assert service.approve(_approval_command(service, started)).accepted
    worker = OutboxWorker(
        service.database_path,
        lambda _permit: HandlerResult(success=False),
        clock=clock,
        max_attempts=1,
        registry=P3_EFFECT_REGISTRY,
    )

    report = worker.run_once(limit=1)
    assert report[0].outcome == "dead_letter"
    view = service.inspect(started.run_id)
    assert view.state.phase is WorkflowPhase.FAILED
    assert view.kernel_status.value == "failed"
    assert view.outbox[0]["status"] == "dead_letter"


def test_p3_wrong_approval_binding_does_not_mutate_waiting_run(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    view = service.inspect(started.run_id)
    action = view.action
    rejected = service.approve(
        ApproveAction.create(
            run_id=started.run_id,
            conversation_id=started.conversation_id,
            interrupt_id=view.state.approval_interrupt_id,
            action_id=action.action_id,
            action_hash="0" * 64,
            envelope_hash=sha256_hex(action.execution_envelope),
            budget_hash=sha256_hex(action.budget),
            expected_revision=view.revision,
            requested_at_utc=clock.now_utc(),
        )
    )
    assert not rejected.accepted
    assert service.inspect(started.run_id).state.phase is WorkflowPhase.AWAITING_APPROVAL


def test_p3_retry_after_start_transaction_rollback_reuses_business_ids(
    tmp_path, monkeypatch
) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    command = StartWaterRun.create(requested_at_utc=clock.now_utc())
    original_append = P3RecordRepository.append
    failed = False

    def fail_once(repository, *args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("simulated start transaction failure")
        return original_append(repository, *args, **kwargs)

    monkeypatch.setattr("orca_agent.infrastructure.p3_records.P3RecordRepository.append", fail_once)
    first = service.start(command)
    assert not first.accepted
    monkeypatch.setattr(
        "orca_agent.infrastructure.p3_records.P3RecordRepository.append", original_append
    )
    second = service.start(command)
    assert second.accepted
    view = service.inspect(command.run_id)
    assert view.state.action_id == second.action_id
    assert view.state.approval_interrupt_id == second.approval_interrupt_id


def test_p3_expired_approval_is_rejected_without_dispatch(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    clock.advance(timedelta(hours=24))
    rejected = service.approve(_approval_command(service, started))
    assert not rejected.accepted
    assert rejected.code == "interrupt_expired"
    assert service.backend.execution_count() == 0
    assert service.inspect(started.run_id).state.phase is WorkflowPhase.FAILED


def test_p3_cancel_before_approval_is_durable_and_idempotent(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    view = service.inspect(started.run_id)
    command = CancelWaterRun.create(
        run_id=started.run_id,
        conversation_id=started.conversation_id,
        expected_revision=view.revision,
        requested_at_utc=clock.now_utc(),
    )
    cancelled = service.cancel(command)
    assert cancelled.accepted
    assert cancelled.phase is WorkflowPhase.CANCELLED
    assert service.backend.execution_count() == 0
    replayed = service.cancel(command)
    assert replayed.accepted
    assert replayed.revision == cancelled.revision
    assert service.inspect(started.run_id).ledger_state is LedgerState.CANCELLED


def test_p3_start_replay_and_non_conversation_request_are_typed(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    command = StartWaterRun.create(requested_at_utc=clock.now_utc())
    first = service.start(command)
    replayed = service.start(command)
    assert first.accepted and replayed.accepted
    assert replayed.revision == first.revision
    rejected = service.start(
        StartWaterRun.create(
            requested_at_utc=clock.now_utc(),
            new_conversation=False,
        )
    )
    assert not rejected.accepted
    assert rejected.code == "invalid_transition"


def test_p3_completion_events_record_upstream_hash_bindings(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    assert service.approve(_approval_command(service, started)).accepted
    worker = service.create_worker()
    assert worker.run_once(limit=1)[0].outcome == "succeeded"
    assert worker.run_once(limit=1)[0].outcome == "succeeded"
    assert worker.run_once(limit=1)[0].outcome == "succeeded"

    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        rows = uow.connection.execute(
            "SELECT event_type, payload_json FROM events WHERE run_id = ? "
            "AND event_type = 'effect_succeeded' ORDER BY sequence_no",
            (str(started.run_id),),
        ).fetchall()
        payloads = [json.loads(row[1]) for row in rows]
        uow.commit()
    assert len(payloads) == 3
    assert {"job_result_hash", "raw_result_hash", "fixture_hash"} <= set(payloads[0]["p3_updates"])
    assert {
        "assessment_hash",
        "claim_hash",
        "evidence_hash",
        "assessment_artifact_hash",
    } <= set(payloads[1]["p3_updates"])
    assert {"manifest_hash", "markdown_hash", "json_hash"} <= set(payloads[2]["p3_updates"])


def test_p3_effect_spec_tamper_fails_closed_before_handler(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    assert service.approve(_approval_command(service, started)).accepted
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        with pytest.raises(sqlite3.IntegrityError):
            uow.connection.execute(
                "UPDATE outbox SET effect_type = 'external.p3.tampered' WHERE run_id = ?",
                (str(started.run_id),),
            )
        uow.connection.commit()
    assert service.inspect(started.run_id).state.phase is WorkflowPhase.DISPATCH_PENDING


def test_p3_job_fixture_identity_is_immutable(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    assert service.approve(_approval_command(service, started)).accepted
    assert service.create_worker().run_once(limit=1)[0].outcome == "succeeded"

    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        with pytest.raises(sqlite3.IntegrityError):
            uow.connection.execute(
                "UPDATE jobs SET fixture_hash = ? WHERE run_id = ?",
                ("0" * 64, str(started.run_id)),
            )
        uow.connection.rollback()
