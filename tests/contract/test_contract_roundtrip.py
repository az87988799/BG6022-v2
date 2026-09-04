from datetime import UTC, datetime

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import ArtifactId, new_id
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


def _primitive() -> PrimitiveSpec:
    return PrimitiveSpec(
        kind=PrimitiveKind.SP,
        molecule_ref="water",
        method_profile_id="baseline.r2scan3c.v1",
        parameters={"charge": 0, "multiplicity": 1},
    )


def test_six_contracts_construct_and_round_trip_through_json() -> None:
    problem = ProblemSpec(
        goal="Compute a gas-phase energy",
        molecule_ref="water",
        charge=0,
        multiplicity=1,
        environment=Environment.GAS,
        target_properties=("electronic_energy", "electronic_energy"),
    )
    primitive = _primitive()
    proposal = PlanProposal(
        problem_spec_id=problem.record_id,
        problem_spec_hash=sha256_hex(problem),
        steps=(primitive,),
        rationale="Use the registered baseline primitive.",
        planner_id="deterministic:p1-fixture",
    )
    envelope = ExecutionEnvelope(
        backend_kind=BackendKind.FAKE,
        artifact_namespace_id=new_id(ArtifactId),
    )
    budget = Budget(wall_time_seconds=60, memory_mb=512, cores=1)
    action = ValidatedAction.create(
        proposal_hash=sha256_hex(proposal),
        primitive=primitive,
        execution_envelope=envelope,
        budget=budget,
    )
    evidence = EvidenceRecord.create(
        action_id=action.action_id,
        evidence_type=EvidenceType.PARSED_ENERGY,
        payload={"energy_hartree": -75.0},
        provenance=Provenance(
            producer="p1-fixture",
            producer_version="1",
            created_at=datetime(2026, 9, 4, tzinfo=UTC),
        ),
    )
    claim = ValidatedClaim.create(
        claim_type=ClaimType.ENERGY,
        value=-75.0,
        unit="hartree",
        evidence_ids=(evidence.evidence_id,),
        status=ClaimStatus.VALIDATED,
    )

    for model in (problem, primitive, proposal, action, evidence, claim):
        restored = type(model).model_validate_json(model.model_dump_json())
        assert restored == model

    action.verify_action_hash()
    evidence.verify_payload_hash()
    claim.verify_claim_hash()
