from datetime import UTC, datetime

import pytest

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.p3_service import P3ApplicationService
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.p3 import ReportManifestV1, WorkflowPhase
from orca_agent.execution.commands import ApproveAction, StartWaterRun
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.p3_records import (
    ArtifactRecordRepository,
    P3RecordRepository,
)
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.reporting.renderer import P3ReportRenderer


def _complete_service(tmp_path):
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
    assert [item.outcome for item in service.create_worker().run_once(limit=3)] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert service.inspect(started.run_id).state.phase is WorkflowPhase.COMPLETED
    return service, started.run_id, clock


def test_report_verifier_rejects_tampered_durable_artifact(tmp_path) -> None:
    service, run_id, clock = _complete_service(tmp_path)
    renderer = P3ReportRenderer(service.database_path, service.state_root, clock=clock)
    verified = renderer.verify(run_id)
    assert verified["valid"] is True

    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        manifest_entry = P3RecordRepository(uow.connection).latest(
            run_id=run_id,
            record_type="report_manifest",
            model_type=ReportManifestV1,
        )
        assert manifest_entry is not None
        artifact = ArtifactRecordRepository(uow.connection).get(manifest_entry[1].json_artifact_id)
        assert artifact is not None
        path = renderer.artifacts.path_for(artifact.relative_path)
        uow.commit()

    path.write_bytes(b'{"fake_marker":"fake_fixture_only"}')
    with pytest.raises(StateIntegrityError):
        renderer.verify(run_id)
