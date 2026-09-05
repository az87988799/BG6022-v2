import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from orca_agent.application.errors import InvalidTransitionError, StateIntegrityError
from orca_agent.application.p3_service import P3ApplicationService
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    ApprovalGrantId,
    ArtifactId,
    CommandId,
    ExecutionId,
    RunId,
    WorkerId,
    WorkflowRecordId,
)
from orca_agent.domain.p3 import ExecutionIntent, P3WorkflowState, WorkflowPhase
from orca_agent.evidence.pipeline import P3EvidencePipeline
from orca_agent.execution.commands import ApproveAction, StartWaterRun
from orca_agent.execution.fake_backend import FakeBackend
from orca_agent.execution.gateway import FakeExecutionGateway
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import DispatchPermit
from orca_agent.infrastructure.p3_records import (
    ActionRepository,
    ArtifactRecordRepository,
    EvidenceRepository,
    JobRepository,
    P3RecordRepository,
    StoredArtifact,
)
from orca_agent.infrastructure.repositories import EventRepository, RunRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, _normalize_handler_result
from orca_agent.interfaces.cli import main
from orca_agent.orchestration.commands import CommandType
from orca_agent.orchestration.dispatch_policy import P3_EFFECT_REGISTRY
from orca_agent.orchestration.effect_receipts import EffectSuccessReceiptV1
from orca_agent.orchestration.effects import EffectClass
from orca_agent.orchestration.events import EventType
from orca_agent.orchestration.p3_kernel import P3KernelEvent, reduce_p3_event
from orca_agent.orchestration.p3_replay import replay_p3, verify_p3_snapshot
from orca_agent.planning.water import build_water_plan
from orca_agent.reporting.renderer import P3ReportRenderer, _verify_report_raw_result


def _authorize_effect(service, effect_id, clock, worker_number):
    worker = service.create_worker(
        worker_id=WorkerId(f"worker_{worker_number:032x}"),
        lease_duration=timedelta(minutes=1),
    )
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        assert uow.runs is not None
        assert uow.events is not None
        assert uow.interrupts is not None
        assert uow.outbox is not None
        claimed = uow.outbox.claim_due_verified(
            runs=uow.runs,
            events=uow.events,
            interrupts=uow.interrupts,
            worker_id=worker.worker_id,
            now=clock.now_utc(),
            lease_duration=worker.lease_duration,
            limit=1,
            registry=P3_EFFECT_REGISTRY,
        )
    assert len(claimed) == 1
    assert claimed[0].effect_id == effect_id
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        assert uow.runs is not None
        assert uow.events is not None
        assert uow.interrupts is not None
        assert uow.outbox is not None
        permit = uow.outbox.authorize_dispatch(
            runs=uow.runs,
            events=uow.events,
            interrupts=uow.interrupts,
            effect_id=effect_id,
            worker_id=worker.worker_id,
            expected_generation=claimed[0].attempt_count,
            now=clock.now_utc(),
            registry=P3_EFFECT_REGISTRY,
        )
    assert permit is not None
    return worker, permit


def test_fake_backend_reuses_one_execution_and_rejects_binding_change(tmp_path) -> None:
    plan = build_water_plan()
    run_id = RunId("run_00000000000000000000000000000000")
    approval_id = ApprovalGrantId("approval_00000000000000000000000000000000")
    execution_id = ExecutionId("execution_00000000000000000000000000000000")
    intent = ExecutionIntent.create(
        run_id=run_id,
        action_id=plan.action.action_id,
        approval_grant_id=approval_id,
        idempotency_key="p3.fake.reuse",
        execution_id=execution_id,
    )
    backend = FakeBackend(tmp_path / "state")

    first = backend.submit_or_get(intent=intent, action=plan.action, fixture=plan.fixture)
    second = backend.submit_or_get(intent=intent, action=plan.action, fixture=plan.fixture)
    assert not first.reused
    assert second.reused
    assert second.call_count == 1
    assert backend.execution_count() == 1

    changed_intent = ExecutionIntent.create(
        run_id=run_id,
        action_id=plan.action.action_id,
        approval_grant_id=approval_id,
        idempotency_key="p3.fake.changed",
        execution_id=execution_id,
    )
    with pytest.raises(StateIntegrityError):
        backend.submit_or_get(intent=changed_intent, action=plan.action, fixture=plan.fixture)


