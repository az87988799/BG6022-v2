import json
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta

import pytest

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.p6_service import P6ApplicationService
from orca_agent.domain.p6 import P6EvidenceRecord, P6Phase
from orca_agent.orchestration.p6_commands import AssessP6Run, CancelP6Run
from orca_agent.reporting.p6_renderer import P6ReportRenderer
from tests.p5.test_p5_workflow import (
    _approve_and_run,
    _CorruptingFakeBackend,
    _prepare,
    _run_all_actions,
    _source,
)
from tests.p6.test_p6_workflow import _fake_sp_source


def test_valid_hash_wrong_energy_is_rejected_by_unmodified_renderer(tmp_path, monkeypatch):
    import orca_agent.application.p6_service as module

    service, clock, source = _fake_sp_source(tmp_path)
    p6, run = _derive(service, clock, source)
    original = module.load_source_bundle

    def injected(*args, **kwargs):
        bundle = original(*args, **kwargs)
        records = []
        for item in bundle.evidence:
            if item.quantity == "electronic_energy":
                fields = item.model_dump(mode="python", exclude={"evidence_hash"})
                fields.update(
                    value=-999.0,
                    raw_value_token="-999.0",
                    locator=item.locator,
                    context=item.context,
                )
                item = P6EvidenceRecord.create(**fields)
            records.append(item)
        return replace(bundle, evidence=tuple(records))

    with monkeypatch.context() as patch:
        patch.setattr(module, "load_source_bundle", injected)
        assert p6.create_worker().run_once(run_id=run)[0].outcome == "succeeded"
    assert any(item.value == -999.0 for item in p6.inspect(run).evidence)
    assert p6.create_worker().run_once(run_id=run)[0].outcome == "retry"
    assert p6.inspect(run).report_manifest is None
    assert not P6ReportRenderer(p6.database_path, p6.state_root).verify(run)["valid"]


def test_publication_crash_rolls_back_records_and_recovers(tmp_path, monkeypatch):
    from orca_agent.infrastructure.p6_records import P6RecordRepository

    service, clock, source = _fake_sp_source(tmp_path)
    p6, run = _derive(service, clock, source)
    original = P6RecordRepository.append_p6

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated crash after first business insert")

    with monkeypatch.context() as patch:
        patch.setattr(P6RecordRepository, "append_p6", crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            p6.create_worker().run_once(run_id=run)
    view = p6.inspect(run)
    assert view.assessment is None and view.evidence == () and view.revision == 1
    clock.advance(timedelta(minutes=10))
    assert [r.outcome for r in p6.create_worker().run_once(run_id=run, limit=2)] == [
        "succeeded",
        "succeeded",
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", "gibbs_free_energy"),
        ("unit", "kJ/mol"),
        ("subject_result_ids", ("workflow_" + "f" * 32,)),
    ],
)
def test_rehashed_claim_semantic_mutations_are_rejected(field, value):
    from orca_agent.domain.ids import WorkflowRecordId, new_id
    from orca_agent.domain.p6 import P6ClaimRecord
    from orca_agent.science.claims import build_energy_claim, validate_claim
    from orca_agent.science.policy import get_policy
    from tests.p6.test_p6_science import _assessment, _context, _evidence

    context = _context()
    result_id = new_id(WorkflowRecordId)
    evidence = _evidence(result_id=result_id, context=context, value=-76.4)
    assessment = _assessment(result_id=result_id, evidence=evidence, context=context)
    policy = get_policy("p6.nonlinear.r2scan3c.v1")
    claim = build_energy_claim(
        assessment=assessment, policy=policy, evidence=evidence, subject_result_id=result_id
    )
    fields = claim.model_dump(mode="python", exclude={"claim_hash"})
    fields[field] = value
    mutated = P6ClaimRecord.create(**fields)
    with pytest.raises(StateIntegrityError):
        validate_claim(
            mutated, evidence={evidence.evidence_id: evidence}, assessment=assessment, policy=policy
        )


