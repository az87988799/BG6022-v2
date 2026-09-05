import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from orca_agent.domain.errors import ContractInvariantError
from orca_agent.domain.ids import (
    ApprovalGrantId,
    ArtifactId,
    ConversationId,
    EvidenceId,
    ExecutionId,
    InterruptId,
    PrimitiveId,
    RunId,
)
from orca_agent.domain.models import (
    BackendKind,
    Budget,
    ClaimStatus,
    ClaimType,
    EvidenceRecord,
    EvidenceType,
    ExecutionEnvelope,
    PrimitiveKind,
    PrimitiveSpec,
    Provenance,
    ValidatedAction,
    ValidatedClaim,
)
from orca_agent.domain.p3 import ApprovalGrantV1, ExecutionIntent, ParsedFakeObservation
from orca_agent.execution.validator import require_valid_water_action, validate_p3_action
from orca_agent.planning.water import build_water_plan


def test_p3_persisted_contracts_require_ids_and_schema_version() -> None:
    plan = build_water_plan()
    action = plan.action.model_dump(mode="json")
    action.pop("action_id")
    with pytest.raises(ValidationError):
        type(plan.action).model_validate_json(json.dumps(action), strict=True)

    raw = plan.action.model_dump(mode="json")
    raw.pop("schema_version")
    with pytest.raises(ValidationError):
        type(plan.action).model_validate_json(json.dumps(raw), strict=True)

    intent = ExecutionIntent.create(
        run_id=RunId("run_00000000000000000000000000000000"),
        action_id=plan.action.action_id,
        approval_grant_id=ApprovalGrantId("approval_00000000000000000000000000000000"),
        idempotency_key="p3.fake.example",
    )
    intent_raw = intent.model_dump(mode="json")
    intent_raw.pop("execution_id")
    with pytest.raises(ValidationError):
        ExecutionIntent.model_validate_json(json.dumps(intent_raw), strict=True)


def test_p3_hashes_are_verified_during_json_loading() -> None:
    plan = build_water_plan()
    raw = plan.action.model_dump(mode="json")
    raw["budget"]["cores"] = 2
    with pytest.raises(ValidationError):
        type(plan.action).model_validate_json(json.dumps(raw), strict=True)

    now = datetime(2026, 9, 5, tzinfo=UTC)
    approval = ApprovalGrantV1.create(
        run_id=RunId("run_00000000000000000000000000000000"),
        conversation_id=ConversationId("conversation_00000000000000000000000000000000"),
        interrupt_id=InterruptId("interrupt_00000000000000000000000000000000"),
        action=plan.action,
        source_revision=2,
        approved_at_utc=now,
        expires_at_utc=now + timedelta(hours=1),
    )
    approval_raw = approval.model_dump(mode="json")
    approval_raw["budget_hash"] = "0" * 64
    with pytest.raises(ValidationError):
        ApprovalGrantV1.model_validate_json(json.dumps(approval_raw), strict=True)


def test_nested_json_values_are_immutable_after_construction() -> None:
    plan = build_water_plan()
    with pytest.raises(TypeError):
        plan.action.primitive.parameters["fixture_id"] = "changed"

    evidence = EvidenceRecord.create(
        action_id=plan.action.action_id,
        evidence_type=EvidenceType.EXECUTION_SUMMARY,
        payload={"nested": {"energy": -75.0}},
        artifact_refs=(),
        provenance=Provenance(
            producer="test",
            producer_version="1",
            created_at=datetime(2026, 9, 5, tzinfo=UTC),
        ),
    )
    with pytest.raises(TypeError):
        evidence.payload["nested"]["energy"] = 0

    claim = ValidatedClaim.create(
        claim_type=ClaimType.ENERGY,
        value={"nested": {"energy": -75.0}},
        unit="Hartree",
        evidence_ids=(EvidenceId("evidence_00000000000000000000000000000000"),),
        status=ClaimStatus.VALIDATED,
    )
    with pytest.raises(TypeError):
        claim.value["nested"]["energy"] = 0

    observation = ParsedFakeObservation(
        schema_version=2,
        engine_version="p3-water-v1",
        action_id=plan.action.action_id,
        execution_id=ExecutionId("execution_00000000000000000000000000000000"),
        fixture_id="water_sp_v1",
        fixture_version="1",
        fixture_hash="0" * 64,
        energy=-75.0,
        unit="Hartree",
        source="fake_fixture",
    )
    assert observation.energy == -75.0


def test_p3_water_validator_enforces_the_fixed_allowlist_and_adapter(tmp_path) -> None:
    plan = build_water_plan(
        artifact_namespace_id=ArtifactId("artifact_00000000000000000000000000000000")
    )
    primitive = plan.action.primitive

    def action_for(**changes) -> ValidatedAction:
        candidate = PrimitiveSpec.create(
            primitive_id=primitive.primitive_id,
            kind=changes.get("kind", primitive.kind),
            molecule_ref=changes.get("molecule_ref", primitive.molecule_ref),
            method_profile_id=changes.get("method_profile_id", primitive.method_profile_id),
            depends_on=changes.get("depends_on", primitive.depends_on),
            parameters=changes.get("parameters", primitive.parameters),
        )
        return ValidatedAction.create(
            proposal_hash=plan.action.proposal_hash,
            primitive=candidate,
            execution_envelope=changes.get("execution_envelope", plan.action.execution_envelope),
            budget=changes.get("budget", plan.action.budget),
        )

    assert validate_p3_action(plan.action, proposal=plan.proposal).code == "water_action_valid"
    require_valid_water_action(plan.action, proposal=plan.proposal)

    invalid_cases = (
        (
            "proposal_hash_mismatch",
            {"proposal": plan.proposal.model_copy(update={"rationale": "changed"})},
        ),
        ("only_single_point_is_supported", {"kind": PrimitiveKind.OPT}),
        (
            "dependencies_are_not_allowed",
            {"depends_on": (PrimitiveId("primitive_11111111111111111111111111111111"),)},
        ),
        ("fixture_molecule_mismatch", {"molecule_ref": "molecule.other"}),
        ("method_profile_not_allowed", {"method_profile_id": "fake.water.opt.v1"}),
        (
            "fixture_id_mismatch",
            {"parameters": {"fixture_id": "other", "fixture_version": "1"}},
        ),
        (
            "fixture_version_mismatch",
            {"parameters": {"fixture_id": "water_sp_v1", "fixture_version": "2"}},
        ),
        (
            "backend_not_allowed",
            {
                "execution_envelope": ExecutionEnvelope(
                    backend_kind=BackendKind.LOCAL,
                    artifact_namespace_id=plan.action.execution_envelope.artifact_namespace_id,
                )
            },
        ),
        ("budget_not_allowed", {"budget": Budget(wall_time_seconds=61, memory_mb=256, cores=1)}),
    )
    for expected_code, changes in invalid_cases:
        proposal = changes.pop("proposal", plan.proposal)
        candidate = action_for(**changes)
        result = validate_p3_action(candidate, proposal=proposal)
        assert not result.valid
        assert result.code == expected_code
        with pytest.raises(ContractInvariantError):
            require_valid_water_action(candidate, proposal=proposal)

    invalid_hash = plan.action.model_copy(update={"action_hash": "0" * 64})
    result = validate_p3_action(invalid_hash, proposal=plan.proposal)
    assert not result.valid
    assert result.code == "action_contract_invalid"
