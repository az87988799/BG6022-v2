from __future__ import annotations

from orca_agent.application.p6_service import P6ApplicationService
from orca_agent.domain.p6 import P6ClaimStatus, P6MinimumStatus, P6Phase
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository
from orca_agent.infrastructure.p5_records import P5RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.p6_commands import AssessP6Run, CancelP6Run
from orca_agent.orchestration.p6_replay import replay_p6
from orca_agent.reporting.p6_renderer import P6ReportRenderer
from tests.p5.test_p5_workflow import _prepare, _run_all_actions


def _fake_sp_source(tmp_path):
    p5_service, clock, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    view = _run_all_actions(p5_service, view)
    return p5_service, clock, view


def test_p6_sp_source_is_derived_idempotent_and_keeps_p5_immutable(tmp_path) -> None:
    p5_service, clock, source = _fake_sp_source(tmp_path)
    with SQLiteUnitOfWork(p5_service.state_root, clock=clock) as uow:
        uow.begin()
        source_before = uow.runs.get_verified(source.run_id, uow.events, outbox=uow.outbox)
        p5_records_before = P5RecordRepository(uow.connection).list_p5_for_run(source.run_id)
        artifacts_before = ArtifactRecordRepository(uow.connection).list_for_run(source.run_id)
        uow.commit()

    p6 = P6ApplicationService(p5_service.state_root, clock=clock)
    command = AssessP6Run.create(source_p5_run_id=source.run_id, requested_at_utc=clock.now_utc())
    created = p6.assess(command)
    replayed = p6.assess(command)
    assert created.accepted and replayed == created

    reports = p6.create_worker().run_once(run_id=created.run_id, limit=2)
    assert [item.outcome for item in reports] == ["succeeded", "succeeded"]
    final = p6.inspect(created.run_id)
    assert final.state.phase is P6Phase.COMPLETED
    assert final.assessment is not None
    assert final.assessment.minimum_status is P6MinimumStatus.INCONCLUSIVE
    assert final.assessment.energy_available is True
    assert final.claims and all(item.status is P6ClaimStatus.QUALIFIED for item in final.claims)
    assert final.report_manifest is not None
    assert any(item.role == "p6_evidence" for item in final.report_manifest.dependencies)
    assert any(item.role == "p6_source_snapshot" for item in final.report_manifest.dependencies)

    with SQLiteUnitOfWork(p5_service.state_root, clock=clock) as uow:
        uow.begin()
        source_after = uow.runs.get_verified(source.run_id, uow.events, outbox=uow.outbox)
        p5_records_after = P5RecordRepository(uow.connection).list_p5_for_run(source.run_id)
        artifacts_after = ArtifactRecordRepository(uow.connection).list_for_run(source.run_id)
        p6_events = tuple(item.event for item in uow.events.list_for_run(created.run_id))
        uow.commit()
    assert source_after == source_before
    assert p5_records_after == p5_records_before
    assert artifacts_after == artifacts_before
    assert replay_p6(p6_events) == final.state

    verification = P6ReportRenderer(p6.database_path, p6.state_root, clock=clock).verify(
        created.run_id
    )
    assert verification["valid"] is True
    assert verification["source_reparse_verified"] is True


def test_p6_cancel_before_worker_cancels_only_derived_run(tmp_path) -> None:
    p5_service, clock, source = _fake_sp_source(tmp_path)
    p6 = P6ApplicationService(p5_service.state_root, clock=clock)
    created = p6.assess(
        AssessP6Run.create(source_p5_run_id=source.run_id, requested_at_utc=clock.now_utc())
    )
    view = p6.inspect(created.run_id)
    cancelled = p6.cancel(
        CancelP6Run.create(
            run_id=created.run_id,
            conversation_id=view.conversation_id,
            expected_revision=view.revision,
            requested_at_utc=clock.now_utc(),
        )
    )
    assert cancelled.accepted
    assert p6.create_worker().run_once(run_id=created.run_id) == ()
    assert p6.inspect(created.run_id).state.phase is P6Phase.CANCELLED
    assert p5_service.inspect(source.run_id).state.phase.value == "completed"


def test_p6_verify_report_fails_closed_when_report_bytes_are_tampered(tmp_path) -> None:
    p5_service, clock, source = _fake_sp_source(tmp_path)
    p6 = P6ApplicationService(p5_service.state_root, clock=clock)
    created = p6.assess(
        AssessP6Run.create(source_p5_run_id=source.run_id, requested_at_utc=clock.now_utc())
    )
    assert len(p6.create_worker().run_once(run_id=created.run_id, limit=2)) == 2
    view = p6.inspect(created.run_id)
    assert view.report_manifest is not None
    with SQLiteUnitOfWork(p6.database_path, clock=clock) as uow:
        uow.begin()
        artifact = ArtifactRecordRepository(uow.connection).get(
            view.report_manifest.markdown_artifact_id
        )
        assert artifact is not None
        data = ArtifactStore(p6.state_root).read(artifact)
        uow.commit()
    (p6.state_root / "artifacts" / artifact.relative_path).write_bytes(data + b"\n# tampered\n")
    verification = P6ReportRenderer(p6.database_path, p6.state_root, clock=clock).verify(
        created.run_id
    )
    assert verification["valid"] is False
    assert verification["report_bytes_verified"] is False