@pytest.mark.parametrize("smiles,dimension", [("O", 9), ("CCO", 27)])
def test_complete_modes_fixture_chain_is_explicitly_qualified(tmp_path, smiles, dimension):
    from orca_agent.application.p5_service import P5ApplicationService
    from orca_agent.execution.local_backend import FakeExecutionBackend

    class CompleteModesBackend(FakeExecutionBackend):
        def start_or_reconcile(self, request):
            observation = super().start_or_reconcile(request)
            if request.node.kind.value == "freq":
                directory = self._workdir(str(request.job.execution_id))
                hess = directory / "input.hess"
                values = [0.0] * 6 + [100.0 + i for i in range(dimension - 6)]
                raw = hess.read_text()
                start = raw.index("$vibrational_frequencies")
                end = raw.index("$atoms", start)
                block = ["$vibrational_frequencies", str(dimension)]
                block += [f"{i} {v:.6f}" for i, v in enumerate(values)]
                block += [
                    "$normal_modes",
                    f"{dimension} {dimension}",
                    " ".join(map(str, range(dimension))),
                ]
                block += [
                    f"{i} "
                    + " ".join("1.0" if i == j and j >= 6 else "0.0" for j in range(dimension))
                    for i in range(dimension)
                ]
                hess.write_text(raw[:start] + "\n".join(block) + "\n" + raw[end:])
                stdout = directory / "stdout.out"
                raw = stdout.read_text()
                start = raw.index("VIBRATIONAL FREQUENCIES")
                end = raw.index("****ORCA TERMINATED", start)
                stdout.write_text(
                    raw[:start]
                    + "VIBRATIONAL FREQUENCIES\n"
                    + "\n".join(f"{i}: {v:.6f} cm**-1" for i, v in enumerate(values))
                    + "\n"
                    + raw[end:]
                )
            return observation

    root, clock, identity = _source(tmp_path, smiles=smiles)
    service = P5ApplicationService(root, clock=clock, backend=CompleteModesBackend(root))
    prepared = service.prepare_execution(
        source_run_id=identity, protocol_id="p5.opt_freq_sp.r2scan3c.v1"
    )
    source = _run_all_actions(service, service.inspect(prepared.run_id))
    p6, run = _derive(service, clock, source)
    assert [r.outcome for r in p6.create_worker().run_once(run_id=run, limit=2)] == [
        "succeeded",
        "succeeded",
    ]
    view = p6.inspect(run)
    assert len(view.assessment.mode_classifications) == dimension
    assert all(claim.status.value == "qualified" for claim in view.claims)


def _derive(service, clock, source):
    p6 = P6ApplicationService(service.state_root, clock=clock)
    result = p6.assess(
        AssessP6Run.create(source_p5_run_id=source.run_id, requested_at_utc=clock.now_utc())
    )
    assert result.accepted, result
    return p6, result.run_id


def test_expired_lease_cannot_publish_scientific_records(tmp_path):
    service, clock, source = _fake_sp_source(tmp_path)
    p6, run = _derive(service, clock, source)
    worker = p6.create_worker(lease_duration=timedelta(seconds=1))
    original = worker.handler

    def expired(permit):
        clock.advance(timedelta(seconds=2))
        return original(permit)

    worker.handler = expired
    assert worker.run_once(run_id=run)[0].outcome == "lease_lost"
    view = p6.inspect(run)
    assert view.assessment is None and view.claims == ()
    assert view.revision == 1
    assert len(p6.create_worker().run_once(run_id=run, limit=2)) == 2


@pytest.mark.parametrize("report_stage", [False, True])
def test_cancellation_between_handler_and_completion_does_not_poison_database(
    tmp_path, report_stage
):
    service, clock, source = _fake_sp_source(tmp_path)
    p6, run = _derive(service, clock, source)
    if report_stage:
        assert p6.create_worker().run_once(run_id=run)[0].outcome == "succeeded"
    worker = p6.create_worker()
    original = worker.handler

    def cancelled(permit):
        result = original(permit)
        view = p6.inspect(run)
        assert p6.cancel(
            CancelP6Run.create(
                run_id=run,
                conversation_id=view.conversation_id,
                expected_revision=view.revision,
                requested_at_utc=clock.now_utc(),
            )
        ).accepted
        return result

    worker.handler = cancelled
    assert worker.run_once(run_id=run)[0].outcome == "lease_lost"
    assert p6.inspect(run).state.phase is P6Phase.CANCELLED
    assert p6.inspect(run).report_manifest is None
    assert (p6.inspect(run).assessment is not None) == report_stage
    _, second = _derive(service, clock, source)
    assert len(p6.create_worker().run_once(limit=2)) == 2
    assert p6.inspect(second).state.phase is P6Phase.COMPLETED


