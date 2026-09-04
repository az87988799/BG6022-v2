import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import ArtifactId, EvidenceId, new_id
from orca_agent.domain.models import (
    BackendKind,
    Budget,
    ClaimStatus,
    ClaimType,
    Environment,
    EvidenceRecord,
    EvidenceType,
    ExecutionEnvelope,
    PlanProposal,
    PrimitiveKind,
    PrimitiveSpec,
    ProblemSpec,
    Provenance,
    ValidatedAction,
    ValidatedClaim,
)


def _records() -> list[tuple[type[object], object, str]]:
    problem = ProblemSpec.create(
        goal="Compute a gas-phase energy",
        molecule_ref="water",
        charge=0,
        multiplicity=1,
        environment=Environment.GAS,
        target_properties=("energy",),
        constraints={"settings": {"temperature": 298.15}},
    )
    primitive = PrimitiveSpec.create(
        kind=PrimitiveKind.SP,
        molecule_ref="water",
        method_profile_id="baseline.r2scan3c.v1",
        parameters={"settings": {"charge": 0}},
    )
    proposal = PlanProposal.create(
        problem_spec_id=problem.record_id,
        problem_spec_hash=sha256_hex(problem),
        steps=(primitive,),
        rationale="Use the registered baseline primitive.",
        planner_id="deterministic:p1-fixture",
    )
    action = ValidatedAction.create(
        proposal_hash=sha256_hex(proposal),
        primitive=primitive,
        execution_envelope=ExecutionEnvelope(
            backend_kind=BackendKind.FAKE,
            artifact_namespace_id=new_id(ArtifactId),
        ),
        budget=Budget(wall_time_seconds=60, memory_mb=512, cores=1),
    )
    evidence = EvidenceRecord.create(
        action_id=action.action_id,
        evidence_type=EvidenceType.PARSED_ENERGY,
        payload={"result": {"energy_hartree": -75.0}, "labels": ["water", "gas"]},
        provenance=Provenance(
            producer="p1-fixture",
            producer_version="1",
            created_at=datetime(2026, 9, 4, tzinfo=UTC),
        ),
    )
    claim = ValidatedClaim.create(
        claim_type=ClaimType.ENERGY,
        value={"result": {"energy_hartree": -75.0}},
        unit="hartree",
        evidence_ids=(evidence.evidence_id,),
        status=ClaimStatus.VALIDATED,
    )
    return [
        (ProblemSpec, problem, "record_id"),
        (PrimitiveSpec, primitive, "primitive_id"),
        (PlanProposal, proposal, "proposal_id"),
        (ValidatedAction, action, "action_id"),
        (EvidenceRecord, evidence, "evidence_id"),
        (ValidatedClaim, claim, "claim_id"),
    ]


@pytest.mark.parametrize("model_type,model,missing_field", _records())
def test_missing_record_id_fails_on_json_load(
    model_type: type[object], model: object, missing_field: str
) -> None:
    payload = json.loads(model.model_dump_json())
    payload.pop(missing_field)

    with pytest.raises(ValidationError):
        model_type.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("model_type,model,_missing_field", _records())
def test_missing_schema_version_fails_on_json_load(
    model_type: type[object], model: object, _missing_field: str
) -> None:
    payload = json.loads(model.model_dump_json())
    payload.pop("schema_version")

    with pytest.raises(ValidationError):
        model_type.model_validate_json(json.dumps(payload))


def test_tampered_hashes_fail_during_json_load() -> None:
    records = _records()
    action = records[3][1]
    action_payload = json.loads(action.model_dump_json())
    action_payload["budget"]["cores"] = 2
    with pytest.raises(ValidationError):
        ValidatedAction.model_validate_json(json.dumps(action_payload))

    evidence = records[4][1]
    evidence_payload = json.loads(evidence.model_dump_json())
    evidence_payload["payload"]["result"]["energy_hartree"] = 0.0
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate_json(json.dumps(evidence_payload))

    claim = records[5][1]
    claim_payload = json.loads(claim.model_dump_json())
    claim_payload["unit"] = "kcal/mol"
    with pytest.raises(ValidationError):
        ValidatedClaim.model_validate_json(json.dumps(claim_payload))

    claim_payload = json.loads(claim.model_dump_json())
    claim_payload["evidence_ids"] = [str(new_id(EvidenceId))]
    with pytest.raises(ValidationError):
        ValidatedClaim.model_validate_json(json.dumps(claim_payload))


def test_nested_json_is_defensively_frozen_after_construction() -> None:
    problem = _records()[0][1]
    primitive = _records()[1][1]
    evidence = _records()[4][1]
    claim = _records()[5][1]

    with pytest.raises(TypeError):
        problem.constraints["settings"]["temperature"] = 0
    with pytest.raises(TypeError):
        primitive.parameters["settings"]["charge"] = 1
    with pytest.raises(TypeError):
        evidence.payload["result"]["energy_hartree"] = 0
    with pytest.raises(TypeError):
        claim.value["result"]["energy_hartree"] = 0


def test_nested_json_arrays_are_defensively_frozen_and_hashable() -> None:
    source = {"values": [{"charge": 0}, {"charge": 1}]}
    primitive = PrimitiveSpec.create(
        kind=PrimitiveKind.SP,
        molecule_ref="water",
        method_profile_id="baseline.r2scan3c.v1",
        parameters=source,
    )
    source["values"][0]["charge"] = 99

    assert primitive.parameters["values"][0]["charge"] == 0
    with pytest.raises(TypeError):
        primitive.parameters["values"][0]["charge"] = 2
    with pytest.raises(AttributeError):
        primitive.parameters["values"].append({"charge": 2})

    evidence = _records()[4][1]
    evidence.verify_payload_hash()
