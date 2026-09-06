from __future__ import annotations

import base64
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from orca_agent.application.effect_completion import EffectCompletionService
from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.p4_handlers import P4IdentityHandler
from orca_agent.application.p4_service import P4ApplicationService
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.ids import (
    CommandId,
    ConversationId,
    EventId,
    InterruptId,
    RunId,
    WorkflowRecordId,
    new_id,
)
from orca_agent.domain.p4 import (
    IdentityDecision,
    IdentityProvider,
    LookupStatus,
    MoleculeInputKind,
    MoleculeQuery,
    ResponseEnvelope,
)
from orca_agent.identity.fake_pubchem import FakePubChemAdapter
from orca_agent.identity.http_pubchem import HttpPubChemAdapter
from orca_agent.identity.ports import (
    IdentityErrorCode,
    LookupResult,
    RawIdentityCandidate,
    RetryAfter,
    normalize_input,
)
from orca_agent.identity.rdkit_normalizer import RDKitNormalizer
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.repositories import EventRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import OutboxWorker
from orca_agent.interfaces.cli import main
from orca_agent.orchestration.dispatch_policy import P4_EFFECT_REGISTRY
from orca_agent.orchestration.p4_commands import (
    CancelPlanningRun,
    ConfirmMoleculeIdentity,
    StartPlanningRun,
)
from orca_agent.orchestration.p4_replay import replay_p4, verify_p4_snapshot
from orca_agent.planning.registry import PRIMITIVE_OPTIMIZATION
from orca_agent.planning.validator import PlanValidationError, validate_ground_state_plan

BASE_TIME = datetime(2026, 9, 6, tzinfo=UTC)


def _service(
    tmp_path: Path,
    *,
    adapter=None,
    clock: FrozenClock | None = None,
) -> tuple[P4ApplicationService, FrozenClock]:
    actual_clock = clock or FrozenClock(BASE_TIME)
    return (
        P4ApplicationService(
            tmp_path / "state",
            clock=actual_clock,
            fake_adapter=adapter or FakePubChemAdapter(),
        ),
        actual_clock,
    )


def _start(
    service: P4ApplicationService,
    clock: FrozenClock,
    *,
    input_kind: MoleculeInputKind = MoleculeInputKind.NAME,
    raw_input: str = "water",
    provider: IdentityProvider = IdentityProvider.FAKE,
    charge: int = 0,
    multiplicity: int = 1,
) -> tuple[StartPlanningRun, object]:
    command = StartPlanningRun.create(
        input_kind=input_kind,
        raw_input=raw_input,
        charge=charge,
        multiplicity=multiplicity,
        provider=provider,
        requested_at_utc=clock.now_utc(),
    )
    return command, service.start(command)


def _resolve(service: P4ApplicationService, result) -> object:
    reports = service.create_worker().run_once(limit=1)
    assert len(reports) == 1
    assert reports[0].outcome == "succeeded"
    return service.inspect(result.run_id)


def _confirm(service: P4ApplicationService, clock: FrozenClock, view, *, index: int = 0):
    command = _confirmation_command(clock, view, index=index)
    return command, service.confirm(command)


def _confirmation_command(clock: FrozenClock, view, *, index: int = 0):
    assert view.interrupt is not None
    assert view.candidate_bundle is not None
    candidate = view.candidate_bundle.candidates[index]
    return ConfirmMoleculeIdentity.create(
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


@pytest.mark.parametrize("name", ("water", "ethanol", "benzene"))
def test_names_require_identity_confirmation_and_create_only_a_plan(tmp_path, name):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock, raw_input=name)
    view = _resolve(service, started)

    assert view.state.phase.value == "awaiting_identity"
    assert view.candidate_bundle is not None
    assert view.candidate_bundle.confirmable is True
    assert len(view.candidate_bundle.candidates) == 1
    assert view.candidate_bundle.candidates[0].normalization_strategy == "rdkit-normalizer-v1"

    _confirm_command, confirmed = _confirm(service, clock, view)
    assert confirmed.accepted is True
    assert confirmed.details["identity_confirmed"] is True
    assert confirmed.details["planning_valid"] is True
    assert confirmed.details["execution_ready"] is False
    assert confirmed.details["execution_approved"] is False
    assert confirmed.details["real_scientific_result"] is False

    final = service.inspect(started.run_id)
    assert final.state.phase.value == "plan_ready"
    assert final.prepared_plan is not None
    assert tuple(step.kind.value for step in final.prepared_plan.proposal.steps) == ("opt", "freq")
    assert final.diagnostics == (
        "execution_not_implemented",
        "execution_approval_required",
    )
    with sqlite3.connect(service.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("input_kind", "raw_input", "provider"),
    (
        (MoleculeInputKind.NAME, "water", IdentityProvider.FAKE),
        (MoleculeInputKind.CAS, "7732-18-5", IdentityProvider.FAKE),
        (MoleculeInputKind.CID, "962", IdentityProvider.FAKE),
        (MoleculeInputKind.SMILES, " O ", IdentityProvider.LOCAL),
    ),
)
def test_input_kinds_route_without_guessing(tmp_path, input_kind, raw_input, provider):
    service, clock = _service(tmp_path)
    _command, started = _start(
        service,
        clock,
        input_kind=input_kind,
        raw_input=raw_input,
        provider=provider,
    )
    view = _resolve(service, started)
    assert view.query.input_kind is input_kind
    assert view.query.normalized_input == (
        "O" if input_kind is MoleculeInputKind.SMILES else raw_input
    )