def test_completed_report_cannot_be_cancelled(tmp_path):
    service, clock, source = _fake_sp_source(tmp_path)
    p6, run = _derive(service, clock, source)
    p6.create_worker().run_once(run_id=run, limit=2)
    view = p6.inspect(run)
    assert not p6.cancel(
        CancelP6Run.create(
            run_id=run,
            conversation_id=view.conversation_id,
            expected_revision=view.revision,
            requested_at_utc=clock.now_utc(),
        )
    ).accepted
    assert P6ReportRenderer(p6.database_path, p6.state_root).verify(run)["valid"]


def test_second_worker_wins_expired_generation_only_once(tmp_path):
    service, clock, source = _fake_sp_source(tmp_path)
    p6, run = _derive(service, clock, source)
    worker = p6.create_worker(lease_duration=timedelta(seconds=1))
    original = worker.handler

    def interleaved(permit):
        result = original(permit)
        clock.advance(timedelta(seconds=2))
        assert [x.outcome for x in p6.create_worker().run_once(run_id=run, limit=2)] == [
            "succeeded",
            "succeeded",
        ]
        return result

    worker.handler = interleaved
    assert worker.run_once(run_id=run)[0].outcome == "lease_lost"
    assert p6.inspect(run).revision == 3
    assert p6.create_worker().run_once(run_id=run) == ()
    assert P6ReportRenderer(p6.database_path, p6.state_root).verify(run)["valid"]


@pytest.mark.parametrize("mutation", ["integrity", "isotope"])
def test_comparison_rejects_invalid_or_isotope_mismatched_inputs(mutation):
    from orca_agent.domain.ids import WorkflowRecordId, new_id
    from orca_agent.science.comparability import compare_electronic_energy
    from tests.p6.test_p6_science import _assessment, _context, _evidence

    contexts = [_context(), _context()]
    if mutation == "isotope":
        fields = contexts[1].model_dump(mode="python", exclude={"context_hash"})
        fields["isotope_policy"] = "explicit_deuterium"
        contexts[1] = type(contexts[1]).create(**fields)
    results = [new_id(WorkflowRecordId), new_id(WorkflowRecordId)]
    evidence = [
        _evidence(result_id=r, context=c, value=-76.4)
        for r, c in zip(results, contexts, strict=True)
    ]
    assessments = [
        _assessment(result_id=r, evidence=e, context=c)
        for r, e, c in zip(results, evidence, contexts, strict=True)
    ]
    if mutation == "integrity":
        assessments = [
            type(a).create(
                **{
                    **a.model_dump(mode="python", exclude={"assessment_hash"}),
                    "integrity_verified": False,
                    "checks": a.checks,
                }
            )
            for a in assessments
        ]
    result = compare_electronic_energy(
        candidate_assessment=assessments[0],
        candidate_context=contexts[0],
        candidate_evidence=evidence[0],
        reference_assessment=assessments[1],
        reference_context=contexts[1],
        reference_evidence=evidence[1],
    )
    assert result.status.value == "incompatible" and result.delta_energy is None


def test_unverified_minimum_support_and_relabelled_energy_are_rejected():
    from orca_agent.domain.ids import WorkflowRecordId, new_id
    from orca_agent.domain.p6 import P6ClaimStatus, P6MinimumStatus
    from orca_agent.science.claims import build_energy_claim, build_minimum_claim, validate_claim
    from orca_agent.science.policy import get_policy
    from tests.p6.test_p6_science import _assessment, _context, _evidence

    context, result = _context(), new_id(WorkflowRecordId)
    evidence = _evidence(result_id=result, context=context, value=-76.4)
    assessment = _assessment(result_id=result, evidence=evidence, context=context)
    policy = get_policy("p6.nonlinear.r2scan3c.v1")
    wrong_unit = _evidence(result_id=result, context=context, value=-76.4, unit="kJ/mol")
    with pytest.raises(StateIntegrityError):
        build_energy_claim(
            assessment=assessment, policy=policy, evidence=wrong_unit, subject_result_id=result
        )
    fields = assessment.model_dump(mode="python", exclude={"assessment_hash"})
    fields.update(minimum_status=P6MinimumStatus.INCONCLUSIVE, checks=assessment.checks)
    assessment = type(assessment).create(**fields)
    claim = build_minimum_claim(
        assessment=assessment,
        policy=policy,
        subject_result_id=result,
        evidence_hashes=(evidence.evidence_hash,),
    )
    fields = claim.model_dump(mode="python", exclude={"claim_hash"})
    fields["status"] = P6ClaimStatus.SUPPORTED
    with pytest.raises(StateIntegrityError):
        validate_claim(
            type(claim).create(**fields),
            evidence={evidence.evidence_id: evidence},
            assessment=assessment,
            policy=policy,
        )


