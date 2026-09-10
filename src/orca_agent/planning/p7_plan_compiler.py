"""Deterministic compiler from P7 candidate graphs to registered P5 plans."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from orca_agent.domain.p7_planning import (
    GeometryRef,
    HistoryGeometryRef,
    InitialGeometryRef,
    PlanProposal,
    PlanProposalStep,
    StepOutputGeometryRef,
    parse_geometry_ref,
)
from orca_agent.domain.p7_task import (
    CalculationPlan,
    CalculationRequest,
    OutputKind,
    PlanNode,
    PlanValidation,
    ValidationStatus,
)
from orca_agent.planning.p5_protocols import (
    P5_FREQ_FROM_OPT_4CORE_2048,
    P5_OPT_FREQ_4CORE_2048,
    P5_OPT_FREQ_SP_4CORE_2048,
    P5_OPT_ONLY_4CORE_2048,
    P5_OPT_SP_4CORE_2048,
    P5_SP_INITIAL_4CORE_2048,
    P5ProtocolSpec,
)
from orca_agent.planning.p7_catalog import (
    P7_FREQ_CAPABILITY_ID,
    P7_OPT_CAPABILITY_ID,
    P7_OPT_FREQ_CAPABILITY_ID,
    P7_OPT_FREQ_SP_CAPABILITY_ID,
    P7_OPT_SP_CAPABILITY_ID,
    P7_SP_CAPABILITY_ID,
    CapabilityCatalog,
)

_CAPABILITY_TO_KIND = {
    P7_OPT_CAPABILITY_ID: "opt",
    P7_FREQ_CAPABILITY_ID: "freq",
    P7_SP_CAPABILITY_ID: "sp",
}
_COMPOSITE_CAPABILITY_BY_OPERATIONS = {
    ("opt",): P7_OPT_CAPABILITY_ID,
    ("freq",): P7_FREQ_CAPABILITY_ID,
    ("sp",): P7_SP_CAPABILITY_ID,
    ("opt", "freq"): P7_OPT_FREQ_CAPABILITY_ID,
    ("opt", "sp"): P7_OPT_SP_CAPABILITY_ID,
    ("opt", "freq", "sp"): P7_OPT_FREQ_SP_CAPABILITY_ID,
}
_PROTOCOL_BY_OPERATIONS: dict[tuple[str, ...], P5ProtocolSpec] = {
    ("opt",): P5_OPT_ONLY_4CORE_2048,
    ("freq",): P5_FREQ_FROM_OPT_4CORE_2048,
    ("sp",): P5_SP_INITIAL_4CORE_2048,
    ("opt", "freq"): P5_OPT_FREQ_4CORE_2048,
    ("opt", "sp"): P5_OPT_SP_4CORE_2048,
    ("opt", "freq", "sp"): P5_OPT_FREQ_SP_4CORE_2048,
}

_OUTPUT_PRODUCERS: dict[OutputKind, set[str]] = {
    OutputKind.OPT_FINAL_ELECTRONIC_ENERGY: {"opt"},
    OutputKind.INDEPENDENT_SP_ELECTRONIC_ENERGY: {"sp"},
    OutputKind.VIBRATIONAL_FREQUENCIES: {"freq"},
    OutputKind.LOCAL_MINIMUM_SUPPORT: {"freq"},
    OutputKind.OPTIMIZED_GEOMETRY: {"opt"},
    OutputKind.EXECUTION_STATUS: {"opt", "freq", "sp"},
    OutputKind.SCIENTIFIC_STATUS: {"opt", "freq", "sp"},
    OutputKind.REPORT: {"opt", "freq", "sp"},
    OutputKind.EXISTING_ENERGY_DIFFERENCE: {"sp"},
}


@dataclass(frozen=True)
class PlanCompilation:
    """Compiler result; no P5 side effect occurs while creating it."""

    status: ValidationStatus
    plan: CalculationPlan | None
    issues: tuple[str, ...] = ()
    clarification_question: str | None = None
    proposal: PlanProposal | None = None
    operations: tuple[str, ...] = ()
    protocol_id: str | None = None
    source_task_id: str | None = None
    source_task_alias: str | None = None
    external_opt_result_id: str | None = None

    @property
    def valid(self) -> bool:
        return self.status is ValidationStatus.VALID and self.plan is not None


def _normalise_operations(operations: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(str(item).casefold().strip() for item in operations)


def operations_from_proposal(proposal: PlanProposal, catalog: CapabilityCatalog) -> tuple[str, ...]:
    """Map a candidate's registered atomic nodes to the allowed sequence."""

    kinds: list[str] = []
    for step in proposal.steps:
        if step.capability_id in _CAPABILITY_TO_KIND:
            kinds.append(_CAPABILITY_TO_KIND[step.capability_id])
            continue
        entry = catalog.get(step.capability_id, "1")
        if entry is None:
            kinds.append("")
            continue
        protocol_nodes = tuple(
            node.value for node in _protocol_for_descriptor(entry.descriptor.protocol_id).nodes
        )
        if len(proposal.steps) == 1:
            kinds.extend(protocol_nodes)
        else:
            kinds.append("")
    return tuple(kinds)


