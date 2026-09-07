"""Execution contract and recovery regressions derived from the P5 audit."""

from datetime import timedelta

import pytest

from orca_agent.application.p5_service import P5ApplicationService
from orca_agent.domain.ids import CommandId, ConversationId, RunId, WorkflowRecordId, new_id
from orca_agent.infrastructure.p3_records import ActionRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from tests.p5.test_p5_workflow import _approve_and_run, _prepare, _source


def _approve(service, view, **overrides):
    values = dict(
        run_id=view.run_id,
        conversation_id=view.conversation_id,
        action_id=view.action.action_id,
        action_hash=view.action.action_hash,
        binding_hash=view.binding.binding_hash,
        envelope_hash=view.action.envelope_hash,
        budget_hash=view.action.budget_hash,
        expected_revision=view.revision,
    )
    return service.approve(**{**values, **overrides})


@pytest.mark.parametrize("field", ["action_hash", "binding_hash", "envelope_hash", "budget_hash"])
def test_each_approval_hash_is_fenced(tmp_path, field):
    service, _, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    assert not _approve(service, view, **{field: "f" * 64}).accepted
    assert service.create_worker().run_once(run_id=view.run_id)[0].physical_start_count == 0


def test_cancel_before_dispatch_cancels_pending_effect_and_replays(tmp_path):
    service, _, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    assert _approve(service, view).accepted
    view = service.inspect(view.run_id)
    command = new_id(CommandId)
    args = dict(
        run_id=view.run_id,
        conversation_id=view.conversation_id,
        expected_revision=view.revision,
        command_id=command,
    )
    first = service.cancel(**args)
    assert first.accepted
    assert service.cancel(**args) == first
    assert service.create_worker().run_once(run_id=view.run_id) == ()
    with SQLiteUnitOfWork(service.state_root) as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM local_jobs").fetchone()[0] == 0
        effects = uow.outbox.list_for_run(view.run_id)
        assert effects[0].status.value == "cancelled"


def test_expired_approval_never_reserves_or_launches(tmp_path):
    service, clock, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    assert _approve(service, view).accepted
    clock.advance(timedelta(hours=25))
    reports = service.create_worker().run_once(run_id=view.run_id)
    assert reports[0].outcome == "approval_expired"
    assert service.inspect(view.run_id).job is None


def test_wrong_owner_and_stale_revision_are_rejected(tmp_path):
    service, _, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    assert not _approve(service, view, conversation_id=new_id(ConversationId)).accepted
    assert not _approve(service, view, expected_revision=0).accepted
    assert not service.cancel(
        run_id=view.run_id, conversation_id=new_id(ConversationId), expected_revision=view.revision
    ).accepted
    assert not service.reconcile(view.run_id, expected_revision=0).accepted


def test_shared_validated_action_and_outbox_are_durable(tmp_path):
    service, _, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    final = _approve_and_run(service, view)
    with SQLiteUnitOfWork(service.state_root) as uow:
        stored = ActionRepository(uow.connection).get(view.action.action_id)
        assert stored.action.action_hash == view.action.action_hash
        assert stored.action.primitive.parameters["binding_hash"] == view.binding.binding_hash
        effects = uow.outbox.list_for_run(view.run_id)
        assert len(effects) == 2 and all(e.status.value == "succeeded" for e in effects)
        assert effects[0].terminal_generation == 1 and effects[0].audit_event_id is not None
    assert final.results[0].scientific_assessment == "not_evaluated"


def test_export_has_no_execution_side_effect_and_rejects_unknown_format(tmp_path):
    service, _, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    assert "not_evaluated" in service.export_execution(view.run_id, format="md")
    assert service.export_execution(view.run_id)["state"]["phase"] == "awaiting_execution_approval"
    with pytest.raises(ValueError):
        service.export_execution(view.run_id, format="html")
    assert service.inspect(view.run_id).revision == view.revision


@pytest.mark.parametrize("smiles", ["O", "CCO", "c1ccccc1", "C[C@H](O)F", "F/C=C/F"])
def test_geometry_preparation_preserves_confirmed_identity(tmp_path, smiles):
    root, clock, source = _source(tmp_path, smiles)
    service = P5ApplicationService(root, clock=clock)
    result = service.prepare_execution(
        source_run_id=source, protocol_id="p5.sp_initial.r2scan3c.v1"
    )
    assert result.accepted
    view = service.inspect(result.run_id)
    assert view.geometry[0].canonical_isomeric_smiles
    assert view.geometry[0].seed == 6022
    assert "H" in view.geometry[0].atom_symbols


def test_missing_source_and_external_result_are_rejected(tmp_path):
    service, _, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    assert not service.prepare_execution(
        source_run_id=new_id(RunId), protocol_id="p5.sp_initial.r2scan3c.v1"
    ).accepted
    assert not service.prepare_execution(
        source_run_id=view.state.source_run_id,
        protocol_id="p5.freq_from_opt.r2scan3c.v1",
        external_opt_result_id=new_id(WorkflowRecordId),
    ).accepted
    assert not service.prepare_execution(
        source_run_id=view.state.source_run_id, protocol_id="unknown"
    ).accepted


def test_replaying_prepare_does_not_regenerate_geometry(tmp_path):
    root, clock, source = _source(tmp_path)
    service = P5ApplicationService(root, clock=clock)
    args = dict(
        source_run_id=source,
        run_id=new_id(RunId),
        command_id=new_id(CommandId),
        protocol_id="p5.sp_initial.r2scan3c.v1",
    )
    first = service.prepare_execution(**args)
    before = service.inspect(first.run_id).geometry
    assert service.prepare_execution(**args) == first
    assert service.inspect(first.run_id).geometry == before
    assert not service.prepare_execution(**{**args, "wall_time_seconds": 1}).accepted


def test_worker_crash_after_observation_acknowledges_without_second_launch(tmp_path, monkeypatch):
    from orca_agent.application.p5_dispatch import P5EffectCompletion

    service, clock, view = _prepare(tmp_path, "p5.opt_freq_sp.r2scan3c.v1")
    assert _approve(service, view).accepted
    complete = P5EffectCompletion.complete

    def crash(*args):
        raise RuntimeError("controlled worker failure before effect completion")

    monkeypatch.setattr(P5EffectCompletion, "complete", crash)
    service.create_worker().run_once(run_id=view.run_id)
    observed = service.inspect(view.run_id)
    assert observed.job.launch_consumed_at_utc is not None
    assert observed.state.phase.value == "collecting"
    monkeypatch.setattr(P5EffectCompletion, "complete", complete)
    clock.advance(timedelta(minutes=2))

    def forbidden(*args):
        pytest.fail("acknowledgement recovery must never enter the launch gateway")

    monkeypatch.setattr(service.backend, "start_or_reconcile", forbidden)
    report = service.create_worker().run_once(run_id=view.run_id)
    assert report[0].outcome == "launch_acknowledged"
    next_node = service.inspect(view.run_id)
    assert next_node.state.phase.value == "awaiting_execution_approval"
    assert len(next_node.results) == 1
    with SQLiteUnitOfWork(service.state_root) as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM local_jobs").fetchone()[0] == 1
        effect = uow.outbox.list_for_run(view.run_id)[0]
        assert effect.status.value == "succeeded" and effect.terminal_generation == 2