@pytest.mark.parametrize(
    ("input_kind", "raw_input"),
    ((MoleculeInputKind.CAS, "7732-18-6"), (MoleculeInputKind.CID, "0")),
)
def test_invalid_cas_and_cid_are_rejected_before_persistence(tmp_path, input_kind, raw_input):
    service, clock = _service(tmp_path)
    _command, result = _start(service, clock, input_kind=input_kind, raw_input=raw_input)
    assert result.accepted is False
    assert result.code == "p4_request_rejected"
    with sqlite3.connect(service.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_smiles_never_calls_the_provider(tmp_path):
    class FailingAdapter:
        def resolve(self, _query):
            raise AssertionError("SMILES must not call PubChem")

    service, clock = _service(tmp_path, adapter=FailingAdapter())
    _command, started = _start(
        service,
        clock,
        input_kind=MoleculeInputKind.SMILES,
        raw_input="CCO",
        provider=IdentityProvider.LOCAL,
    )
    view = _resolve(service, started)
    assert view.query.provider is IdentityProvider.LOCAL
    assert view.candidate_bundle is not None
    assert view.candidate_bundle.candidates[0].canonical_isomeric_smiles == "CCO"


def test_multiple_candidates_are_presented_without_default_selection(tmp_path):
    records = {
        "name:mixture": (
            RawIdentityCandidate(
                candidate_id="candidate:ethanol",
                smiles="CCO",
                molecular_formula="C2H6O",
            ),
            RawIdentityCandidate(
                candidate_id="candidate:water",
                smiles="O",
                molecular_formula="H2O",
            ),
        )
    }
    service, clock = _service(tmp_path, adapter=FakePubChemAdapter(records))
    _command, started = _start(service, clock, raw_input="mixture")
    view = _resolve(service, started)
    assert view.state.phase.value == "awaiting_identity"
    assert view.candidate_bundle is not None
    assert len(view.candidate_bundle.candidates) == 2
    assert view.confirmed_molecule is None

    _confirm_command, confirmed = _confirm(service, clock, view, index=1)
    assert confirmed.accepted is True
    assert service.inspect(started.run_id).confirmed_molecule.candidate_id == "candidate:water"


def test_candidate_count_is_limited_without_truncation(tmp_path):
    candidates = tuple(
        RawIdentityCandidate(
            candidate_id=f"candidate:{index}",
            smiles="O",
            molecular_formula="H2O",
        )
        for index in range(11)
    )
    service, clock = _service(
        tmp_path,
        adapter=FakePubChemAdapter(records={"name:too-many": candidates}),
    )
    _command, started = _start(service, clock, raw_input="too-many")
    reports = service.create_worker().run_once(limit=1)
    assert reports[0].outcome == "succeeded"
    view = service.inspect(started.run_id)
    assert view.state.phase.value == "failed"
    assert view.candidate_bundle is None
    assert view.interrupt is None
    assert view.attempts[0].status is LookupStatus.FAILED


def test_equivalent_smiles_share_structure_hash_but_stereo_does_not():
    normalizer = RDKitNormalizer()
    first = normalizer.normalize("C(O)C", charge=0, multiplicity=1)
    second = normalizer.normalize("CCO", charge=0, multiplicity=1)
    assert first.structure_hash == second.structure_hash

    enantiomer_a = normalizer.normalize("C[C@H](F)Cl", charge=0, multiplicity=1)
    enantiomer_b = normalizer.normalize("C[C@@H](F)Cl", charge=0, multiplicity=1)
    assert enantiomer_a.canonical_isomeric_smiles != enantiomer_b.canonical_isomeric_smiles
    assert enantiomer_a.structure_hash != enantiomer_b.structure_hash

    e_isomer = normalizer.normalize("F/C=C/F", charge=0, multiplicity=1)
    z_isomer = normalizer.normalize("F/C=C\\F", charge=0, multiplicity=1)
    unspecified = normalizer.normalize("FC=CF", charge=0, multiplicity=1)
    assert e_isomer.canonical_isomeric_smiles != z_isomer.canonical_isomeric_smiles
    assert e_isomer.structure_hash != z_isomer.structure_hash
    assert unspecified.stereo_status == "unspecified"
    assert unspecified.checks["protocol_range_supported"] is False

    isotope = normalizer.normalize("[13CH3]CO", charge=0, multiplicity=1)
    ordinary = normalizer.normalize("CCO", charge=0, multiplicity=1)
    assert isotope.structure_hash != ordinary.structure_hash


def test_protocol_range_reports_unsupported_without_execution():
    normalizer = RDKitNormalizer()
    charged = normalizer.normalize("[NH4+]", charge=1, multiplicity=1)
    assert charged.checks["protocol_range_supported"] is True
    assert charged.formal_charge == 1


def test_p4_contracts_require_ids_versions_and_reject_tampering(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    view = _resolve(service, started)
    assert view.attempts
    attempt = view.attempts[0]
    with pytest.raises(ValidationError):
        MoleculeQuery.model_validate(
            view.query.model_dump(mode="json", exclude={"query_id"}), strict=True
        )
    with pytest.raises(ValidationError):
        MoleculeQuery.model_validate(
            view.query.model_dump(mode="json", exclude={"schema_version"}), strict=True
        )

    artifact = view.attempts[0].response_artifact_id
    assert artifact is not None
    with sqlite3.connect(service.database_path) as connection:
        row = connection.execute(
            "SELECT record_json FROM workflow_records WHERE record_id = ?",
            (str(attempt.record_id),),
        ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    payload["request_summary"]["query_hash"] = "0" * 64
    with pytest.raises(ValidationError):
        type(attempt).model_validate(payload, strict=True)

    with pytest.raises(TypeError):
        view.candidate_bundle.candidates[0].checks["nested"] = "blocked"


def test_response_envelope_hash_and_nested_json_are_immutable(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    view = _resolve(service, started)
    attempt = view.attempts[0]
    assert attempt.response_artifact_id is not None
    with sqlite3.connect(service.database_path) as connection:
        artifact_row = connection.execute(
            "SELECT content_hash FROM artifacts WHERE artifact_id = ?",
            (str(attempt.response_artifact_id),),
        ).fetchone()
    assert artifact_row is not None
    from orca_agent.infrastructure.artifacts import ArtifactRecordRepository, ArtifactStore
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        record = ArtifactRecordRepository(uow.connection).get(attempt.response_artifact_id)
        assert record is not None
        envelope = ResponseEnvelope.model_validate_json(
            ArtifactStore(service.state_root).read(record).decode("utf-8"), strict=True
        )
        uow.commit()
    with pytest.raises(TypeError):
        envelope.request_metadata["tampered"] = True
    with pytest.raises(ValidationError):
        ResponseEnvelope.model_validate(
            {**envelope.model_dump(mode="json"), "body_sha256": "0" * 64}, strict=True
        )
    assert len(base64.b64decode(envelope.body_base64)) > 0


def test_corrupted_response_artifact_blocks_verified_reads_and_export(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    view = _resolve(service, started)
    attempt = view.attempts[0]
    assert attempt.response_artifact_id is not None
    from orca_agent.infrastructure.artifacts import ArtifactRecordRepository, ArtifactStore
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        record = ArtifactRecordRepository(uow.connection).get(attempt.response_artifact_id)
        assert record is not None
        artifact_path = ArtifactStore(service.state_root).path_for(record.relative_path)
        uow.commit()
    artifact_path.write_bytes(artifact_path.read_bytes() + b"tampered")
    with pytest.raises(StateIntegrityError):
        service.inspect(started.run_id)
    with pytest.raises(StateIntegrityError):
        service.export_plan(started.run_id)


def test_identical_provider_responses_remain_owner_bound_across_runs(tmp_path):
    service, clock = _service(tmp_path)
    _first_command, first = _start(service, clock, raw_input="water")
    first_view = _resolve(service, first)
    _second_command, second = _start(service, clock, raw_input="water")
    second_view = _resolve(service, second)
    assert (
        first_view.attempts[0].response_artifact_id != second_view.attempts[0].response_artifact_id
    )
    assert first_view.candidate_bundle.candidates[0].source_artifact_id == (
        first_view.attempts[0].response_artifact_id
    )
    assert second_view.candidate_bundle.candidates[0].source_artifact_id == (
        second_view.attempts[0].response_artifact_id
    )


def test_protocol_out_of_range_fails_without_confirmation_or_execution(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(
        service,
        clock,
        input_kind=MoleculeInputKind.SMILES,
        raw_input="[NH4+]",
        provider=IdentityProvider.LOCAL,
        charge=1,
        multiplicity=1,
    )
    reports = service.create_worker().run_once(limit=1)
    assert reports[0].outcome == "succeeded"
    view = service.inspect(started.run_id)
    assert view.state.phase.value == "failed"
    assert view.interrupt is None
    assert view.candidate_bundle is None
    assert view.attempts[0].error_code == "protocol_unsupported"


def test_restart_inspection_uses_verified_history_not_provider_cache(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    _resolve(service, started)

    class FailingAdapter:
        def resolve(self, _query):
            raise AssertionError("restart inspection must not resolve again")

    restarted = P4ApplicationService(
        tmp_path / "state",
        clock=clock,
        fake_adapter=FailingAdapter(),
    )
    view = restarted.inspect(started.run_id)
    assert view.state.phase.value == "awaiting_identity"
    assert view.candidate_bundle is not None


def test_confirmation_expiry_is_durable(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    view = _resolve(service, started)
    command = _confirmation_command(clock, view)
    clock.advance(timedelta(hours=24))
    expired = service.confirm(command)
    assert expired.accepted is False
    assert expired.code == "identity_confirmation_expired"
    assert service.inspect(started.run_id).state.phase.value == "failed"


def test_retry_after_is_persisted_and_blocks_early_claim(tmp_path):
    clock = FrozenClock(BASE_TIME)

    class RetryThenSuccess:
        def __init__(self):
            self.calls = 0
            self.fake = FakePubChemAdapter()

        def resolve(self, query):
            self.calls += 1
            if self.calls == 1:
                return LookupResult(
                    provider=IdentityProvider.FAKE,
                    adapter_version="retry-test-v1",
                    body=canonical_json_bytes({"status": 429}),
                    request_metadata={"test": "retry"},
                    error_code=IdentityErrorCode.PROVIDER_THROTTLED,
                    retryable=True,
                    status_code=429,
                    retry_after=RetryAfter(
                        raw="5",
                        status="valid",
                        not_before_utc=clock.now_utc() + timedelta(seconds=5),
                    ),
                )
            return self.fake.resolve(query)

    adapter = RetryThenSuccess()
    service, _clock = _service(tmp_path, adapter=adapter, clock=clock)
    _command, started = _start(service, clock)
    first = service.create_worker().run_once(limit=1)
    assert first[0].outcome == "retry"
    first_view = service.inspect(started.run_id)
    assert first_view.attempts[0].retry_not_before_utc == BASE_TIME + timedelta(seconds=5)

    clock.advance(timedelta(seconds=4))
    assert service.create_worker().run_once(limit=1) == ()
    assert service.inspect(started.run_id).attempts[0].generation == 1
    assert adapter.calls == 1

    clock.advance(timedelta(seconds=1))
    second = service.create_worker().run_once(limit=1)
    assert second[0].outcome == "succeeded"
    assert adapter.calls == 2


def test_http_adapter_network_disabled_and_url_encoding():
    query = MoleculeQuery.create(
        run_id=new_id(RunId),
        conversation_id=new_id(ConversationId),
        input_kind=MoleculeInputKind.NAME,
        raw_input="A/B ?",
        normalized_input="A/B ?",
        charge=0,
        multiplicity=1,
        provider=IdentityProvider.PUBCHEM,
        protocol_id="ground_state_baseline_r2scan3c_v1",
    )
    disabled = HttpPubChemAdapter()
    assert disabled.resolve(query).error_code is IdentityErrorCode.NETWORK_DISABLED

    seen: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, content=b"not found")

    live = HttpPubChemAdapter(allow_network=True, transport=httpx.MockTransport(transport))
    result = live.resolve(query)
    assert result.error_code is IdentityErrorCode.NOT_FOUND
    assert seen and "A%2FB%20%3F" in seen[0]


def test_cli_process_p4_request_replay_and_export(tmp_path, capsys):
    state_root = tmp_path / "state"
    prepare_file = tmp_path / "prepare.json"
    confirm_file = tmp_path / "confirm.json"
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "prepare",
                "--name",
                "water",
                "--charge",
                "0",
                "--multiplicity",
                "1",
                "--provider",
                "fake",
                "--new-conversation",
                "--save-request",
                str(prepare_file),
                "--json",
            ]
        )
        == 0
    )
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["phase"] == "resolving_identity"
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "worker",
                "--workflow",
                "p4",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--state-root", str(state_root), "inspect", "--run", prepared["run_id"]]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["state"]["phase"] == "awaiting_identity"
    candidate = inspected["candidate_bundle"]["candidates"][0]
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "confirm-identity",
                "--run",
                prepared["run_id"],
                "--conversation-id",
                prepared["conversation_id"],
                "--expected-revision",
                str(inspected["revision"]),
                "--interrupt-id",
                inspected["interrupt"]["interrupt_id"],
                "--query-hash",
                inspected["query"]["query_hash"],
                "--candidate-set-hash",
                inspected["candidate_bundle"]["candidate_set_hash"],
                "--candidate-id",
                candidate["candidate_id"],
                "--candidate-hash",
                candidate["candidate_hash"],
                "--decision",
                "accept",
                "--save-request",
                str(confirm_file),
                "--json",
            ]
        )
        == 0
    )
    confirmed = json.loads(capsys.readouterr().out)
    assert confirmed["phase"] == "plan_ready"
    output = tmp_path / "plan.md"
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "export-plan",
                "--run",
                prepared["run_id"],
                "--format",
                "md",
                "--output",
                str(output),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert "Execution ready: `false`" in output.read_text(encoding="utf-8")
    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "replay-request",
                "--file",
                str(confirm_file),
            ]
        )
        == 0
    )
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["revision"] == confirmed["revision"]