def proposal_from_operations(
    operations: tuple[str, ...] | list[str],
    *,
    goal: str,
    requested_outputs: tuple[str, ...] | list[str] = (),
    action: str = "plan_new",
    source_input_ref: str | GeometryRef | None = "current_task.optimized_geometry",
) -> PlanProposal:
    """Create a program-owned candidate for deterministic/offline routing."""

    sequence = _normalise_operations(operations)
    steps: list[PlanProposalStep] = []
    opt_key: str | None = None
    for index, kind in enumerate(sequence, start=1):
        key = f"s{index}"
        if kind == "opt":
            input_ref = "current_molecule.initial_geometry"
            opt_key = key
        elif kind == "freq":
            input_ref = (
                f"{opt_key}.optimized_geometry"
                if opt_key
                else source_input_ref or "current_task.optimized_geometry"
            )
        elif kind == "sp":
            # A standalone SP is bound to the selected molecule's initial
            # geometry.  A downstream SP in Opt→SP/Opt→Freq→SP is bound to
            # the Opt node, never to the preceding Freq node.
            input_ref = (
                f"{opt_key}.optimized_geometry" if opt_key else "current_molecule.initial_geometry"
            )
        else:
            input_ref = "current_molecule.initial_geometry"
        capability_id = {
            "opt": P7_OPT_CAPABILITY_ID,
            "freq": P7_FREQ_CAPABILITY_ID,
            "sp": P7_SP_CAPABILITY_ID,
        }.get(kind, f"orca.{kind}.v1")
        depends = () if not steps else (steps[-1].key,)
        # SP reads the optimized geometry.  In the complete chain it also
        # waits for the Freq node; removing Freq from the candidate therefore
        # produces the distinct Opt→SP graph.
        if kind == "sp" and opt_key is not None:
            depends = (opt_key,) if steps[-1].key == opt_key else (opt_key, steps[-1].key)
        steps.append(
            PlanProposalStep(
                key=key,
                capability_id=capability_id,
                input_ref=input_ref,
                depends_on=depends,
                purpose={
                    "opt": "获得优化结构",
                    "freq": "检查频率与局部极小支持",
                    "sp": "在明确几何上计算独立单点能",
                }.get(kind, f"执行 {kind}"),
                why="根据当前用户目标生成的最小必要步骤。",
            )
        )
    return PlanProposal(
        action=action,
        goal=goal,
        strategy_summary="按目标选择最小必要的已注册计算步骤。",
        steps=tuple(steps),
        requested_outputs=tuple(requested_outputs),
    )


def _protocol_for_descriptor(protocol_id: str) -> P5ProtocolSpec:
    for protocol in _PROTOCOL_BY_OPERATIONS.values():
        if protocol.protocol_id == protocol_id:
            return protocol
    # This branch is only used for a custom composite descriptor.  Returning
    # a clear failure in the caller is preferable to inventing a protocol.
    raise ValueError("catalog descriptor is not bound to a P7 candidate protocol")


