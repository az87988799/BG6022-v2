"""Fixed, offline Water single-point fake planner for P3."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from orca_agent.domain.errors import ContractInvariantError
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    ArtifactId,
    PlanProposalId,
    PrimitiveId,
    ProblemSpecId,
    new_id,
)
from orca_agent.domain.models import (
    BackendKind,
    Budget,
    Environment,
    ExecutionEnvelope,
    PlanProposal,
    PrimitiveKind,
    PrimitiveSpec,
    ProblemSpec,
    ValidatedAction,
)
from orca_agent.orchestration.p3_versions import (
    P3_ENGINE_VERSION,
    P3_FIXTURE_ID,
    P3_FIXTURE_VERSION,
    P3_SCHEMA_VERSION,
)

FIXTURE_RESOURCE = "fixtures/water_sp_v1.json"
METHOD_PROFILE_ID = "fake.water.sp.v1"
WATER_GOAL = "Compute the fixed offline Water single-point fixture energy."
WATER_BUDGET = Budget(wall_time_seconds=60, memory_mb=256, cores=1)


@dataclass(frozen=True)
class WaterFixture:
    fixture_id: str
    fixture_version: str
    molecule_ref: str
    charge: int
    multiplicity: int
    environment: str
    method_profile_id: str
    energy: float
    unit: str
    source: str
    fixture_hash: str


def _read_fixture_payload() -> dict[str, Any]:
    raw = files("orca_agent").joinpath(FIXTURE_RESOURCE).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ContractInvariantError("Water fixture resource is not a JSON object")
    return value


_FIXTURE_PAYLOAD = _read_fixture_payload()
WATER_FIXTURE_HASH = sha256_hex(_FIXTURE_PAYLOAD)


def load_water_fixture() -> WaterFixture:
    """Load the packaged fixture and verify its fixed identity."""

    value = dict(_FIXTURE_PAYLOAD)
    if (
        value.get("fixture_id") != P3_FIXTURE_ID
        or value.get("fixture_version") != P3_FIXTURE_VERSION
        or value.get("method_profile_id") != METHOD_PROFILE_ID
        or value.get("source") != "fake_fixture"
    ):
        raise ContractInvariantError("packaged Water fixture identity is invalid")
    try:
        return WaterFixture(
            fixture_id=str(value["fixture_id"]),
            fixture_version=str(value["fixture_version"]),
            molecule_ref=str(value["molecule_ref"]),
            charge=int(value["charge"]),
            multiplicity=int(value["multiplicity"]),
            environment=str(value["environment"]),
            method_profile_id=str(value["method_profile_id"]),
            energy=float(value["energy"]),
            unit=str(value["unit"]),
            source=str(value["source"]),
            fixture_hash=WATER_FIXTURE_HASH,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ContractInvariantError("packaged Water fixture is invalid") from error


@dataclass(frozen=True)
class WaterPlanBundle:
    fixture: WaterFixture
    problem: ProblemSpec
    proposal: PlanProposal
    action: ValidatedAction


def build_water_plan(
    *,
    artifact_namespace_id: ArtifactId | None = None,
    problem_spec_id: ProblemSpecId | None = None,
    proposal_id: PlanProposalId | None = None,
    primitive_id: PrimitiveId | None = None,
    action_id: ActionId | None = None,
) -> WaterPlanBundle:
    """Build exactly one deterministic SP action without touching external systems."""

    fixture = load_water_fixture()
    problem = ProblemSpec.create(
        record_id=problem_spec_id,
        goal=WATER_GOAL,
        molecule_ref=fixture.molecule_ref,
        charge=fixture.charge,
        multiplicity=fixture.multiplicity,
        environment=Environment.GAS,
        target_properties=("energy",),
        constraints={
            "fixture_id": fixture.fixture_id,
            "fixture_version": fixture.fixture_version,
            "fixture_hash": fixture.fixture_hash,
            "workflow_schema_version": P3_SCHEMA_VERSION,
            "workflow_engine_version": P3_ENGINE_VERSION,
        },
    )
    primitive = PrimitiveSpec.create(
        primitive_id=primitive_id,
        kind=PrimitiveKind.SP,
        molecule_ref=fixture.molecule_ref,
        method_profile_id=METHOD_PROFILE_ID,
        depends_on=(),
        parameters={
            "fixture_id": fixture.fixture_id,
            "fixture_version": fixture.fixture_version,
        },
    )
    proposal = PlanProposal.create(
        proposal_id=proposal_id,
        problem_spec_id=problem.record_id,
        problem_spec_hash=sha256_hex(problem),
        steps=(primitive,),
        rationale="Fixed P3 Water fixture plan; no user-selected method or backend.",
        planner_id="p3.water.fixture.planner.v1",
    )
    action = ValidatedAction.create(
        proposal_hash=sha256_hex(proposal),
        primitive=primitive,
        execution_envelope=ExecutionEnvelope(
            backend_kind=BackendKind.FAKE,
            artifact_namespace_id=artifact_namespace_id or new_id(ArtifactId),
        ),
        budget=WATER_BUDGET,
        action_id=action_id,
    )
    validation = validate_water_action(action, proposal=proposal, fixture=fixture)
    if not validation.valid:
        raise ContractInvariantError(validation.code, details=validation.details)
    return WaterPlanBundle(fixture=fixture, problem=problem, proposal=proposal, action=action)


@dataclass(frozen=True)
class WaterValidation:
    valid: bool
    code: str
    details: dict[str, object]


def validate_water_action(
    action: ValidatedAction,
    *,
    proposal: PlanProposal | None = None,
    fixture: WaterFixture | None = None,
) -> WaterValidation:
    """Pure allowlist validation for the only P3 execution action."""

    fixture = fixture or load_water_fixture()
    try:
        action.verify_action_hash()
        if proposal is not None and action.proposal_hash != sha256_hex(proposal):
            return WaterValidation(False, "proposal_hash_mismatch", {})
        primitive = action.primitive
        if primitive.kind is not PrimitiveKind.SP:
            return WaterValidation(False, "only_single_point_is_supported", {})
        if primitive.depends_on:
            return WaterValidation(False, "dependencies_are_not_allowed", {})
        if primitive.molecule_ref != fixture.molecule_ref:
            return WaterValidation(False, "fixture_molecule_mismatch", {})
        if primitive.method_profile_id != METHOD_PROFILE_ID:
            return WaterValidation(False, "method_profile_not_allowed", {})
        parameters = primitive.parameters
        if parameters.get("fixture_id") != fixture.fixture_id:
            return WaterValidation(False, "fixture_id_mismatch", {})
        if parameters.get("fixture_version") != fixture.fixture_version:
            return WaterValidation(False, "fixture_version_mismatch", {})
        if action.execution_envelope.backend_kind is not BackendKind.FAKE:
            return WaterValidation(False, "backend_not_allowed", {})
        if action.budget != WATER_BUDGET:
            return WaterValidation(False, "budget_not_allowed", {})
        return WaterValidation(True, "water_action_valid", {})
    except Exception as error:
        return WaterValidation(False, "action_contract_invalid", {"error": type(error).__name__})


__all__ = [
    "FIXTURE_RESOURCE",
    "METHOD_PROFILE_ID",
    "WATER_BUDGET",
    "WATER_FIXTURE_HASH",
    "WATER_GOAL",
    "WaterFixture",
    "WaterPlanBundle",
    "WaterValidation",
    "build_water_plan",
    "load_water_fixture",
    "validate_water_action",
]