def test_registry_plan_validator_rejects_unknown_or_wrong_geometry(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    view = _resolve(service, started)
    _confirm_command, confirmed = _confirm(service, clock, view)
    final = service.inspect(started.run_id)
    assert final.prepared_plan is not None and final.confirmed_molecule is not None
    validate_ground_state_plan(
        final.prepared_plan,
        confirmed=final.confirmed_molecule,
        snapshot=final.registry,
    )
    bad = final.prepared_plan.model_copy(
        update={
            "geometry_bindings": {
                "optimization_input": {"kind": "absolute_path", "reference": "C:\\x"},
                "frequency_input": {
                    "kind": "primitive_output",
                    "primitive_id": str(final.prepared_plan.proposal.steps[0].primitive_id),
                    "output": PRIMITIVE_OPTIMIZATION.output_geometry,
                },
            }
        }
    )
    with pytest.raises(PlanValidationError):
        validate_ground_state_plan(bad, confirmed=final.confirmed_molecule, snapshot=final.registry)


def _pubchem_query(
    *,
    input_kind: MoleculeInputKind = MoleculeInputKind.NAME,
    raw_input: str = "water",
) -> MoleculeQuery:
    return MoleculeQuery.create(
        run_id=new_id(RunId),
        conversation_id=new_id(ConversationId),
        input_kind=input_kind,
        raw_input=raw_input,
        normalized_input=raw_input,
        charge=0,
        multiplicity=1,
        provider=IdentityProvider.PUBCHEM,
        protocol_id="ground_state_baseline_r2scan3c_v1",
    )


def _mock_http_adapter(
    *,
    status_code: int,
    content: bytes = b"not found",
    headers: dict[str, str] | None = None,
    error: Exception | None = None,
    **kwargs,
) -> HttpPubChemAdapter:
    def transport(request: httpx.Request) -> httpx.Response:
        if error is not None:
            raise error
        return httpx.Response(status_code, headers=headers, content=content, request=request)

    return HttpPubChemAdapter(
        allow_network=True,
        transport=httpx.MockTransport(transport),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("status_code", "code", "retryable"),
    (
        (429, IdentityErrorCode.PROVIDER_THROTTLED, True),
        (503, IdentityErrorCode.PROVIDER_THROTTLED, True),
        (502, IdentityErrorCode.PROVIDER_UNAVAILABLE, True),
        (504, IdentityErrorCode.PROVIDER_UNAVAILABLE, True),
        (400, IdentityErrorCode.QUERY_REJECTED, False),
        (404, IdentityErrorCode.NOT_FOUND, False),
        (500, IdentityErrorCode.PROVIDER_UNAVAILABLE, True),
    ),
)
def test_http_statuses_are_typed_and_retry_policy_is_explicit(status_code, code, retryable):
    result = _mock_http_adapter(
        status_code=status_code,
        headers={"Retry-After": "2"} if status_code in (429, 503) else None,
    ).resolve(_pubchem_query())
    assert result.error_code is code
    assert result.retryable is retryable
    assert result.status_code == status_code
    if status_code in (429, 503):
        assert result.retry_after.status == "valid"


def test_http_success_schema_limits_and_transport_failures_are_typed():
    success_body = json.dumps(
        {
            "PropertyTable": {
                "Properties": [
                    {
                        "CID": 962,
                        "SMILES": "O",
                        "MolecularFormula": "H2O",
                        "Charge": 0,
                    }
                ]
            }
        }
    ).encode()
    success = _mock_http_adapter(status_code=200, content=success_body).resolve(_pubchem_query())
    assert success.succeeded is True
    assert success.candidates[0].cid == 962

    malformed = _mock_http_adapter(status_code=200, content=b"<html>").resolve(_pubchem_query())
    assert malformed.error_code is IdentityErrorCode.PROVIDER_SCHEMA_ERROR
    empty = _mock_http_adapter(
        status_code=200,
        content=b'{"PropertyTable":{"Properties":[]}}',
    ).resolve(_pubchem_query())
    assert empty.error_code is IdentityErrorCode.NOT_FOUND
    too_many = json.dumps(
        {
            "PropertyTable": {
                "Properties": [
                    {"CID": index + 1, "SMILES": "O", "Charge": 0} for index in range(11)
                ]
            }
        }
    ).encode()
    limited = _mock_http_adapter(status_code=200, content=too_many).resolve(_pubchem_query())
    assert limited.error_code is IdentityErrorCode.RESULT_LIMIT_EXCEEDED

    cid_query = _pubchem_query(input_kind=MoleculeInputKind.CID, raw_input="962")
    cid_mismatch_body = success_body.replace(b'"CID": 962', b'"CID": 963')
    cid_mismatch = _mock_http_adapter(status_code=200, content=cid_mismatch_body).resolve(cid_query)
    assert cid_mismatch.error_code is IdentityErrorCode.PROVIDER_SCHEMA_ERROR

    oversized = _mock_http_adapter(
        status_code=200,
        content=b"x" * (HttpPubChemAdapter.max_response_bytes + 1),
    ).resolve(_pubchem_query())
    assert oversized.error_code is IdentityErrorCode.RESULT_LIMIT_EXCEEDED

    request = httpx.Request("GET", "https://pubchem.ncbi.nlm.nih.gov")
    timeout = _mock_http_adapter(
        status_code=200,
        error=httpx.ReadTimeout("timeout", request=request),
    ).resolve(_pubchem_query())
    assert timeout.error_code is IdentityErrorCode.LOOKUP_TIMEOUT
    network = _mock_http_adapter(
        status_code=200,
        error=httpx.ConnectError("unavailable", request=request),
    ).resolve(_pubchem_query())
    assert network.error_code is IdentityErrorCode.PROVIDER_UNAVAILABLE


def test_http_retry_after_parser_and_rate_limit_are_deterministic():
    from orca_agent.identity.http_pubchem import _parse_retry_after

    now = BASE_TIME
    assert _parse_retry_after(None, now).status == "missing"
    assert _parse_retry_after("", now).status == "invalid"
    assert _parse_retry_after("-1", now).status == "invalid"
    assert _parse_retry_after("3", now).not_before_utc == now + timedelta(seconds=3)
    assert _parse_retry_after("Sun, 06 Sep 2026 00:00:05 GMT", now).status == "valid"
    assert _parse_retry_after("not-a-date", now).status == "invalid"

    ticks = iter((0.0, 0.2, 1.2))
    sleeps: list[float] = []
    adapter = _mock_http_adapter(
        status_code=404,
        monotonic=lambda: next(ticks),
        sleeper=sleeps.append,
    )
    adapter.resolve(_pubchem_query())
    adapter.resolve(_pubchem_query(raw_input="ethanol"))
    assert sleeps == [pytest.approx(0.8)]


@pytest.mark.parametrize(
    ("smiles", "charge", "multiplicity", "code"),
    (
        ("", 0, 1, "invalid_identity"),
        ("C" * 4097, 0, 1, "identity_result_limit_exceeded"),
        ("not a smiles", 0, 1, "invalid_identity"),
        ("[*]", 0, 1, "invalid_identity"),
        ("[O-]", 0, 1, "invalid_identity"),
        ("C", 0, 2, "invalid_identity"),
    ),
)
def test_rdkit_normalizer_rejects_unsafe_identity_inputs(smiles, charge, multiplicity, code):
    with pytest.raises(ValueError) as error:
        RDKitNormalizer().normalize(smiles, charge=charge, multiplicity=multiplicity)
    assert getattr(error.value, "code", None) == code


def test_rdkit_normalizer_reports_protocol_range_without_rewriting_structure():
    normalizer = RDKitNormalizer()
    sodium = normalizer.normalize("[Na+]", charge=1, multiplicity=1)
    assert sodium.checks["unsupported_elements"] == ["Na"]
    assert sodium.checks["protocol_range_supported"] is False
    mixture = normalizer.normalize("CC.O", charge=0, multiplicity=1)
    assert mixture.fragment_count == 2
    assert mixture.checks["protocol_range_supported"] is False


@pytest.mark.parametrize(
    "kind,value,expected",
    (
        (MoleculeInputKind.NAME, "  é  ", "é"),
        (MoleculeInputKind.SMILES, " O ", "O"),
        (MoleculeInputKind.CID, "000962", "962"),
        (MoleculeInputKind.CAS, "7732-18-5", "7732-18-5"),
    ),
)
def test_input_normalization_is_kind_specific(kind, value, expected):
    assert normalize_input(kind, value) == expected


@pytest.mark.parametrize(
    "kind,value",
    (
        (MoleculeInputKind.NAME, ""),
        (MoleculeInputKind.CID, "0"),
        (MoleculeInputKind.CID, "nope"),
        (MoleculeInputKind.CAS, "1-2-3"),
        (MoleculeInputKind.SMILES, "\x00"),
    ),
)
def test_input_normalization_rejects_invalid_boundaries(kind, value):
    with pytest.raises(ValueError):
        normalize_input(kind, value)


def test_fake_adapter_reports_missing_and_mismatched_fixture_records():
    unknown = _pubchem_query()
    assert FakePubChemAdapter(records={}).resolve(unknown).error_code is IdentityErrorCode.NOT_FOUND
    mismatched = FakePubChemAdapter(
        records={"cid:962": (RawIdentityCandidate(candidate_id="wrong", cid=963, smiles="O"),)}
    ).resolve(_pubchem_query(input_kind=MoleculeInputKind.CID, raw_input="962"))
    assert mismatched.error_code is IdentityErrorCode.PROVIDER_SCHEMA_ERROR


class _StaticIdentityAdapter:
    adapter_version = "static-p4-test-v1"

    def __init__(self, result: LookupResult):
        self.result = result

    def resolve(self, _query):
        return self.result


def test_handler_persists_typed_provider_failures_without_confirmation(tmp_path):
    cases = (
        LookupResult(
            provider=IdentityProvider.FAKE,
            adapter_version="empty-v1",
            body=b"",
            request_metadata={},
        ),
        LookupResult(
            provider=IdentityProvider.LOCAL,
            adapter_version="wrong-provider-v1",
            body=b"{}",
            request_metadata={},
        ),
        LookupResult(
            provider=IdentityProvider.FAKE,
            adapter_version="duplicate-v1",
            body=b"{}",
            request_metadata={},
            candidates=(
                RawIdentityCandidate(candidate_id="same", smiles="O"),
                RawIdentityCandidate(candidate_id="same", smiles="O"),
            ),
        ),
        LookupResult(
            provider=IdentityProvider.FAKE,
            adapter_version="formula-v1",
            body=b"{}",
            request_metadata={},
            candidates=(
                RawIdentityCandidate(candidate_id="bad", smiles="O", molecular_formula="CO"),
            ),
        ),
    )
    for index, result in enumerate(cases):
        service, clock = _service(
            tmp_path / f"case-{index}",
            adapter=_StaticIdentityAdapter(result),
        )
        _command, started = _start(service, clock, raw_input="water")
        reports = service.create_worker().run_once(limit=1)
        assert reports[0].outcome == "succeeded"
        view = service.inspect(started.run_id)
        assert view.state.phase.value == "failed"
        assert view.interrupt is None
        assert view.candidate_bundle is None
        assert view.attempts[0].status is LookupStatus.FAILED


def test_worker_handler_exception_is_fail_closed(tmp_path):
    class ExplodingAdapter:
        adapter_version = "exploding-v1"

        def resolve(self, _query):
            raise RuntimeError("synthetic adapter failure")

    service, clock = _service(tmp_path, adapter=ExplodingAdapter())
    service.max_attempts = 1
    _command, started = _start(service, clock)
    reports = service.create_worker().run_once(limit=1)
    assert reports[0].outcome == "dead_letter"
    assert service.inspect(started.run_id).state.phase.value == "failed"


def test_completion_crash_after_terminal_write_rolls_back_and_replays_attempt(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    handler = P4IdentityHandler(
        service.database_path,
        service.state_root,
        clock=clock,
        fake_adapter=service.fake_adapter,
        pubchem_adapter=service.pubchem_adapter,
    )

    def metadata_factory(**kwargs):
        return service._completion_metadata(**kwargs)

    def crash_hook(**_kwargs):
        raise RuntimeError("synthetic post-write completion crash")

    def completion_factory():
        return EffectCompletionService(
            service.database_path,
            clock=clock,
            registry=P4_EFFECT_REGISTRY,
            max_attempts=3,
            completion_metadata_factory=metadata_factory,
            completion_hook=crash_hook,
        )

    crashing_worker = OutboxWorker(
        service.database_path,
        handler,
        clock=clock,
        lease_duration=timedelta(seconds=1),
        max_attempts=3,
        registry=P4_EFFECT_REGISTRY,
        completion_service_factory=completion_factory,
    )
    with pytest.raises(RuntimeError, match="post-write completion crash"):
        crashing_worker.run_once(limit=1)
    after_crash = service.inspect(started.run_id)
    assert after_crash.state.phase.value == "resolving_identity"
    assert len(after_crash.attempts) == 1
    assert after_crash.outbox[0]["status"] == "dispatching"

    clock.advance(timedelta(seconds=2))
    recovered = service.create_worker(lease_duration=timedelta(seconds=1)).run_once(limit=1)
    assert recovered[0].outcome == "succeeded"
    assert service.inspect(started.run_id).state.phase.value == "awaiting_identity"


def test_p4_confirmation_reject_and_command_replay_are_idempotent(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    view = _resolve(service, started)
    command = _confirmation_command(clock, view)
    reject = command.model_copy(update={"decision": IdentityDecision.REJECT})
    first = service.confirm(reject)
    second = service.confirm(reject)
    assert first.accepted is False and first.code == "identity_rejected"
    assert second == first
    assert service.inspect(started.run_id).state.phase.value == "failed"


def test_p4_confirmation_rejects_each_cross_boundary_binding(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    view = _resolve(service, started)
    command = _confirmation_command(clock, view)
    mutations = (
        {"run_id": new_id(RunId)},
        {"conversation_id": new_id(ConversationId)},
        {"expected_revision": view.revision - 1},
        {"interrupt_id": new_id(InterruptId)},
        {"query_id": new_id(WorkflowRecordId)},
        {"query_hash": "0" * 64},
        {"candidate_bundle_id": new_id(WorkflowRecordId)},
        {"candidate_bundle_hash": "0" * 64},
        {"candidate_set_hash": "0" * 64},
        {"candidate_id": "missing-candidate"},
        {"candidate_hash": "0" * 64},
    )
    for mutation in mutations:
        rejected = service.confirm(command.model_copy(update=mutation))
        assert rejected.accepted is False
    assert service.inspect(started.run_id).state.phase.value == "awaiting_identity"


def test_p4_cancel_waiting_run_and_reject_duplicate_command_payload(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    cancel = CancelPlanningRun.create(
        run_id=started.run_id,
        conversation_id=started.conversation_id,
        expected_revision=started.revision,
        requested_at_utc=clock.now_utc(),
    )
    first = service.cancel(cancel)
    second = service.cancel(cancel)
    assert first.accepted is True and first.code == "run_cancelled"
    assert second == first
    assert service.inspect(started.run_id).state.phase.value == "cancelled"

    conflicting = CancelPlanningRun.create(
        run_id=started.run_id,
        conversation_id=started.conversation_id,
        expected_revision=started.revision,
        reason_code="workflow_failed",
        command_id=cancel.command_id,
        requested_at_utc=clock.now_utc(),
    )
    rejected = service.cancel(conflicting)
    assert rejected.accepted is False
    assert rejected.code == "duplicate_command_conflict"


def test_p4_start_rejects_protocol_provider_and_conversation_invariants(tmp_path):
    service, clock = _service(tmp_path)
    invalid_protocol = StartPlanningRun.create(
        input_kind=MoleculeInputKind.NAME,
        raw_input="water",
        charge=0,
        multiplicity=1,
        provider=IdentityProvider.FAKE,
        protocol_id="unknown",
        requested_at_utc=clock.now_utc(),
    )
    assert service.start(invalid_protocol).code == "invalid_transition"
    wrong_provider = StartPlanningRun.create(
        input_kind=MoleculeInputKind.SMILES,
        raw_input="O",
        charge=0,
        multiplicity=1,
        provider=IdentityProvider.FAKE,
        requested_at_utc=clock.now_utc(),
    )
    assert service.start(wrong_provider).code == "invalid_transition"
    old_conversation = StartPlanningRun.create(
        input_kind=MoleculeInputKind.NAME,
        raw_input="water",
        charge=0,
        multiplicity=1,
        provider=IdentityProvider.FAKE,
        new_conversation=False,
        requested_at_utc=clock.now_utc(),
    )
    assert service.start(old_conversation).code == "invalid_transition"
    bad_id = StartPlanningRun.create(
        input_kind=MoleculeInputKind.NAME,
        raw_input="water",
        charge=0,
        multiplicity=1,
        provider=IdentityProvider.FAKE,
        command_id=CommandId("command_" + "0" * 32),
        requested_at_utc=clock.now_utc(),
    )
    assert service.start(bad_id).code == "invalid_transition"


def test_p4_replay_and_snapshot_verifier_reject_corrupt_metadata(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        snapshot = uow.runs.require(started.run_id)
        stored = EventRepository(uow.connection).list_for_run(started.run_id)
        uow.commit()
    events = tuple(item.event for item in stored)
    assert replay_p4(events) == snapshot.state
    assert (
        verify_p4_snapshot(
            snapshot=snapshot.state,
            stored_state_hash=snapshot.state_hash,
            stored_revision=snapshot.revision,
            stored_last_event_id=snapshot.last_event_id,
            events=events,
        )
        == snapshot.state
    )
    with pytest.raises(StateIntegrityError):
        replay_p4(())
    with pytest.raises(StateIntegrityError):
        replay_p4((object(),))
    with pytest.raises(StateIntegrityError):
        replay_p4((events[0].model_copy(update={"engine_version": "other"}),))
    with pytest.raises(StateIntegrityError):
        replay_p4((events[0].model_copy(update={"sequence_no": 2}),))
    with pytest.raises(StateIntegrityError):
        replay_p4((events[0].model_copy(update={"expected_revision": 1}),))
    with pytest.raises(StateIntegrityError):
        replay_p4((events[0].model_copy(update={"run_id": new_id(RunId)}),))
    with pytest.raises(StateIntegrityError):
        replay_p4((events[0].model_copy(update={"previous_event_hash": "f" * 64}),))
    with pytest.raises(StateIntegrityError):
        verify_p4_snapshot(
            snapshot=snapshot.state,
            stored_state_hash="0" * 64,
            stored_revision=snapshot.revision,
            stored_last_event_id=snapshot.last_event_id,
            events=events,
        )
    with pytest.raises(StateIntegrityError):
        verify_p4_snapshot(
            snapshot=snapshot.state,
            stored_state_hash=snapshot.state_hash,
            stored_revision=snapshot.revision + 1,
            stored_last_event_id=snapshot.last_event_id,
            events=events,
        )
    with pytest.raises(StateIntegrityError):
        verify_p4_snapshot(
            snapshot=snapshot.state,
            stored_state_hash=snapshot.state_hash,
            stored_revision=snapshot.revision,
            stored_last_event_id=new_id(EventId),
            events=events,
        )


def test_registry_validator_rejects_each_untrusted_registry_or_plan_binding(tmp_path):
    service, clock = _service(tmp_path)
    _command, started = _start(service, clock)
    view = _resolve(service, started)
    _confirm_command, _confirmed_result = _confirm(service, clock, view)
    final = service.inspect(started.run_id)
    assert final.prepared_plan is not None and final.confirmed_molecule is not None

    for snapshot_update in (
        {"registry_version": "other"},
        {"manifest": tuple(reversed(final.registry.manifest))},
        {"protocols": {}},
        {"methods": {}},
        {"capabilities": {}},
        {"primitives": {}},
    ):
        with pytest.raises(PlanValidationError):
            validate_ground_state_plan(
                final.prepared_plan,
                confirmed=final.confirmed_molecule,
                snapshot=final.registry.model_copy(update=snapshot_update),
            )

    plan = final.prepared_plan
    confirmed = final.confirmed_molecule
    bad_plans = (
        plan.model_copy(update={"run_id": new_id(RunId)}),
        plan.model_copy(update={"confirmed_molecule_id": new_id(type(plan.confirmed_molecule_id))}),
        plan.model_copy(update={"registry_snapshot_id": new_id(type(plan.registry_snapshot_id))}),
        plan.model_copy(
            update={"problem_spec": plan.problem_spec.model_copy(update={"molecule_ref": "wrong"})}
        ),
        plan.model_copy(
            update={
                "problem_spec": plan.problem_spec.model_copy(
                    update={"charge": plan.problem_spec.charge + 1}
                )
            }
        ),
        plan.model_copy(
            update={
                "proposal": plan.proposal.model_copy(
                    update={"steps": tuple(reversed(plan.proposal.steps))}
                )
            }
        ),
        plan.model_copy(
            update={
                "proposal": plan.proposal.model_copy(
                    update={
                        "steps": (
                            plan.proposal.steps[0].model_copy(
                                update={"method_profile_id": "other"}
                            ),
                            plan.proposal.steps[1],
                        )
                    }
                )
            }
        ),
        plan.model_copy(
            update={
                "proposal": plan.proposal.model_copy(
                    update={
                        "steps": (
                            plan.proposal.steps[0],
                            plan.proposal.steps[1].model_copy(update={"depends_on": ()}),
                        )
                    }
                )
            }
        ),
        plan.model_copy(
            update={
                "proposal": plan.proposal.model_copy(
                    update={
                        "steps": (
                            plan.proposal.steps[0].model_copy(update={"parameters": {}}),
                            plan.proposal.steps[1],
                        )
                    }
                )
            }
        ),
        plan.model_copy(update={"capability_id": "other"}),
        plan.model_copy(update={"planning_valid": False}),
        plan.model_copy(update={"execution_ready": True}),
    )
    for bad in bad_plans:
        with pytest.raises(PlanValidationError):
            validate_ground_state_plan(bad, confirmed=confirmed, snapshot=final.registry)


__all__ = []
