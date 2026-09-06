"""Fixed P4 registry data; there is no runtime plugin or hot reload path."""

from __future__ import annotations

from orca_agent.domain.registry import (
    CapabilityRegistryEntry,
    MethodRegistryEntry,
    PrimitiveRegistryEntry,
    ProtocolRegistryEntry,
    RegistrySnapshot,
)
from orca_agent.orchestration.p4_versions import P4_REGISTRY_VERSION

METHOD_R2SCAN3C = MethodRegistryEntry.create(
    registry_id="baseline.r2scan3c.v1",
    version="1",
    orca_version="6.1",
    category="composite_dft",
    method_name="r2SCAN-3c",
    declared_primitives=("single_point_v1", "geometry_optimization_v1", "frequency_v1"),
    maturity="draft",
    content={
        "orca_version": "6.1",
        "category": "composite_dft",
        "method_name": "r2SCAN-3c",
        "declared_use": ["sp", "opt", "freq"],
        "basis_policy": "composite_method_defined",
    },
)

PRIMITIVE_SINGLE_POINT = PrimitiveRegistryEntry.create(
    registry_id="single_point_v1",
    version="1",
    kind="sp",
    input_geometry="optimized_geometry",
    output_geometry="single_point_observation",
    allowed_parameters=(),
    content={"execution": "p5_only", "registered": True},
)

PRIMITIVE_OPTIMIZATION = PrimitiveRegistryEntry.create(
    registry_id="geometry_optimization_v1",
    version="1",
    kind="opt",
    input_geometry="initial_geometry_for_confirmed_identity",
    output_geometry="optimized_geometry",
    allowed_parameters=("geometry_input",),
    content={"execution": "p5_only", "initial_geometry_required": True},
)

PRIMITIVE_FREQUENCY = PrimitiveRegistryEntry.create(
    registry_id="frequency_v1",
    version="1",
    kind="freq",
    input_geometry="optimized_geometry",
    output_geometry="frequency_observation",
    allowed_parameters=("geometry_input", "source_output", "source_primitive_id"),
    content={"execution": "p5_only", "optimized_geometry_required": True},
)

PROTOCOL_GROUND_STATE = ProtocolRegistryEntry.create(
    registry_id="ground_state_baseline_r2scan3c_v1",
    version="1",
    environment="gas",
    supported_elements=("Br", "C", "Cl", "F", "H", "I", "N", "O", "P", "S"),
    max_non_h_atoms=50,
    method_profile_id="baseline.r2scan3c.v1",
    primitive_template=("geometry_optimization_v1", "frequency_v1"),
    content={
        "environment": "gas",
        "neutral": True,
        "single_component": True,
        "closed_shell_singlet": True,
        "stereochemistry": "fully_specified",
        "radical": False,
        "opt_then_freq": True,
    },
)

CAPABILITY_GROUND_STATE_PLANNING = CapabilityRegistryEntry.create(
    registry_id="ground_state_baseline_planning_v1",
    version="1",
    mode="planning_only",
    can_plan=True,
    can_execute=False,
    content={"execution_backend": None, "requires_p5": True},
)

FIXED_METHODS = (METHOD_R2SCAN3C,)
FIXED_PRIMITIVES = (PRIMITIVE_SINGLE_POINT, PRIMITIVE_OPTIMIZATION, PRIMITIVE_FREQUENCY)
FIXED_PROTOCOLS = (PROTOCOL_GROUND_STATE,)
FIXED_CAPABILITIES = (CAPABILITY_GROUND_STATE_PLANNING,)


def build_registry_snapshot(*, record_id=None) -> RegistrySnapshot:
    return RegistrySnapshot.create(
        record_id=record_id,
        registry_version=P4_REGISTRY_VERSION,
        methods=FIXED_METHODS,
        primitives=FIXED_PRIMITIVES,
        protocols=FIXED_PROTOCOLS,
        capabilities=FIXED_CAPABILITIES,
    )


def fixed_registry_snapshot(*, record_id=None) -> RegistrySnapshot:
    """Alias emphasizing that this is a closed implementation registry."""

    return build_registry_snapshot(record_id=record_id)


__all__ = [
    "CAPABILITY_GROUND_STATE_PLANNING",
    "FIXED_CAPABILITIES",
    "FIXED_METHODS",
    "FIXED_PRIMITIVES",
    "FIXED_PROTOCOLS",
    "METHOD_R2SCAN3C",
    "PRIMITIVE_FREQUENCY",
    "PRIMITIVE_OPTIMIZATION",
    "PRIMITIVE_SINGLE_POINT",
    "PROTOCOL_GROUND_STATE",
    "build_registry_snapshot",
    "fixed_registry_snapshot",
]
