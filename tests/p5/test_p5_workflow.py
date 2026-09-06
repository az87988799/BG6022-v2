from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orca_agent.application.p4_service import P4ApplicationService
from orca_agent.application.p5_errors import OutputTruncated
from orca_agent.application.p5_service import P5ApplicationService
from orca_agent.domain.ids import InterruptId, RunId
from orca_agent.domain.p4 import IdentityDecision, IdentityProvider, MoleculeInputKind
from orca_agent.domain.p5 import P5NodeKind, P5ParseStatus
from orca_agent.execution.local_backend import FakeExecutionBackend
from orca_agent.execution.orca_compiler import compile_orca_input
from orca_agent.execution.orca_parser import parse_orca_output
from orca_agent.identity.fake_pubchem import FakePubChemAdapter
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository
from orca_agent.infrastructure.p5_records import LocalJobRepository
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.p4_commands import ConfirmMoleculeIdentity, StartPlanningRun
from orca_agent.orchestration.p5_commands import ApproveP5Action, ReconcileP5Execution
from orca_agent.planning.p5_protocols import P5_PROTOCOLS
from orca_agent.planning.registry import METHOD_R2SCAN3C

BASE_TIME = datetime(2026, 9, 6, tzinfo=UTC)


class _CorruptingFakeBackend(FakeExecutionBackend):
    def start_or_reconcile(self, launch_request):
        observation = super().start_or_reconcile(launch_request)
        output = self._workdir(str(launch_request.job.execution_id)) / "stdout.out"
        output.write_bytes(output.read_bytes().replace(b"ORCA TERMINATED", b"ORCA TRUNCATED"))
        return observation


def _source(tmp_path: Path) -> tuple[Path, FrozenClock, RunId]:
    state_root = tmp_path / "state"
    clock = FrozenClock(BASE_TIME)
    service = P4ApplicationService(
        state_root,
        clock=clock,
        fake_adapter=FakePubChemAdapter(),
    )
    started = service.start(
        StartPlanningRun.create(
            input_kind=MoleculeInputKind.SMILES,
            raw_input="O",
            charge=0,
            multiplicity=1,
            provider=IdentityProvider.LOCAL,
            requested_at_utc=clock.now_utc(),
        )
    )
    assert started.accepted
    service.create_worker().run_once(limit=1)
    view = service.inspect(started.run_id)
    assert view.interrupt is not None
    assert view.candidate_bundle is not None
    candidate = view.candidate_bundle.candidates[0]
    confirmed = service.confirm(
        ConfirmMoleculeIdentity.create(
            run_id=view.run_id,
            conversation_id=view.conversation_id,
            interrupt_id=InterruptId(view.interrupt["interrupt_id"]),
            expected_revision=view.revision,
            query_id=view.query.query_id,
            query_hash=view.query.query_hash,
            candidate_bundle_id=view.candidate_bundle.record_id,
            candidate_bundle_hash=view.candidate_bundle.bundle_hash,
            candidate_set_hash=view.candidate_bundle.candidate_set_hash,
            candidate_id=candidate.candidate_id,
            candidate_hash=candidate.candidate_hash,
            decision=IdentityDecision.ACCEPT,
            requested_at_utc=clock.now_utc(),
        )
    )
    assert confirmed.accepted
    assert service.inspect(started.run_id).state.phase.value == "plan_ready"
    return state_root, clock, started.run_id


def _prepare(
    tmp_path: Path,
    protocol_id: str,
    *,
    external_opt_result_id=None,
) -> tuple[P5ApplicationService, FrozenClock, object]:
    state_root, clock, source_run_id = _source(tmp_path)
    service = P5ApplicationService(state_root, clock=clock)
    prepared = service.prepare_execution(
        source_run_id=source_run_id,
        protocol_id=protocol_id,
        external_opt_result_id=external_opt_result_id,
    )
    assert prepared.accepted, prepared.model_dump(mode="json")
    return service, clock, service.inspect(prepared.run_id)


def _approve_and_run(service: P5ApplicationService, view):
    assert view.action is not None
    assert view.binding is not None
    approved = service.approve(
        run_id=view.run_id,
        conversation_id=view.conversation_id,
        action_id=view.action.action_id,
        action_hash=view.action.action_hash,
        binding_hash=view.binding.binding_hash,
        envelope_hash=view.action.envelope_hash,
        budget_hash=view.action.budget_hash,
        expected_revision=view.revision,
    )
    assert approved.accepted, approved.model_dump(mode="json")
    report = service.create_worker().run_once(run_id=view.run_id)
    assert len(report) == 1
    return service.inspect(view.run_id)


