"""Closed P5 execution protocol registry and deterministic plan expansion."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import WorkflowRecordId, new_id
from orca_agent.domain.p5 import (
    P5Budget,
    P5ExecutionNode,
    P5ExecutionPlan,
    P5GeometrySource,
    P5Model,
    P5NodeKind,
)
from orca_agent.orchestration.p5_versions import P5_REGISTRY_VERSION
from orca_agent.planning.registry import METHOD_R2SCAN3C


class P5ProtocolSpec(P5Model):
    protocol_id: str
    version: str = "1"
    nodes: tuple[P5NodeKind, ...] = Field(min_length=1, max_length=3)
    geometry_sources: tuple[P5GeometrySource, ...]
    dependencies: tuple[tuple[int, ...], ...]
    source_from_opt: bool = False
    content: Mapping[str, object]
    protocol_hash: str


def _protocol(
    protocol_id: str,
    nodes: tuple[P5NodeKind, ...],
    sources: tuple[P5GeometrySource, ...],
    dependencies: tuple[tuple[int, ...], ...],
    *,
    source_from_opt: bool = False,
) -> P5ProtocolSpec:
    content = {
        "registry_version": P5_REGISTRY_VERSION,
        "method_profile_id": METHOD_R2SCAN3C.registry_id,
        "method_profile_hash": METHOD_R2SCAN3C.entry_hash,
        "nodes": [item.value for item in nodes],
        "geometry_sources": [item.value for item in sources],
        "dependencies": [list(item) for item in dependencies],
        "source_from_opt": source_from_opt,
    }
    hash_values = {
        "protocol_id": protocol_id,
        "version": "1",
        "nodes": list(nodes),
        "geometry_sources": list(sources),
        "dependencies": [list(item) for item in dependencies],
        "source_from_opt": source_from_opt,
        "content": content,
    }
    return P5ProtocolSpec(
        protocol_id=protocol_id,
        version="1",
        nodes=nodes,
        geometry_sources=sources,
        dependencies=dependencies,
        source_from_opt=source_from_opt,
        content=content,
        protocol_hash=sha256_hex(hash_values),
    )


P5_SP_INITIAL = _protocol(
    "p5.sp_initial.r2scan3c.v1", (P5NodeKind.SP,), (P5GeometrySource.INITIAL,), ((),)
)
P5_OPT_ONLY = _protocol(
    "p5.opt_only.r2scan3c.v1", (P5NodeKind.OPT,), (P5GeometrySource.INITIAL,), ((),)
)
P5_FREQ_FROM_OPT = _protocol(
    "p5.freq_from_opt.r2scan3c.v1",
    (P5NodeKind.FREQ,),
    (P5GeometrySource.OPTIMIZED,),
    ((),),
    source_from_opt=True,
)
P5_OPT_FREQ = _protocol(
    "p5.opt_freq.r2scan3c.v1",
    (P5NodeKind.OPT, P5NodeKind.FREQ),
    (P5GeometrySource.INITIAL, P5GeometrySource.OPTIMIZED),
    ((), (0,)),
)
P5_OPT_FREQ_SP = _protocol(
    "p5.opt_freq_sp.r2scan3c.v1",
    (P5NodeKind.OPT, P5NodeKind.FREQ, P5NodeKind.SP),
    (P5GeometrySource.INITIAL, P5GeometrySource.OPTIMIZED, P5GeometrySource.OPTIMIZED),
    ((), (0,), (0, 1)),
)

P5_PROTOCOLS = (
    P5_SP_INITIAL,
    P5_OPT_ONLY,
    P5_FREQ_FROM_OPT,
    P5_OPT_FREQ,
    P5_OPT_FREQ_SP,
)
P5_PROTOCOLS_BY_ID = {item.protocol_id: item for item in P5_PROTOCOLS}


def get_p5_protocol(protocol_id: str) -> P5ProtocolSpec:
    try:
        return P5_PROTOCOLS_BY_ID[protocol_id]
    except KeyError as error:
        raise ValueError("unknown P5 execution protocol") from error


def expand_p5_execution_plan(
    *,
    run_id,
    source_run_id,
    source_plan_id: WorkflowRecordId,
    source_plan_hash: str,
    confirmed_molecule_id: WorkflowRecordId,
    confirmed_molecule_hash: str,
    source_registry_snapshot_id: WorkflowRecordId,
    source_registry_snapshot_hash: str,
    protocol_id: str,
    record_id: WorkflowRecordId | None = None,
    external_opt_result_id: WorkflowRecordId | None = None,
    wall_time_seconds: int | None = None,
) -> P5ExecutionPlan:
    spec = get_p5_protocol(protocol_id)
    if spec.source_from_opt and external_opt_result_id is None:
        raise ValueError("freq_from_opt requires an explicit completed P5 Opt result")
    nodes: list[P5ExecutionNode] = []
    for index, (kind, geometry, dependency_indexes) in enumerate(
        zip(spec.nodes, spec.geometry_sources, spec.dependencies, strict=True)
    ):
        node_id = f"{protocol_id}:node-{index + 1}"
        dependencies = tuple(nodes[item].node_id for item in dependency_indexes)
        source_node_id = (
            str(external_opt_result_id)
            if spec.source_from_opt
            else (dependencies[0] if kind is P5NodeKind.FREQ and dependencies else None)
        )
        nodes.append(
            P5ExecutionNode(
                node_id=node_id,
                kind=kind,
                geometry_source=geometry,
                depends_on=dependencies,
                source_node_id=source_node_id,
                method_profile_id=METHOD_R2SCAN3C.registry_id,
                method_profile_hash=METHOD_R2SCAN3C.entry_hash,
                budget=P5Budget.defaults_for(kind)
                if wall_time_seconds is None
                else P5Budget(wall_time_seconds=wall_time_seconds),
            )
        )
    values = {
        "record_id": str(record_id or new_id(WorkflowRecordId)),
        "source_run_id": str(source_run_id),
        "source_plan_id": str(source_plan_id),
        "source_plan_hash": source_plan_hash,
        "confirmed_molecule_id": str(confirmed_molecule_id),
        "confirmed_molecule_hash": confirmed_molecule_hash,
        "protocol_id": spec.protocol_id,
        "protocol_hash": spec.protocol_hash,
        "registry_snapshot_id": str(source_registry_snapshot_id),
        "registry_snapshot_hash": source_registry_snapshot_hash,
        "method_profile_id": METHOD_R2SCAN3C.registry_id,
        "method_profile_hash": METHOD_R2SCAN3C.entry_hash,
        "nodes": tuple(node.model_dump(mode="python") for node in nodes),
        "run_budget_seconds": 3600,
    }
    hash_values = {
        **values,
        "nodes": [node.model_dump(mode="json") for node in nodes],
    }
    return P5ExecutionPlan(**values, plan_hash=sha256_hex(hash_values))


__all__ = [
    "P5_FREQ_FROM_OPT",
    "P5_OPT_FREQ",
    "P5_OPT_FREQ_SP",
    "P5_OPT_ONLY",
    "P5_PROTOCOLS",
    "P5_PROTOCOLS_BY_ID",
    "P5ProtocolSpec",
    "P5_SP_INITIAL",
    "expand_p5_execution_plan",
    "get_p5_protocol",
]
