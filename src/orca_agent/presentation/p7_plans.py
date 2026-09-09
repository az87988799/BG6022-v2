"""Common P7 draft/execution display model.

The display is a projection only.  It contains no approval token and never
serves as an authorization object; its content hash binds the scientific
fields shown to the corresponding task revision.
"""

from __future__ import annotations

from collections.abc import Mapping

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p7_task import TaskPhase, TaskRecord
from orca_agent.planning.p7_parameter_policy import default_parameter_policy

PLAN_DISPLAY_VERSION = "p7.plan-display.v1"


def _parameter(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "value": thaw_json(getattr(value, "value", None)),
        "source": getattr(getattr(value, "source", None), "value", None),
        "source_reference": getattr(value, "source_reference", None),
        "source_fragment": getattr(value, "source_fragment", None),
        "structure_hash": getattr(value, "structure_hash", None),
        "rule_version": getattr(value, "rule_version", None),
        "recommendation_accepted": getattr(value, "recommendation_accepted", False),
    }


def _request_projection(task: TaskRecord) -> dict[str, object]:
    request = task.request
    if request is None:
        return {
            "kind": None,
            "original": None,
            "normalized_candidate": None,
            "status": "missing",
        }
    return {
        "kind": None if request.molecule_kind is None else request.molecule_kind.value,
        "original": request.molecule_value,
        "normalized_candidate": None,
        "status": "provided" if request.molecule_value else "missing",
    }


def _science_projection(task: TaskRecord) -> dict[str, object]:
    request = task.request
    plan = task.plan
    validation = task.validation
    return {
        "molecule": _request_projection(task),
        "parameters": {
            "method": None if request is None else _parameter(request.method),
            "environment": None if request is None else _parameter(request.environment),
            "charge": None if request is None else _parameter(request.charge),
            "multiplicity": None if request is None else _parameter(request.multiplicity),
        },
        "scope": [] if request is None else list(request.operations),
        "nodes": []
        if plan is None
        else [item.model_dump(mode="json") for item in plan.nodes],
        "resources": None if plan is None else thaw_json(plan.resources),
        "outputs": None
        if request is None
        else request.output_spec.model_dump(mode="json"),
        "validation": None
        if validation is None
        else validation.model_dump(mode="json"),
    }


def _base_display(task: TaskRecord, planning_record: object | None) -> dict[str, object]:
    constraints = (
        {}
        if planning_record is None
        else thaw_json(getattr(planning_record, "normalized_constraints", {}))
    )
    if not isinstance(constraints, dict):
        constraints = {}
    science = _science_projection(task)
    precheck = constraints.get("precheck")
    if isinstance(precheck, Mapping):
        molecule = science["molecule"]
        if isinstance(molecule, dict):
            molecule["normalized_candidate"] = {
                "canonical_isomeric_smiles": precheck.get("canonical_isomeric_smiles"),
                "molecular_formula": precheck.get("molecular_formula"),
                "formal_charge": precheck.get("formal_charge"),
                "structure_hash": precheck.get("structure_hash"),
            }
            molecule["status"] = str(precheck.get("status", molecule.get("status")))
    request = task.request
    if (
        request is not None
        and request.molecule_kind is not None
        and request.molecule_value is not None
        and science["molecule"].get("normalized_candidate") is None
    ):
        known = default_parameter_policy().molecule_for(
            request.molecule_kind, request.molecule_value
        )
        if known is not None:
            science["molecule"]["normalized_candidate"] = {
                "canonical_isomeric_smiles": known.canonical_smiles,
                "molecular_formula": known.molecular_formula,
                "formal_charge": known.charge,
                "rule_id": known.rule_id,
            }
    policy = constraints.get("policy_snapshot")
    changes = constraints.get("draft_changes", [])
    unresolved: list[object] = []
    if task.validation is not None:
        unresolved.extend(task.validation.missing_fields)
        unresolved.extend(task.validation.issues)
        unresolved.extend(task.validation.unsupported_requests)
    allowed: list[str] = []
    if task.state in {TaskPhase.DRAFT, TaskPhase.NEEDS_CLARIFICATION, TaskPhase.PLAN_READY}:
        allowed.append("revise_draft")
    if task.state is TaskPhase.PLAN_READY and task.accepted_plan_hash is None:
        allowed.append("accept_plan")
    if task.state is TaskPhase.IDENTITY_PENDING:
        allowed.append("confirm_identity")
    if task.state is TaskPhase.EXECUTION_PENDING:
        allowed.append("approve_execution")
    display = {
        "display_version": PLAN_DISPLAY_VERSION,
        "task_id": task.task_id,
        "alias": task.alias,
        "revision": task.revision,
        "state": task.state.value,
        "draft_semantics_version": constraints.get("draft_semantics_version", "legacy"),
        "science": science,
        "changes": changes if isinstance(changes, list) else [],
        "identity": {
            "original": science["molecule"].get("original"),
            "normalized_candidate": science["molecule"].get("normalized_candidate"),
            "status": science["molecule"].get("status"),
            "raw_fragment": constraints.get("raw_molecule_fragment"),
        },
        "provenance": {
            "planning_record_id": None
            if planning_record is None
            else getattr(planning_record, "proposal_id", None),
            "planning_record_hash": None
            if planning_record is None
            else getattr(planning_record, "record_hash", None),
            "policy_version": (
                None if not isinstance(policy, dict) else policy.get("policy_version")
            ),
            "policy_snapshot_hash": None
            if not isinstance(policy, dict)
            else policy.get("content_hash"),
            "model_attempt_id": None
            if planning_record is None
            else getattr(planning_record, "model_attempt_id", None),
        },
        "unresolved": unresolved,
        "allowed_actions": allowed,
    }
    hash_input = {key: value for key, value in display.items() if key not in {"provenance"}}
    display["content_hash"] = sha256_hex(hash_input)
    display["rendered_text"] = render_plan_display(display)
    return display