def _run_all_actions(service: P5ApplicationService, view):
    while view.state.phase.value == "awaiting_execution_approval":
        view = _approve_and_run(service, view)
    return view


@pytest.mark.parametrize(
    ("protocol_id", "expected_nodes"),
    tuple((item.protocol_id, len(item.nodes)) for item in P5_PROTOCOLS if not item.source_from_opt),
)
def test_all_p5_protocols_are_closed_and_fake_executable(tmp_path, protocol_id, expected_nodes):
    service, _clock, view = _prepare(tmp_path, protocol_id)
    if protocol_id == "p5.freq_from_opt.r2scan3c.v1":
        pytest.skip("freq_from_opt requires an explicit completed Opt source")
    final = _run_all_actions(service, view)
    assert final.state.phase.value == "completed"
    assert len(final.results) == expected_nodes
    assert all(result.data_origin.value == "fake_fixture" for result in final.results)
    assert all(result.scientific_assessment == "not_evaluated" for result in final.results)
    assert all(result.claim_status == "not_generated" for result in final.results)
    with sqlite3.connect(resolve_database_path(service.state_root)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_jobs").fetchone()[0] == expected_nodes


def test_freq_from_opt_requires_and_uses_a_completed_opt_source(tmp_path):
    state_root, clock, source_run_id = _source(tmp_path)
    service = P5ApplicationService(state_root, clock=clock)
    opt = service.prepare_execution(
        source_run_id=source_run_id,
        protocol_id="p5.opt_only.r2scan3c.v1",
    )
    opt_view = _run_all_actions(service, service.inspect(opt.run_id))
    assert opt_view.results[0].primitive is P5NodeKind.OPT
    freq = service.prepare_execution(
        source_run_id=source_run_id,
        protocol_id="p5.freq_from_opt.r2scan3c.v1",
        external_opt_result_id=opt_view.results[0].record_id,
    )
    assert freq.accepted
    freq_view = service.inspect(freq.run_id)
    assert freq_view.binding is not None
    assert freq_view.binding.upstream_result_id == opt_view.results[0].record_id
    final = _run_all_actions(service, freq_view)
    assert final.state.phase.value == "completed"
    assert final.results[0].primitive is P5NodeKind.FREQ


def test_approval_binding_mismatch_and_command_replay_are_fenced(tmp_path):
    service, clock, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    assert view.action is not None and view.binding is not None
    rejected = service.approve(
        run_id=view.run_id,
        conversation_id=view.conversation_id,
        action_id=view.action.action_id,
        action_hash="0" * 64,
        binding_hash=view.binding.binding_hash,
        envelope_hash=view.action.envelope_hash,
        budget_hash=view.action.budget_hash,
        expected_revision=view.revision,
    )
    assert rejected.accepted is False
    assert rejected.code == "approval_mismatch"
    command = ApproveP5Action.create(
        run_id=view.run_id,
        conversation_id=view.conversation_id,
        action_id=view.action.action_id,
        action_hash=view.action.action_hash,
        binding_hash=view.binding.binding_hash,
        envelope_hash=view.action.envelope_hash,
        budget_hash=view.action.budget_hash,
        expected_revision=view.revision,
        requested_at_utc=clock.now_utc(),
    )
    first = service.approve(
        run_id=command.run_id,
        conversation_id=command.conversation_id,
        action_id=command.action_id,
        action_hash=command.action_hash,
        binding_hash=command.binding_hash,
        envelope_hash=command.envelope_hash,
        budget_hash=command.budget_hash,
        expected_revision=command.expected_revision,
        command_id=command.command_id,
    )
    second = service.approve(
        run_id=command.run_id,
        conversation_id=command.conversation_id,
        action_id=command.action_id,
        action_hash=command.action_hash,
        binding_hash=command.binding_hash,
        envelope_hash=command.envelope_hash,
        budget_hash=command.budget_hash,
        expected_revision=command.expected_revision,
        command_id=command.command_id,
    )
    assert first.accepted and second.accepted
    assert second.revision == first.revision
    assert service.create_worker().run_once(run_id=view.run_id)
    assert service.create_worker().run_once(run_id=view.run_id) == ()


def test_owned_artifacts_keep_same_bytes_separate_by_owner(tmp_path):
    state_root, clock, source_run_id = _source(tmp_path)
    service = P5ApplicationService(state_root, clock=clock)
    first = service.prepare_execution(
        source_run_id=source_run_id,
        protocol_id="p5.sp_initial.r2scan3c.v1",
    )
    second = service.prepare_execution(
        source_run_id=source_run_id,
        protocol_id="p5.sp_initial.r2scan3c.v1",
    )
    assert first.accepted and second.accepted
    first_view = service.inspect(first.run_id)
    second_view = service.inspect(second.run_id)
    assert first_view.binding is not None and second_view.binding is not None
    assert first_view.binding.geometry_artifact_id != second_view.binding.geometry_artifact_id
    with sqlite3.connect(resolve_database_path(state_root)) as connection:
        rows = connection.execute(
            "SELECT relative_path FROM artifacts WHERE run_id IN (?, ?) ORDER BY relative_path",
            (str(first.run_id), str(second.run_id)),
        ).fetchall()
    assert len(rows) >= 4
    assert len({row[0] for row in rows}) == len(rows)


def test_compiler_and_parser_reject_open_text_and_truncated_output(tmp_path):
    service, _clock, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    assert view.geometry
    node = view.plan.nodes[0]
    compiled = compile_orca_input(
        node,
        METHOD_R2SCAN3C,
        view.geometry[0],
        node.budget,
        {"parallel": False, "implicit_threads": 1},
    )
    assert b"r2SCAN-3c TightSCF SP" in compiled.input_bytes
    assert b"$new_job" not in compiled.input_bytes
    output = b"Program Version 6.1.0\nSCF CONVERGED\nFINAL SINGLE POINT ENERGY -75.0\n"
    with pytest.raises(OutputTruncated):
        parse_orca_output(
            output,
            primitive=node,
            geometry=view.geometry[0],
            input_manifest_hash=compiled.manifest_hash,
            exit_code=0,
        )


def test_local_job_terminal_state_is_immutable(tmp_path):
    service, _clock, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    assert view.action is not None and view.binding is not None
    final = _run_all_actions(service, view)
    assert final.job is not None
    with SQLiteUnitOfWork(service.database_path) as uow:
        uow.begin()
        job = LocalJobRepository(uow.connection)
        assert job.get_by_action(view.action.action_id) is not None
        with pytest.raises(sqlite3.IntegrityError):
            uow.connection.execute(
                "UPDATE local_jobs SET status = 'failed' WHERE execution_id = ?",
                (str(final.job.execution_id),),
            )
        uow.commit()


def test_reconcile_request_is_recorded_and_replays_after_terminal_completion(tmp_path):
    service, clock, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    final = _run_all_actions(service, view)
    command = ReconcileP5Execution.create(
        run_id=final.run_id,
        expected_revision=final.revision,
        requested_at_utc=clock.now_utc(),
    )
    first = service.reconcile(
        final.run_id,
        command_id=command.command_id,
        expected_revision=command.expected_revision,
    )
    second = service.reconcile(
        final.run_id,
        command_id=command.command_id,
        expected_revision=command.expected_revision,
    )
    assert first.accepted and first.code == "nothing_to_reconcile"
    assert second.model_dump(mode="json") == first.model_dump(mode="json")


def test_rejected_output_is_archived_and_closes_the_execution(tmp_path):
    service, _clock, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    service.backend = _CorruptingFakeBackend(service.state_root)
    final = _approve_and_run(service, view)
    assert final.state.phase.value == "failed"
    assert final.results[-1].parse_status is P5ParseStatus.REJECTED
    assert final.results[-1].stdout_artifact_id is not None
    assert final.job is not None and final.job.status.value == "failed"


def test_downstream_action_binds_the_exact_optimized_xyz_bytes(tmp_path):
    service, _clock, view = _prepare(tmp_path, "p5.opt_freq_sp.r2scan3c.v1")
    after_opt = _approve_and_run(service, view)
    assert after_opt.state.phase.value == "awaiting_execution_approval"
    assert len(after_opt.results) == 1
    assert after_opt.results[0].optimized_geometry_artifact_id is not None
    assert after_opt.binding is not None
    with SQLiteUnitOfWork(service.database_path) as uow:
        uow.begin()
        artifacts = ArtifactRecordRepository(uow.connection)
        optimized = artifacts.get(after_opt.results[0].optimized_geometry_artifact_id)
        downstream = artifacts.get(after_opt.binding.geometry_artifact_id)
        assert optimized is not None and downstream is not None
        store = ArtifactStore(service.state_root)
        assert store.read(optimized) == store.read(downstream)
        assert after_opt.binding.xyz_bytes_sha256 == downstream.content_hash
        uow.commit()
