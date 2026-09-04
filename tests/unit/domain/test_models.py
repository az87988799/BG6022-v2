import pytest
from pydantic import ValidationError

from orca_agent.domain.models import (
    Environment,
    PlanProposal,
    PrimitiveKind,
    PrimitiveSpec,
    ProblemSpec,
)


def _primitive(**overrides: object) -> PrimitiveSpec:
    values: dict[str, object] = {
        "kind": PrimitiveKind.SP,
        "molecule_ref": "water",
        "method_profile_id": "baseline.r2scan3c.v1",
        "parameters": {"charge": 0},
    }
    values.update(overrides)
    return PrimitiveSpec(**values)


def test_problem_spec_deduplicates_targets_in_stable_order() -> None:
    model = ProblemSpec(
        goal="  Compute energy  ",
        molecule_ref="water",
        charge=0,
        multiplicity=1,
        environment=Environment.GAS,
        target_properties=("energy", "frequency", "energy"),
    )

    assert model.goal == "Compute energy"
    assert model.target_properties == ("energy", "frequency")


def test_models_are_strict_and_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ProblemSpec(
            goal="Compute energy",
            molecule_ref="water",
            charge="0",
            multiplicity=1,
            environment=Environment.GAS,
            target_properties=("energy",),
        )
    with pytest.raises(ValidationError):
        ProblemSpec(
            goal="Compute energy",
            molecule_ref="water",
            charge=0,
            multiplicity=1,
            environment=Environment.GAS,
            target_properties=("energy",),
            unexpected="reject",
        )


def test_empty_steps_and_duplicate_step_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanProposal(
            problem_spec_id="problem_" + "0" * 32,
            problem_spec_hash="0" * 64,
            steps=(),
            rationale="fixture",
            planner_id="deterministic:p1-fixture",
        )

    primitive = _primitive()
    with pytest.raises(ValidationError):
        PlanProposal(
            problem_spec_id="problem_" + "0" * 32,
            problem_spec_hash="0" * 64,
            steps=(primitive, primitive),
            rationale="fixture",
            planner_id="deterministic:p1-fixture",
        )


def test_proposal_parameter_keys_cannot_smuggle_execution_details() -> None:
    with pytest.raises(ValidationError):
        _primitive(parameters={"shell_command": "orca input.inp"})