class P7PlanCompiler:
    """Compile only exact, registered P7 graphs; never silently repair them."""

    def __init__(self, catalog: CapabilityCatalog) -> None:
        self.catalog = catalog

    def compile(
        self,
        request: CalculationRequest,
        proposal: PlanProposal,
        *,
        validation: PlanValidation | None = None,
        trusted_artifacts: Mapping[str, Mapping[str, object]] | None = None,
    ) -> PlanCompilation:
        issues: list[str] = []
        if len(proposal.steps) > 3:
            issues.append("candidate plan may contain at most three calculation nodes")
        if proposal.action not in {"plan_new", "revise_draft"}:
            issues.append("candidate action is unsupported")
        keys = tuple(step.key for step in proposal.steps)
        if len(keys) != len(set(keys)):
            issues.append("candidate step keys must be unique")
        known = set(keys)
        if any(dep not in known for step in proposal.steps for dep in step.depends_on):
            issues.append("candidate dependency references an unknown step")
        if self._has_cycle(proposal.steps):
            issues.append("candidate step dependencies contain a cycle")
        if issues:
            return self._invalid(proposal, tuple(issues))
        if proposal.clarification_fields:
            fields = "、".join(proposal.clarification_fields[:3])
            return self._clarify(
                proposal,
                (f"candidate requests clarification: {fields}",),
                question=(
                    "请明确本次要执行的范围：Opt、Freq（复用已完成 Opt）、"
                    "独立 SP，或 Opt→Freq/Opt→SP/完整 Opt→Freq→SP。"
                ),
            )

        try:
            operations = operations_from_proposal(proposal, self.catalog)
        except ValueError:
            return self._unsupported(
                proposal, ("candidate capability is not bound to a registered protocol",)
            )
        if not operations or any(kind not in {"opt", "freq", "sp"} for kind in operations):
            return self._unsupported(
                proposal, ("candidate uses an unknown or disabled capability",)
            )
        if len(operations) != len(proposal.steps):
            return self._unsupported(
                proposal,
                ("candidate must express one registered atomic capability per step",),
                operations=operations,
            )
        if operations not in _PROTOCOL_BY_OPERATIONS:
            return self._unsupported(
                proposal,
                ("candidate graph is not an approved P7 execution combination",),
                operations=operations,
            )
        if tuple(request.operations) and tuple(request.operations) != operations:
            return self._invalid(
                proposal,
                ("candidate operations do not match the normalized execution scope",),
                operations=operations,
            )

        protocol = _PROTOCOL_BY_OPERATIONS[operations]
        capability_id = _COMPOSITE_CAPABILITY_BY_OPERATIONS[operations]
        entry = self.catalog.get(capability_id, "1")
        if entry is None or not entry.descriptor.enabled:
            return self._unsupported(proposal, (f"capability {capability_id} is not enabled",))
        descriptor = entry.descriptor
        if (
            descriptor.protocol_id != protocol.protocol_id
            or descriptor.protocol_hash != protocol.protocol_hash
        ):
            return self._invalid(proposal, ("catalog capability/protocol binding is inconsistent",))
        expected_dependencies = tuple(
            tuple(keys[dependency] for dependency in dependency_indexes)
            for dependency_indexes in protocol.dependencies
        )
        if tuple(step.depends_on for step in proposal.steps) != expected_dependencies:
            return self._invalid(
                proposal,
                ("candidate dependencies do not match the registered protocol",),
                operations=operations,
            )
        for index, step in enumerate(proposal.steps):
            expected_kind = operations[index]
            step_kind = _CAPABILITY_TO_KIND.get(step.capability_id)
            step_entry = self.catalog.get(step.capability_id, "1")
            if step_entry is None or not step_entry.descriptor.enabled:
                return self._unsupported(
                    proposal, (f"candidate capability is unavailable: {step.capability_id}",)
                )
            if step_kind != expected_kind:
                return self._invalid(
                    proposal,
                    (f"step {step.key} does not provide the required {expected_kind} capability",),
                    operations=operations,
                )
            if not self._valid_input_ref(
                self._step_input(step),
                expected_kind,
                proposal.steps,
                index,
                trusted_artifacts or {},
            ):
                return self._clarify(
                    proposal,
                    (f"step {step.key} has no trusted compatible input geometry",),
                    question=(
                        "请指定可访问会话内已完成 Opt 的优化几何，或明确使用当前分子的初始几何。"
                    ),
                    operations=operations,
                )

        output_names = tuple(proposal.requested_outputs) or tuple(
            item.kind.value for item in request.output_spec.quantities
        )
        output_kinds: list[OutputKind] = []
        for name in output_names:
            try:
                output_kinds.append(OutputKind(name))
            except ValueError:
                return self._unsupported(proposal, (f"requested output is not registered: {name}",))
        for quantity in request.output_spec.quantities:
            producers = _OUTPUT_PRODUCERS.get(quantity.kind, set())
            if not producers.intersection(operations):
                if quantity.kind in {
                    OutputKind.VIBRATIONAL_FREQUENCIES,
                    OutputKind.LOCAL_MINIMUM_SUPPORT,
                }:
                    return self._clarify(
                        proposal,
                        (f"required output has no producer: {quantity.kind.value}",),
                        question=(
                            "当前计划已去掉 Freq，但仍要求频率/局部极小结论；"
                            "请保留 Freq，或确认放弃该输出。"
                        ),
                        operations=operations,
                    )
                return self._invalid(
                    proposal,
                    (f"required output has no producer: {quantity.kind.value}",),
                    operations=operations,
                )
        if (
            any(
                self._mentions_frequency(item)
                for item in request.prohibited_requests
                if "freq" in item.casefold() or "频率" in item
            )
            and "freq" in operations
        ):
            if OutputKind.LOCAL_MINIMUM_SUPPORT in output_kinds:
                return self._clarify(
                    proposal,
                    ("frequency is prohibited while local-minimum support is requested",),
                    question="你要保留局部极小验证目标并执行频率，还是放弃该验证后去掉频率？",
                    operations=operations,
                )
            return self._invalid(
                proposal,
                ("candidate contains a step explicitly prohibited by the user",),
                operations=operations,
            )
        for constraint in (*request.hard_constraints, *request.prohibited_requests):
            lowered = constraint.casefold()
            if "global" in lowered or "全局" in constraint or "构象搜索" in constraint:
                return self._unsupported(proposal, ("global conformer minimum is not enabled",))

        trusted = trusted_artifacts or {}
        source_task_id, source_task_alias, external_result_id = self._trusted_source(
            proposal, trusted
        )
        if operations == ("freq",) and not any(
            self._is_reuse_ref(self._step_input(step)) for step in proposal.steps
        ):
            return self._clarify(
                proposal,
                ("frequency requires a completed trusted Opt source",),
                question="请指定当前会话中已完成且可复用的 Opt 任务。",
                operations=operations,
            )
        if operations == ("freq",) and external_result_id is None:
            return self._clarify(
                proposal,
                ("frequency requires a completed trusted Opt result",),
                question="请指定当前会话中已完成且可复用的 Opt 任务。",
                operations=operations,
            )

        validation_hash = "0" * 64 if validation is None else validation.validation_hash
        nodes = tuple(
            PlanNode(
                node_id=f"{protocol.protocol_id}:node-{index + 1}",
                kind=node_kind.value,
                depends_on=tuple(
                    f"{protocol.protocol_id}:node-{dep + 1}" for dep in protocol.dependencies[index]
                ),
                geometry_source=source.value,
                method_profile_id=descriptor.method_profile_id,
                budget=protocol.budget_for(node_kind).model_dump(mode="json"),
            )
            for index, (node_kind, source) in enumerate(
                zip(protocol.nodes, protocol.geometry_sources, strict=True)
            )
        )
        plan = CalculationPlan.create(
            capability_id=descriptor.capability_id,
            capability_version=descriptor.version,
            capability_hash=descriptor.content_hash,
            request_hash=request.request_hash,
            validation_hash=validation_hash,
            output_spec_hash=request.output_spec.output_spec_hash,
            method_profile_id=descriptor.method_profile_id,
            method_profile_hash=descriptor.method_profile_hash,
            environment="gas",
            protocol_id=protocol.protocol_id,
            protocol_hash=protocol.protocol_hash,
            registry_snapshot_id=descriptor.registry_snapshot_id,
            registry_snapshot_hash=descriptor.registry_snapshot_hash,
            nodes=nodes,
            expected_outputs=tuple(dict.fromkeys(output_kinds)),
            applicability={
                "input_kinds": list(descriptor.input_kinds),
                "neutral_closed_shell_singlet": True,
                "single_component": True,
                "one_starting_conformer": True,
                "candidate_step_keys": list(keys),
            },
            resources={
                "nprocs": protocol.nprocs,
                "total_memory_mb": protocol.total_memory_mb,
                "maxcore_mb": protocol.maxcore_mb,
                "implicit_threads": protocol.implicit_threads,
                "parallel": protocol.parallel,
                "real_concurrency": 1,
                "wall_time_seconds": dict(protocol.wall_time_seconds),
            },
            scope_note=(
                f"{proposal.strategy_summary}; one starting conformer; "
                "local minimum support is not a global minimum claim"
            ),
        )
        if validation is None:
            return PlanCompilation(
                ValidationStatus.VALID,
                plan,
                proposal=proposal,
                operations=operations,
                protocol_id=protocol.protocol_id,
                source_task_id=source_task_id,
                source_task_alias=source_task_alias,
                external_opt_result_id=external_result_id,
            )
        return PlanCompilation(
            ValidationStatus.VALID,
            plan,
            proposal=proposal,
            operations=operations,
            protocol_id=protocol.protocol_id,
            source_task_id=source_task_id,
            source_task_alias=source_task_alias,
            external_opt_result_id=external_result_id,
        )

    def compile_plan(self, *args, **kwargs) -> PlanCompilation:
        """Readable alias used by application callers and acceptance tests."""

        return self.compile(*args, **kwargs)

    @staticmethod
    def _has_cycle(steps: tuple[PlanProposalStep, ...]) -> bool:
        graph = {step.key: set(step.depends_on) for step in steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> bool:
            if key in visiting:
                return True
            if key in visited:
                return False
            visiting.add(key)
            if any(visit(dep) for dep in graph.get(key, ())):
                return True
            visiting.remove(key)
            visited.add(key)
            return False

        return any(visit(key) for key in graph)

    @staticmethod
    def _is_reuse_ref(value: str | GeometryRef) -> bool:
        if isinstance(value, HistoryGeometryRef):
            return True
        if not isinstance(value, str):
            return False
        lowered = value.casefold().strip()
        return (
            lowered.startswith("task:")
            or lowered.startswith("current_task.")
            or lowered.startswith("completed_opt.")
            or lowered.startswith("existing_opt.")
        )

    @staticmethod
    def _valid_input_ref(
        value: str | GeometryRef | None,
        kind: str,
        steps: tuple[PlanProposalStep, ...],
        index: int,
        trusted: Mapping[str, Mapping[str, object]],
    ) -> bool:
        if value is None:
            return False
        typed = parse_geometry_ref(value)
        if kind == "opt":
            return isinstance(typed, InitialGeometryRef)
        if kind == "sp":
            if isinstance(typed, InitialGeometryRef):
                return True
            if isinstance(typed, StepOutputGeometryRef):
                return any(
                    step.key.casefold() == typed.step_key.casefold()
                    and _CAPABILITY_TO_KIND.get(step.capability_id) == "opt"
                    for step in steps[:index]
                )
            return False
        if kind == "freq":
            if isinstance(typed, StepOutputGeometryRef):
                return any(
                    step.key.casefold() == typed.step_key.casefold()
                    and _CAPABILITY_TO_KIND.get(step.capability_id) == "opt"
                    for step in steps[:index]
                )
            if isinstance(typed, HistoryGeometryRef):
                alias = typed.task_selector.casefold()
                return alias in trusted
            if P7PlanCompiler._is_reuse_ref(value):
                alias = P7PlanCompiler._alias_from_ref(value)
                return alias in trusted or "current_task" in trusted
            return False
        return False

    @staticmethod
    def _step_input(step: PlanProposalStep) -> str | GeometryRef | None:
        return step.input if step.input is not None else step.input_ref

    @staticmethod
    def _alias_from_ref(value: str | GeometryRef) -> str:
        if isinstance(value, HistoryGeometryRef):
            return value.task_selector.casefold()
        if not isinstance(value, str):
            return ""
        lowered = value.casefold()
        if lowered.startswith("task:"):
            return lowered.split(":", 1)[1].split(".", 1)[0]
        return "current_task"

    @staticmethod
    def _trusted_source(
        proposal: PlanProposal, trusted: Mapping[str, Mapping[str, object]]
    ) -> tuple[str | None, str | None, str | None]:
        for step in proposal.steps:
            input_ref = P7PlanCompiler._step_input(step)
            if input_ref is None or not P7PlanCompiler._is_reuse_ref(input_ref):
                continue
            alias = P7PlanCompiler._alias_from_ref(input_ref)
            # A named history reference is an integrity boundary.  Falling
            # back to the active task can bind a missing ``task:...`` ref to
            # an unrelated completed Opt source.
            value = trusted.get(alias)
            if value is None:
                continue
            return (
                _optional_string(value.get("task_id")),
                _optional_string(value.get("alias")) or alias,
                _optional_string(value.get("result_id")),
            )
        return None, None, None

    @staticmethod
    def _mentions_frequency(value: str) -> bool:
        lowered = value.casefold()
        return "freq" in lowered or "频率" in value

    @staticmethod
    def _invalid(
        proposal: PlanProposal,
        issues: tuple[str, ...],
        *,
        operations: tuple[str, ...] = (),
    ) -> PlanCompilation:
        return PlanCompilation(
            ValidationStatus.INVALID, None, issues, proposal=proposal, operations=operations
        )

    @staticmethod
    def _unsupported(
        proposal: PlanProposal,
        issues: tuple[str, ...],
        *,
        operations: tuple[str, ...] = (),
    ) -> PlanCompilation:
        return PlanCompilation(
            ValidationStatus.UNSUPPORTED, None, issues, proposal=proposal, operations=operations
        )

    @staticmethod
    def _clarify(
        proposal: PlanProposal,
        issues: tuple[str, ...],
        *,
        question: str,
        operations: tuple[str, ...] = (),
    ) -> PlanCompilation:
        return PlanCompilation(
            ValidationStatus.NEEDS_CLARIFICATION,
            None,
            issues,
            clarification_question=question,
            proposal=proposal,
            operations=operations,
        )


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value).strip() or None


__all__ = [
    "P7PlanCompiler",
    "PlanCompilation",
    "operations_from_proposal",
    "proposal_from_operations",
]