def test_gateway_rejects_non_dispatch_permits_before_any_storage_write(tmp_path) -> None:
    gateway = FakeExecutionGateway(tmp_path / "state.sqlite", tmp_path / "state")
    assert not gateway.execute(object()).success


def test_gateway_rejects_wrong_effect_class_without_calling_backend(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    view = service.inspect(started.run_id)
    approved = service.approve(
        ApproveAction.create(
            run_id=started.run_id,
            conversation_id=started.conversation_id,
            interrupt_id=view.state.approval_interrupt_id,
            action_id=view.action.action_id,
            action_hash=view.action.action_hash,
            envelope_hash=sha256_hex(view.action.execution_envelope),
            budget_hash=sha256_hex(view.action.budget),
            expected_revision=view.revision,
            requested_at_utc=clock.now_utc(),
        )
    )
    assert approved.accepted
    dispatch_effect_id = approved.dispatch_effect_id
    assert dispatch_effect_id is not None
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        effect = uow.outbox.get(dispatch_effect_id) if uow.outbox else None
        uow.commit()
    assert effect is not None
    permit = DispatchPermit(
        effect=replace(effect, effect_class=EffectClass.INTERNAL),
        worker_id=WorkerId("worker_00000000000000000000000000000000"),
        generation=1,
        run_revision=2,
        policy_version=3,
    )
    gateway = FakeExecutionGateway(service.database_path, service.state_root, clock=clock)
    assert not gateway.execute(permit).success
    assert service.backend.execution_count() == 0


def test_p3_handlers_reuse_persisted_work_before_completion(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    view = service.inspect(started.run_id)
    approved = service.approve(
        ApproveAction.create(
            run_id=started.run_id,
            conversation_id=started.conversation_id,
            interrupt_id=view.state.approval_interrupt_id,
            action_id=view.action.action_id,
            action_hash=view.action.action_hash,
            envelope_hash=sha256_hex(view.action.execution_envelope),
            budget_hash=sha256_hex(view.action.budget),
            expected_revision=view.revision,
            requested_at_utc=clock.now_utc(),
        )
    )
    assert approved.accepted and approved.dispatch_effect_id is not None

    dispatch_worker, dispatch_permit = _authorize_effect(
        service, approved.dispatch_effect_id, clock, 1
    )
    gateway = FakeExecutionGateway(service.database_path, service.state_root, clock=clock)
    dispatch_first = gateway.execute(dispatch_permit)
    dispatch_second = gateway.execute(dispatch_permit)
    assert dispatch_first.success and dispatch_second.success
    assert service.backend.execution_count() == 1
    dispatch_completion = dispatch_worker.completion_service_factory().complete(
        dispatch_permit, dispatch_first
    )
    assert dispatch_completion.outcome == "succeeded"

    assessment_effect_id = service.inspect(started.run_id).state.assessment_effect_id
    assert assessment_effect_id is not None
    assessment_worker, assessment_permit = _authorize_effect(
        service, assessment_effect_id, clock, 2
    )
    evidence = P3EvidencePipeline(service.database_path, service.state_root, clock=clock)
    assess_first = evidence.assess(assessment_permit)
    assess_second = evidence.assess(assessment_permit)
    assert assess_first.success and assess_second.success
    assessment_completion = assessment_worker.completion_service_factory().complete(
        assessment_permit, assess_first
    )
    assert assessment_completion.outcome == "succeeded"

    report_effect_id = service.inspect(started.run_id).state.report_effect_id
    assert report_effect_id is not None
    report_worker, report_permit = _authorize_effect(service, report_effect_id, clock, 3)
    renderer = P3ReportRenderer(service.database_path, service.state_root, clock=clock)
    report_first = renderer.render(report_permit)
    report_second = renderer.render(report_permit)
    assert report_first.success and report_second.success
    report_completion = report_worker.completion_service_factory().complete(
        report_permit, report_first
    )
    assert report_completion.outcome == "succeeded"
    assert service.inspect(started.run_id).state.phase.value == "completed"


def test_handler_result_normalization_is_fail_closed_and_receipt_typed() -> None:
    assert _normalize_handler_result(None).error_code.value == "invalid_handler_result"
    assert (
        _normalize_handler_result(HandlerResult(success=False)).error_code.value == "handler_failed"
    )
    invalid_code = _normalize_handler_result(
        HandlerResult(success=False, error_code="not-allowed")
    ).error_code
    assert invalid_code.value == "invalid_handler_result"
    invalid = _normalize_handler_result(HandlerResult(success=True, result_summary={"bad": True}))
    assert not invalid.success
    assert invalid.error_code.value == "invalid_handler_result"
    valid = _normalize_handler_result(HandlerResult(success=True, result_summary=None))
    assert valid.success
    assert isinstance(valid.result_summary, EffectSuccessReceiptV1)


def test_p3_reducer_rejects_non_creation_without_a_current_state() -> None:
    run_id = RunId("run_00000000000000000000000000000000")
    event = P3KernelEvent.create(
        command_id=CommandId("command_00000000000000000000000000000000"),
        command_type=CommandType.CANCEL_RUN,
        run_id=run_id,
        sequence_no=1,
        expected_revision=0,
        event_type=EventType.RUN_CANCELLED,
        payload={"reason_code": "user_cancelled"},
        result={},
        occurred_at_utc=datetime(2026, 9, 5, tzinfo=UTC),
        command_hash="0" * 64,
    )
    with pytest.raises(InvalidTransitionError):
        reduce_p3_event(None, event)


def test_p3_start_replay_with_same_command_is_not_a_second_run(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    command = StartWaterRun.create(requested_at_utc=clock.now_utc())
    first = service.start(command)
    second = service.start(command)
    assert first.accepted and second.accepted
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        count = uow.connection.execute(
            "SELECT count(*) FROM runs WHERE run_id = ?", (str(command.run_id),)
        ).fetchone()[0]
        uow.commit()
    assert count == 1


def test_p3_replay_and_snapshot_verification_reject_damaged_inputs(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        snapshot = RunRepository(uow.connection).require(started.run_id)
        stored_events = EventRepository(uow.connection).list_for_run(started.run_id)
        events = tuple(item.event for item in stored_events)
        uow.commit()
    assert replay_p3(events) == snapshot.state
    assert (
        verify_p3_snapshot(
            snapshot=snapshot.state,
            stored_state_hash=snapshot.state_hash,
            stored_revision=snapshot.revision,
            stored_last_event_id=snapshot.last_event_id,
            events=events,
        )
        == snapshot.state
    )
    with pytest.raises(StateIntegrityError):
        replay_p3(())
    with pytest.raises(StateIntegrityError):
        replay_p3(events + (events[-1],))
    with pytest.raises(StateIntegrityError):
        verify_p3_snapshot(
            snapshot=snapshot.state,
            stored_state_hash="0" * 64,
            stored_revision=snapshot.revision,
            stored_last_event_id=snapshot.last_event_id,
            events=events,
        )
    with pytest.raises(StateIntegrityError):
        verify_p3_snapshot(
            snapshot=snapshot.state,
            stored_state_hash=snapshot.state_hash,
            stored_revision=snapshot.revision + 1,
            stored_last_event_id=snapshot.last_event_id,
            events=events,
        )
    with pytest.raises(StateIntegrityError):
        verify_p3_snapshot(
            snapshot=snapshot.state,
            stored_state_hash=snapshot.state_hash,
            stored_revision=snapshot.revision,
            stored_last_event_id=events[0].event_id,
            events=events,
        )


def test_p3_repositories_return_none_for_missing_bindings(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    missing = RunId("run_11111111111111111111111111111111")
    from orca_agent.domain.ids import ArtifactId, EventId, EvidenceId, ExecutionId
    from orca_agent.domain.p3 import FixtureScientificAssessment

    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        records = P3RecordRepository(uow.connection)
        assert (
            records.latest(
                run_id=started.run_id,
                record_type="assessment",
                model_type=FixtureScientificAssessment,
            )
            is None
        )
        assert ActionRepository(uow.connection).get_by_run(missing) is None
        assert (
            JobRepository(uow.connection).get_by_execution(
                ExecutionId("execution_11111111111111111111111111111111")
            )
            is None
        )
        assert (
            ArtifactRecordRepository(uow.connection).get(
                ArtifactId("artifact_11111111111111111111111111111111")
            )
            is None
        )
        assert (
            EvidenceRepository(uow.connection).get_with_binding(
                EvidenceId("evidence_11111111111111111111111111111111")
            )
            is None
        )
        assert not EvidenceRepository(uow.connection).exists(
            EvidenceId("evidence_11111111111111111111111111111111")
        )
        assert (
            EventRepository(uow.connection).get(EventId("event_11111111111111111111111111111111"))
            is None
        )
        uow.commit()


def test_p3_inspect_rejects_a_rehashed_malformed_typed_record(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        row = uow.connection.execute(
            "SELECT record_json, created_at_utc, source_event_id "
            "FROM workflow_records WHERE run_id = ? AND record_type = 'problem_spec'",
            (str(started.run_id),),
        ).fetchone()
        assert row is not None
        malformed = json.loads(row[0])
        malformed.pop("goal")
        raw = canonical_json_bytes(malformed).decode("utf-8")
        uow.connection.execute(
            "INSERT INTO workflow_records(record_id, run_id, record_type, schema_version, "
            "engine_version, record_json, record_hash, source_event_id, created_at_utc) "
            "VALUES (?, ?, 'problem_spec', 1, 'p1-domain-v1', ?, ?, ?, ?)",
            (
                str(WorkflowRecordId("workflow_11111111111111111111111111111111")),
                str(started.run_id),
                raw,
                sha256_hex(malformed),
                row[2],
                row[1],
            ),
        )
        uow.commit()
    with pytest.raises(StateIntegrityError):
        service.inspect(started.run_id)


def test_artifact_store_rejects_invalid_content_and_paths(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "state")
    with pytest.raises(TypeError):
        store.put(
            connection=None,
            run_id=RunId("run_00000000000000000000000000000000"),
            content="not-bytes",
            media_type="application/octet-stream",
        )
    with pytest.raises(ValueError):
        store.put(
            connection=None,
            run_id=RunId("run_00000000000000000000000000000000"),
            content=b"bytes",
            media_type="",
        )
    with pytest.raises(StateIntegrityError):
        store.path_for("../outside")
    with pytest.raises(StateIntegrityError):
        store.path_for("C:/outside")
    missing = StoredArtifact(
        artifact_id=ArtifactId("artifact_00000000000000000000000000000000"),
        run_id=RunId("run_00000000000000000000000000000000"),
        action_id=None,
        execution_id=None,
        content_hash="0" * 64,
        size_bytes=1,
        media_type="application/octet-stream",
        relative_path="sha256/00/missing",
        created_at_utc=datetime(2026, 9, 5, tzinfo=UTC),
    )
    with pytest.raises(StateIntegrityError):
        store.read(missing)


def test_p3_reducer_rejects_invalid_approval_request_payloads(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        events = EventRepository(uow.connection).list_for_run(started.run_id)
        uow.commit()
    first = events[0].event
    initial = reduce_p3_event(None, first).next_state
    base_inner = {
        "conversation_id": str(initial.conversation_id),
        "action_id": str(initial.action_id),
        "action_hash": initial.action_hash,
        "envelope_hash": initial.envelope_hash,
        "budget_hash": initial.budget_hash,
        "fixture_id": "water_sp_v1",
        "fixture_version": "1",
    }
    base = {
        "interrupt_id": str(initial.approval_interrupt_id),
        "kind": "p3.action_approval",
        "payload": base_inner,
        "expires_at_utc": "2026-09-05T01:00:00Z",
        "effects": [],
    }
    invalid_payloads = []
    missing_interrupt = dict(base)
    missing_interrupt.pop("interrupt_id")
    invalid_payloads.append(missing_interrupt)
    invalid_kind = dict(base)
    invalid_kind["kind"] = "wrong"
    invalid_payloads.append(invalid_kind)
    invalid_effects = dict(base)
    invalid_effects["effects"] = [1]
    invalid_payloads.append(invalid_effects)
    invalid_inner = dict(base)
    invalid_inner["payload"] = "not-an-object"
    invalid_payloads.append(invalid_inner)
    wrong_conversation = dict(base)
    wrong_conversation["payload"] = {**base_inner, "conversation_id": "wrong"}
    invalid_payloads.append(wrong_conversation)
    wrong_action = dict(base)
    wrong_action["payload"] = {**base_inner, "action_id": "wrong"}
    invalid_payloads.append(wrong_action)
    wrong_hash = dict(base)
    wrong_hash["payload"] = {**base_inner, "action_hash": "0" * 64}
    invalid_payloads.append(wrong_hash)
    wrong_expiry = dict(base)
    wrong_expiry["expires_at_utc"] = "2026-09-05T00:00:00Z"
    invalid_payloads.append(wrong_expiry)

    for payload in invalid_payloads:
        event = P3KernelEvent.create(
            command_id=CommandId("command_11111111111111111111111111111111"),
            command_type=CommandType.REQUEST_INTERRUPT,
            run_id=started.run_id,
            sequence_no=2,
            expected_revision=1,
            event_type=EventType.INTERRUPT_REQUESTED,
            payload=payload,
            result={},
            occurred_at_utc=clock.now_utc(),
            command_hash="0" * 64,
            previous_event_hash=first.event_hash,
        )
        with pytest.raises(InvalidTransitionError):
            reduce_p3_event(initial, event)


def test_p3_event_contract_rejects_envelope_mutations(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        first = EventRepository(uow.connection).list_for_run(started.run_id)[0].event
        uow.commit()
    original = first.model_dump(mode="json")
    mutations = []
    for key, value in (
        ("schema_version", 1),
        ("engine_version", "old-engine"),
        ("command_hash", "not-a-hash"),
        ("new_revision", 2),
        ("sequence_no", 2),
    ):
        mutated = dict(original)
        mutated[key] = value
        mutations.append(mutated)
    payload_tampered = dict(original)
    payload_tampered["payload"] = {**original["payload"], "tampered": True}
    mutations.append(payload_tampered)
    for mutated in mutations:
        with pytest.raises(ValidationError):
            P3KernelEvent.model_validate(mutated, strict=True)


def test_p3_reducer_expires_a_pending_approval_as_a_terminal_failure(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        events = EventRepository(uow.connection).list_for_run(started.run_id)
        assert uow.interrupts is not None
        interrupt = uow.interrupts.get(events[1].event.payload["interrupt_id"])
        uow.commit()
    assert interrupt is not None
    created = reduce_p3_event(None, events[0].event).next_state
    waiting = reduce_p3_event(created, events[1].event).next_state
    expired_event = P3KernelEvent.create(
        command_id=CommandId("command_22222222222222222222222222222222"),
        command_type=CommandType.EXPIRE_INTERRUPT,
        run_id=started.run_id,
        sequence_no=3,
        expected_revision=2,
        event_type=EventType.INTERRUPT_EXPIRED,
        payload={
            "interrupt_id": str(waiting.pending_interrupt_id),
            "expires_at_utc": interrupt.expires_at_utc.isoformat().replace("+00:00", "Z"),
            "effects": [],
        },
        result={},
        occurred_at_utc=interrupt.expires_at_utc,
        command_hash="0" * 64,
        previous_event_hash=events[1].event.event_hash,
    )
    transition = reduce_p3_event(waiting, expired_event)
    assert transition.next_state.phase.value == "failed"
    assert transition.next_state.last_error_code == "approval_expired"
    assert transition.interrupt_operations[0].status.value == "expired"


def test_cli_cancel_and_incomplete_report_are_safe_errors(tmp_path, capsys) -> None:
    state_root = tmp_path / "state"
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "start",
                "--fixture",
                "water_sp_v1",
                "--new-conversation",
                "--json",
            ]
        )
        == 0
    )
    started = json.loads(capsys.readouterr().out)
    assert (
        main(["--state-root", str(state_root), "inspect", "--run", started["run_id"], "--json"])
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "cancel",
                "--run",
                started["run_id"],
                "--conversation-id",
                started["conversation_id"],
                "--expected-revision",
                "2",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["phase"] == "cancelled"
    report_path = tmp_path / "cancelled.json"
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "report",
                "--run-id",
                started["run_id"],
                "--format",
                "json",
                "--output",
                str(report_path),
                "--json",
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().err)["code"] == "cli_error"
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "verify-report",
                "--run",
                started["run_id"],
                "--report",
                str(report_path),
                "--json",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["valid"] is False


def test_report_raw_result_rejects_non_json_bytes() -> None:
    with pytest.raises(StateIntegrityError):
        _verify_report_raw_result(
            b"not-json",
            action_id=ActionId("action_00000000000000000000000000000000"),
            action_hash="0" * 64,
            execution_id=ExecutionId("execution_00000000000000000000000000000000"),
        )


def test_p3_state_contract_rejects_phase_and_reference_mismatches(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock)
    started = service.start(StartWaterRun.create(requested_at_utc=clock.now_utc()))
    state = service.inspect(started.run_id).state
    for update in (
        {"phase": WorkflowPhase.DISPATCH_PENDING},
        {"pending_interrupt_id": state.approval_interrupt_id},
        {"phase": WorkflowPhase.COMPLETED},
    ):
        invalid = state.model_copy(update=update).model_dump(mode="json")
        with pytest.raises(ValidationError):
            P3WorkflowState.model_validate(invalid, strict=True)
