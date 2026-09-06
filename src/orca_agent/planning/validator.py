"""Pure, closed validation for registry-bound P4 plans."""

from __future__ import annotations

from orca_agent.domain.p4 import ConfirmedMolecule, PreparedPlan
from orca_agent.domain.registry import RegistrySnapshot
from orca_agent.planning.registry import (
    CAPABILITY_GROUND_STATE_PLANNING,
    METHOD_R2SCAN3C,
    PRIMITIVE_FREQUENCY,
    PRIMITIVE_OPTIMIZATION,
    PROTOCOL_GROUND_STATE,
)


class PlanValidationError(ValueError):
    """A safe typed reason for rejecting a registry-bound plan."""


def validate_registry_snapshot(snapshot: RegistrySnapshot) -> None:
    if snapshot.registry_version != "registry-v1":
        raise PlanValidationError("registry snapshot version is unsupported")
    if not isinstance(snapshot.manifest, tuple):
        raise PlanValidationError("registry manifest shape is invalid")
    if (
        tuple(
            sorted(
                snapshot.manifest,
                key=lambda item: (item["kind"], item["registry_id"], item["version"]),
            )
        )
        != snapshot.manifest
    ):
        raise PlanValidationError("registry manifest ordering is invalid")
    if not isinstance(snapshot.protocols, tuple) or PROTOCOL_GROUND_STATE not in snapshot.protocols:
        raise PlanValidationError("required protocol is not registered")
    if not isinstance(snapshot.methods, tuple) or METHOD_R2SCAN3C not in snapshot.methods:
        raise PlanValidationError("required method is not registered")
    if (
        not isinstance(snapshot.capabilities, tuple)
        or CAPABILITY_GROUND_STATE_PLANNING not in snapshot.capabilities
    ):
        raise PlanValidationError("required planning capability is not registered")
    for required in (PRIMITIVE_OPTIMIZATION, PRIMITIVE_FREQUENCY):
        if not isinstance(snapshot.primitives, tuple) or required not in snapshot.primitives:
            raise PlanValidationError("required primitive is not registered")


def validate_ground_state_plan(
    plan: PreparedPlan,
    *,
    confirmed: ConfirmedMolecule,
    snapshot: RegistrySnapshot,
) -> None:
    """Check every reference, parameter, geometry edge, and capability flag."""

    validate_registry_snapshot(snapshot)
    if plan.run_id != confirmed.run_id:
        raise PlanValidationError("plan and confirmed molecule have different runs")
    if (
        plan.confirmed_molecule_id != confirmed.record_id
        or plan.confirmed_molecule_hash != confirmed.identity_record_hash
    ):
        raise PlanValidationError("plan molecule binding is invalid")
    if (
        plan.registry_snapshot_id != snapshot.record_id
        or plan.registry_snapshot_hash != snapshot.snapshot_hash
    ):
        raise PlanValidationError("plan registry binding is invalid")
    if plan.problem_spec.molecule_ref != str(confirmed.record_id):
        raise PlanValidationError("problem spec does not reference the confirmed molecule")
    if (
        plan.problem_spec.charge != confirmed.formal_charge
        or plan.problem_spec.multiplicity != confirmed.multiplicity
    ):
        raise PlanValidationError("problem spec spin binding is invalid")
    if plan.problem_spec.environment.value != PROTOCOL_GROUND_STATE.environment:
        raise PlanValidationError("plan environment is not supported by protocol")
    steps = plan.proposal.steps
    if tuple(step.kind.value for step in steps) != ("opt", "freq") or len(steps) != 2:
        raise PlanValidationError("P4 protocol requires exactly Opt then Freq")
    opt, freq = steps
    if opt.molecule_ref != str(confirmed.record_id) or freq.molecule_ref != str(
        confirmed.record_id
    ):
        raise PlanValidationError("primitive identity binding is invalid")
    if (
        opt.method_profile_id != METHOD_R2SCAN3C.registry_id
        or freq.method_profile_id != METHOD_R2SCAN3C.registry_id
    ):
        raise PlanValidationError("primitive method binding is invalid")
    if opt.depends_on or freq.depends_on != (opt.primitive_id,):
        raise PlanValidationError("primitive dependency graph is invalid")
    if opt.parameters != {"geometry_input": PRIMITIVE_OPTIMIZATION.input_geometry}:
        raise PlanValidationError("Opt geometry parameter is invalid")
    if freq.parameters != {
        "geometry_input": "from_primitive_output",
        "source_primitive_id": str(opt.primitive_id),
        "source_output": PRIMITIVE_OPTIMIZATION.output_geometry,
    }:
        raise PlanValidationError("Freq geometry parameter is invalid")
    if set(step.primitive_id for step in steps) != {opt.primitive_id, freq.primitive_id}:
        raise PlanValidationError("primitive IDs are not unique")
    if (
        plan.capability_id != CAPABILITY_GROUND_STATE_PLANNING.registry_id
        or not plan.planning_valid
    ):
        raise PlanValidationError("plan capability is invalid")
    if plan.execution_ready or plan.execution_approved or plan.real_scientific_result:
        raise PlanValidationError("P4 plan cannot be execution-ready")
    expected_geometry = {
        "optimization_input": {
            "kind": "logical_reference",
            "reference": PRIMITIVE_OPTIMIZATION.input_geometry,
        },
        "frequency_input": {
            "kind": "primitive_output",
            "primitive_id": str(opt.primitive_id),
            "output": PRIMITIVE_OPTIMIZATION.output_geometry,
        },
    }
    if plan.geometry_bindings != expected_geometry:
        raise PlanValidationError("geometry bindings are invalid")


__all__ = ["PlanValidationError", "validate_ground_state_plan", "validate_registry_snapshot"]
