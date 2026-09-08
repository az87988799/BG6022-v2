"""Closed P5 execution protocol registry and deterministic plan expansion."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field, model_validator

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import WorkflowRecordId, new_id
from orca_agent.domain.p5 import (
    P5_DEFAULT_MAXCORE_MB,
    P5_DEFAULT_NPROCS,
    P5_DEFAULT_TOTAL_MEMORY_MB,
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
    nprocs: int = Field(default=P5_DEFAULT_NPROCS, ge=1, le=64)
    total_memory_mb: int = Field(default=P5_DEFAULT_TOTAL_MEMORY_MB, ge=256, le=1_048_576)
    maxcore_mb: int = Field(default=P5_DEFAULT_MAXCORE_MB, ge=1, le=1_048_576)
    parallel: bool = True
    implicit_threads: int = Field(default=1, ge=1)
    run_wall_time_seconds: int = Field(default=3600, ge=1, le=604_800)
    wall_time_seconds: Mapping[str, int] = Field(default_factory=dict)
    fixed_budget: bool = False

    @model_validator(mode="after")
    def _resource_invariants(self) -> P5ProtocolSpec:
        expected = (self.total_memory_mb * 75) // (100 * self.nprocs)
        if self.maxcore_mb != expected:
            raise ValueError("protocol maxcore must follow the 75% per-process rule")
        if self.nprocs > 1 and not self.parallel:
            raise ValueError("multi-process protocol requires parallel mode")
        if self.implicit_threads != 1:
            raise ValueError("P5 fixes implicit computational threads to one")
        return self

    def budget_for(self, kind: P5NodeKind, *, wall_time_seconds: int | None = None) -> P5Budget:
        if self.fixed_budget and wall_time_seconds is not None:
            raise ValueError("the registered four-core protocol has fixed node budgets")
        base = P5Budget.defaults_for(kind)
        default_wall = base.wall_time_seconds
        seconds = self.wall_time_seconds.get(kind.value, default_wall)
        if wall_time_seconds is not None:
            seconds = wall_time_seconds
        return base.model_copy(
            update={
                "nprocs": self.nprocs,
                "total_memory_mb": self.total_memory_mb,
                "maxcore_mb": self.maxcore_mb,
                "wall_time_seconds": seconds,
                "run_wall_time_seconds": self.run_wall_time_seconds,
            }
        )

    def feature_profile(self) -> dict[str, object]:
        return {"parallel": self.parallel, "implicit_threads": self.implicit_threads}

    def budget_is_registered(self, budget: P5Budget, kind: P5NodeKind) -> bool:
        expected = self.budget_for(kind)
        resource_fields = (
            "nprocs",
            "total_memory_mb",
            "maxcore_mb",
            "run_wall_time_seconds",
        )
        if any(getattr(budget, field) != getattr(expected, field) for field in resource_fields):
            return False
        return not self.fixed_budget or budget.wall_time_seconds == expected.wall_time_seconds


def _protocol(
    protocol_id: str,
    nodes: tuple[P5NodeKind, ...],
    sources: tuple[P5GeometrySource, ...],
    dependencies: tuple[tuple[int, ...], ...],
    *,
    version: str = "1",
    source_from_opt: bool = False,
    nprocs: int = P5_DEFAULT_NPROCS,
    total_memory_mb: int = P5_DEFAULT_TOTAL_MEMORY_MB,
    maxcore_mb: int = P5_DEFAULT_MAXCORE_MB,
    parallel: bool = True,
    implicit_threads: int = 1,
    run_wall_time_seconds: int = 3600,
    fixed_budget: bool = False,
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
    wall_time_by_kind = {
        kind.value: P5Budget.defaults_for(kind).wall_time_seconds for kind in nodes
    }
    if fixed_budget or nprocs != 1 or total_memory_mb != 2048 or maxcore_mb != 1536:
        content["resource_profile"] = {
            "nprocs": nprocs,
            "total_memory_mb": total_memory_mb,
            "maxcore_mb": maxcore_mb,
            "parallel": parallel,
            "implicit_threads": implicit_threads,
            "run_wall_time_seconds": run_wall_time_seconds,
            "wall_time_seconds": wall_time_by_kind,
        }
    hash_values = {
        "protocol_id": protocol_id,
        "version": version,
        "nodes": list(nodes),
        "geometry_sources": list(sources),
        "dependencies": [list(item) for item in dependencies],
        "source_from_opt": source_from_opt,
        "content": content,
    }
    return P5ProtocolSpec(
        protocol_id=protocol_id,
        version=version,
        nodes=nodes,
        geometry_sources=sources,
        dependencies=dependencies,
        source_from_opt=source_from_opt,
        content=content,
        protocol_hash=sha256_hex(hash_values),
        nprocs=nprocs,
        total_memory_mb=total_memory_mb,
        maxcore_mb=maxcore_mb,
        parallel=parallel,
        implicit_threads=implicit_threads,
        run_wall_time_seconds=run_wall_time_seconds,
        wall_time_seconds=wall_time_by_kind if fixed_budget else {},
        fixed_budget=fixed_budget,
    )


P5_SP_INITIAL = _protocol(
    "p5.sp_initial.r2scan3c.v1",
    (P5NodeKind.SP,),
    (P5GeometrySource.INITIAL,),
    ((),),
    nprocs=1,
    total_memory_mb=2048,
    maxcore_mb=1536,
    parallel=False,
)
P5_OPT_ONLY = _protocol(
    "p5.opt_only.r2scan3c.v1",
    (P5NodeKind.OPT,),
    (P5GeometrySource.INITIAL,),
    ((),),
    nprocs=1,
    total_memory_mb=2048,
    maxcore_mb=1536,
    parallel=False,
)
P5_FREQ_FROM_OPT = _protocol(
    "p5.freq_from_opt.r2scan3c.v1",
    (P5NodeKind.FREQ,),
    (P5GeometrySource.OPTIMIZED,),
    ((),),
    source_from_opt=True,
    nprocs=1,
    total_memory_mb=2048,
    maxcore_mb=1536,
    parallel=False,
)
P5_OPT_FREQ = _protocol(
    "p5.opt_freq.r2scan3c.v1",
    (P5NodeKind.OPT, P5NodeKind.FREQ),
    (P5GeometrySource.INITIAL, P5GeometrySource.OPTIMIZED),
    ((), (0,)),
    nprocs=1,
    total_memory_mb=2048,
    maxcore_mb=1536,
    parallel=False,
)
P5_OPT_FREQ_SP = _protocol(
    "p5.opt_freq_sp.r2scan3c.v1",
    (P5NodeKind.OPT, P5NodeKind.FREQ, P5NodeKind.SP),
    (P5GeometrySource.INITIAL, P5GeometrySource.OPTIMIZED, P5GeometrySource.OPTIMIZED),
    ((), (0,), (0, 1)),
    nprocs=1,
    total_memory_mb=2048,
    maxcore_mb=1536,
    parallel=False,
)
P5_OPT_FREQ_SP_4CORE = _protocol(
    "p5.opt_freq_sp.r2scan3c.4core.v2",
    (P5NodeKind.OPT, P5NodeKind.FREQ, P5NodeKind.SP),
    (P5GeometrySource.INITIAL, P5GeometrySource.OPTIMIZED, P5GeometrySource.OPTIMIZED),
    ((), (0,), (0, 1)),
    version="2",
    nprocs=4,
    total_memory_mb=4096,
    maxcore_mb=768,
    parallel=True,
    fixed_budget=True,
)
P5_OPT_FREQ_SP_4CORE_2048 = _protocol(
    "p5.opt_freq_sp.r2scan3c.4core.v3",
    (P5NodeKind.OPT, P5NodeKind.FREQ, P5NodeKind.SP),
    (P5GeometrySource.INITIAL, P5GeometrySource.OPTIMIZED, P5GeometrySource.OPTIMIZED),
    ((), (0,), (0, 1)),
    version="3",
    fixed_budget=True,
)

P5_DEFAULT_PROTOCOL = P5_OPT_FREQ_SP_4CORE_2048

P5_PROTOCOLS = (
    P5_SP_INITIAL,
    P5_OPT_ONLY,
    P5_FREQ_FROM_OPT,
    P5_OPT_FREQ,
    P5_OPT_FREQ_SP,
    P5_OPT_FREQ_SP_4CORE,
    P5_OPT_FREQ_SP_4CORE_2048,
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
                budget=spec.budget_for(kind, wall_time_seconds=wall_time_seconds),
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
    "P5_OPT_FREQ_SP_4CORE",
    "P5_OPT_FREQ_SP_4CORE_2048",
    "P5_DEFAULT_PROTOCOL",
    "P5_OPT_ONLY",
    "P5_PROTOCOLS",
    "P5_PROTOCOLS_BY_ID",
    "P5ProtocolSpec",
    "P5_SP_INITIAL",
    "expand_p5_execution_plan",
    "get_p5_protocol",
]