def build_draft_display(
    task: TaskRecord, planning_record: object | None = None
) -> dict[str, object]:
    """Build the immutable scientific projection for a draft/revision."""

    return _base_display(task, planning_record)


def build_execution_display(
    task: TaskRecord,
    *,
    planning_record: object | None = None,
    p4: Mapping[str, object] | None = None,
    p5: Mapping[str, object] | None = None,
    p6: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the same display model with downstream state projections."""

    display = _base_display(task, planning_record)
    display["downstream"] = {"p4": p4, "p5": p5, "p6": p6}
    hash_input = {
        key: value
        for key, value in display.items()
        if key not in {"provenance", "downstream", "content_hash", "rendered_text"}
    }
    display["content_hash"] = sha256_hex(hash_input)
    display["rendered_text"] = render_plan_display(display)
    return display


def render_plan_display(display: Mapping[str, object]) -> str:
    """Render a concise Chinese card from the shared projection."""

    science = display.get("science")
    if not isinstance(science, Mapping):
        return "当前没有可展示的计算草稿。"
    molecule = science.get("molecule")
    molecule_text = "未确定分子"
    if isinstance(molecule, Mapping) and molecule.get("original"):
        molecule_text = str(molecule["original"])
    scope = science.get("scope")
    scope_text = " → ".join(str(item) for item in scope) if isinstance(scope, list) else "未确定"
    parameters = science.get("parameters")
    parameter_text: list[str] = []
    if isinstance(parameters, Mapping):
        for name, value in parameters.items():
            if isinstance(value, Mapping) and value.get("value") is not None:
                source = value.get("source") or "unknown"
                parameter_text.append(f"{name}={value['value']}（{source}）")
    state = str(display.get("state", "unknown"))
    unresolved = display.get("unresolved")
    suffix = ""
    if isinstance(unresolved, list) and unresolved:
        suffix = "；待补充：" + "、".join(str(item) for item in unresolved[:3])
    return (
        f"分子：{molecule_text}；范围：{scope_text}；"
        f"参数：{'，'.join(parameter_text) or '未确定'}；状态：{state}{suffix}"
    )


__all__ = [
    "PLAN_DISPLAY_VERSION",
    "build_draft_display",
    "build_execution_display",
    "render_plan_display",
]
