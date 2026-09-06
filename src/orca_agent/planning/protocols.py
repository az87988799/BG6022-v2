"""Pure expansion of the P4 ground-state planning protocol."""

from __future__ import annotations

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import PrimitiveId, new_id
from orca_agent.domain.models import (
    Environment,
    PlanProposal,
    PrimitiveKind,
    PrimitiveSpec,
    ProblemSpec,
)
from orca_agent.domain.p4 import ConfirmedMolecule, PreparedPlan
from orca_agent.domain.registry import RegistrySnapshot
from orca_agent.planning.registry import (
    CAPABILITY_GROUND_STATE_PLANNING,
    METHOD_R2SCAN3C,
    PRIMITIVE_OPTIMIZATION,
    PROTOCOL_GROUND_STATE,
)


def expand_ground_state_plan(
    *,
    confirmed: ConfirmedMolecule,
    snapshot: RegistrySnapshot,
    problem_spec_id=None,
    proposal_id=None,
    optimization_id: PrimitiveId | None = None,
    frequency_id: PrimitiveId | None = None,
    prepared_plan_id=None,
) -> PreparedPlan:
    """Build the fixed Opt -> Freq DAG without creating an execution action."""

    if PROTOCOL_GROUND_STATE not in snapshot.protocols:
        raise ValueError("ground-state protocol is not present in the snapshot")
    if METHOD_R2SCAN3C not in snapshot.methods:
        raise ValueError("r2SCAN-3c method is not present in the snapshot")
    if CAPABILITY_GROUND_STATE_PLANNING not in snapshot.capabilities:
        raise ValueError("planning capability is not present in the snapshot")
    if (
        not CAPABILITY_GROUND_STATE_PLANNING.can_plan
        or CAPABILITY_GROUND_STATE_PLANNING.can_execute
    ):
        raise ValueError("P4 capability is not planning-only")

    opt_id = optimization_id or new_id(PrimitiveId)
    freq_id = frequency_id or new_id(PrimitiveId)
    opt = PrimitiveSpec.create(
        primitive_id=opt_id,
        kind=PrimitiveKind.OPT,
        molecule_ref=str(confirmed.record_id),
        method_profile_id=METHOD_R2SCAN3C.registry_id,
        parameters={"geometry_input": PRIMITIVE_OPTIMIZATION.input_geometry},
    )
    freq = PrimitiveSpec.create(
        primitive_id=freq_id,
        kind=PrimitiveKind.FREQ,
        molecule_ref=str(confirmed.record_id),
        method_profile_id=METHOD_R2SCAN3C.registry_id,
        depends_on=(opt_id,),
        parameters={
            "geometry_input": "from_primitive_output",
            "source_primitive_id": str(opt_id),
            "source_output": PRIMITIVE_OPTIMIZATION.output_geometry,
        },
    )
    problem = ProblemSpec.create(
        record_id=problem_spec_id,
        goal="prepare ground-state baseline plan",
        molecule_ref=str(confirmed.record_id),
        charge=confirmed.formal_charge,
        multiplicity=confirmed.multiplicity,
        environment=Environment.GAS,
        target_properties=("optimized_geometry", "frequencies"),
        constraints={"protocol_id": PROTOCOL_GROUND_STATE.registry_id},
    )
    proposal = PlanProposal.create(
        proposal_id=proposal_id,
        problem_spec_id=problem.record_id,
        problem_spec_hash=sha256_hex(problem),
        steps=(opt, freq),
        rationale="Fixed P4 protocol expansion; execution is deferred to P5.",
        planner_id="p4.fixed.protocol",
    )
    geometry_bindings = {
        "optimization_input": {
            "kind": "logical_reference",
            "reference": PRIMITIVE_OPTIMIZATION.input_geometry,
        },
        "frequency_input": {
            "kind": "primitive_output",
            "primitive_id": str(opt_id),
            "output": PRIMITIVE_OPTIMIZATION.output_geometry,
        },
    }
    return PreparedPlan.create(
        record_id=prepared_plan_id,
        run_id=confirmed.run_id,
        confirmed_molecule_id=confirmed.record_id,
        confirmed_molecule_hash=confirmed.identity_record_hash,
        registry_snapshot_id=snapshot.record_id,
        registry_snapshot_hash=snapshot.snapshot_hash,
        protocol_id=PROTOCOL_GROUND_STATE.registry_id,
        protocol_hash=PROTOCOL_GROUND_STATE.entry_hash,
        method_profile_id=METHOD_R2SCAN3C.registry_id,
        method_profile_hash=METHOD_R2SCAN3C.entry_hash,
        problem_spec=problem,
        proposal=proposal,
        geometry_bindings=geometry_bindings,
        capability_id=CAPABILITY_GROUND_STATE_PLANNING.registry_id,
    )


__all__ = ["expand_ground_state_plan"]