def test_orca_version_is_compared_with_raw_reparse(tmp_path):
    from orca_agent.evidence.p6_ingestion import _compare_p5_result, load_source_bundle
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

    service, _, source = _fake_sp_source(tmp_path)
    with SQLiteUnitOfWork(service.database_path) as uow:
        bundle = load_source_bundle(
            connection=uow.connection, state_root=service.state_root, source_run_id=source.run_id
        ).results[0]
        with pytest.raises(StateIntegrityError, match="source_reparse_mismatch"):
            parsed = type(bundle.parsed).model_validate(
                {**bundle.parsed.model_dump(mode="python"), "orca_version": "0.0.0"}
            )
            _compare_p5_result(bundle.result, parsed)


def test_external_opt_reference_freezes_original_owner(tmp_path):
    from orca_agent.application.p5_service import P5ApplicationService

    root, clock, identity_run = _source(tmp_path)
    service = P5ApplicationService(root, clock=clock)
    prepared = service.prepare_execution(
        source_run_id=identity_run, protocol_id="p5.opt_only.r2scan3c.v1"
    )
    opt = _run_all_actions(service, service.inspect(prepared.run_id))
    prepared = service.prepare_execution(
        source_run_id=identity_run,
        protocol_id="p5.freq_from_opt.r2scan3c.v1",
        external_opt_result_id=opt.results[0].record_id,
    )
    freq = _run_all_actions(service, service.inspect(prepared.run_id))
    p6, run = _derive(service, clock, freq)
    assert [x.outcome for x in p6.create_worker().run_once(run_id=run, limit=2)] == [
        "succeeded",
        "succeeded",
    ]
    assert opt.run_id in {x.owner_run_id for x in p6.inspect(run).source_snapshot.artifacts}
    assert P6ReportRenderer(p6.database_path, p6.state_root).verify(run)["valid"]


def test_archive_is_portable_and_required_manifests_are_verified(tmp_path, capsys):
    from scripts.export_p6_evidence import main as export
    from scripts.verify_p6_evidence import main as verify

    service, clock, source = _fake_sp_source(tmp_path)
    p6, run = _derive(service, clock, source)
    # A genuinely fresh interpreter resumes the persisted workflow.
    command = [
        sys.executable,
        "-m",
        "orca_agent",
        "--state-root",
        str(p6.state_root),
        "worker",
        "--workflow",
        "p6",
        "--run-id",
        str(run),
        "--drain",
        "--json",
    ]
    process = subprocess.run(command, capture_output=True, text=True, check=True)
    assert [x["outcome"] for x in json.loads(process.stdout)["reports"]] == [
        "succeeded",
        "succeeded",
    ]
    packet = tmp_path / "packet"
    assert (
        export(["--state-root", str(p6.state_root), "--run-id", str(run), "--output", str(packet)])
        == 0
    )
    args = ["--mode", "archived_packet", "--run-id", str(run), "--evidence-root", str(packet)]
    assert verify(args) == 0, capsys.readouterr().out
    original = (packet / "manifest.json").read_bytes()
    (packet / "manifest.json").unlink()
    assert verify(args) == 2
    (packet / "manifest.json").write_bytes(original)
    (packet / "packet_manifest.json").write_text("{}", encoding="utf-8")
    assert verify(args) == 2


@pytest.mark.parametrize("protocol", ["p5.opt_freq_sp.r2scan3c.v1", "p5.sp_initial.r2scan3c.v1"])
def test_full_fixture_and_confirmed_failure_are_reportable(tmp_path, protocol):
    service, clock, source = _prepare(tmp_path, protocol)
    failed = protocol == "p5.sp_initial.r2scan3c.v1"
    if failed:
        service.backend = _CorruptingFakeBackend(service.state_root)
        source = _approve_and_run(service, source)
    else:
        source = _run_all_actions(service, source)
    p6, run = _derive(service, clock, source)
    reports = p6.create_worker().run_once(run_id=run, limit=2)
    assert [item.outcome for item in reports] == ["succeeded", "succeeded"]
    view = p6.inspect(run)
    if failed:
        assert view.claims == ()
        assert any(item.quantity == "execution_fact" for item in view.evidence)
    assert P6ReportRenderer(p6.database_path, p6.state_root).verify(run)["valid"]
