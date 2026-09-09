"""Regression tests for the v3 candidate-plan dialogue boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_agent.application.p7_runtime_config import P7RuntimeConfig
from orca_agent.bootstrap.p7_modules import build_p7_runtime
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p7_conversation import CalculationIntent
from orca_agent.domain.p7_task import TaskPhase, ValidationStatus
from orca_agent.infrastructure.p7_records import P7RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.planning.p7_validator import output_spec_from_intent


def _runtime(tmp_path):
    config = P7RuntimeConfig.for_profile("offline", tmp_path, project_root=Path.cwd())
    return build_p7_runtime(tmp_path, runtime_config=config)


def _message(runtime, conversation_id: str, text: str):
    response = runtime.conversation.message(conversation_id, text)
    task_id = response.get("task_ids", [None])[0]
    assert task_id is not None
    task = runtime.task.get_task(conversation_id, task_id)
    assert task is not None
    return response, task


def test_candidate_planning_selects_exact_scope_and_budget(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]

    _response, opt = _message(
        runtime,
        conversation_id,
        "calculate SMILES CCO charge 0 multiplicity 1 only optimization",
    )
    assert opt.state is TaskPhase.PLAN_READY
    assert opt.request is not None and opt.request.operations == ("opt",)
    assert opt.plan is not None
    assert opt.plan.protocol_id == "p5.opt_only.r2scan3c.4core2048.v1"
    assert opt.plan.resources["nprocs"] == 4
    assert opt.plan.resources["total_memory_mb"] == 2048
    assert opt.plan.resources["maxcore_mb"] == 384

    _response, sp = _message(
        runtime,
        conversation_id,
        "calculate SMILES CCO charge 0 multiplicity 1 independent single point energy",
    )
    assert sp.state is TaskPhase.PLAN_READY
    assert sp.request is not None and sp.request.operations == ("sp",)
    assert sp.plan is not None
    assert sp.plan.protocol_id == "p5.sp_initial.r2scan3c.4core2048.v1"
    assert tuple(node.kind for node in sp.plan.nodes) == ("sp",)
    assert sp.plan.nodes[0].depends_on == ()


def test_output_scope_does_not_silently_choose_an_execution_scope(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response, task = _message(
        runtime,
        conversation_id,
        "calculate SMILES CCO charge 0 multiplicity 1 only show energy",
    )
    assert response["pending_actions"] == []
    assert task.state is TaskPhase.NEEDS_CLARIFICATION
    assert task.plan is None
    assert task.validation is not None
    assert task.validation.status is ValidationStatus.NEEDS_CLARIFICATION
    question = task.validation.clarification_question or ""
    assert "执行" in question and "范围" in question


def test_draft_revision_recompiles_dependencies_and_reports_conflict(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    _response, original = _message(
        runtime,
        conversation_id,
        "calculate SMILES CCO charge 0 multiplicity 1 complete workflow",
    )

    _response, revised = _message(runtime, conversation_id, "remove frequency")
    assert revised.task_id == original.task_id
    assert revised.revision == original.revision + 1
    assert revised.state is TaskPhase.PLAN_READY
    assert revised.plan is not None
    assert revised.plan.protocol_id == "p5.opt_sp.r2scan3c.4core2048.v1"
    assert tuple(node.kind for node in revised.plan.nodes) == ("opt", "sp")
    assert revised.plan.nodes[1].depends_on == (revised.plan.nodes[0].node_id,)

    _response, conflict = _message(
        runtime,
        conversation_id,
        "remove frequency but still require local minimum",
    )
    assert conflict.task_id == original.task_id
    assert conflict.state is TaskPhase.NEEDS_CLARIFICATION
    assert conflict.plan is None
    assert conflict.validation is not None
    assert conflict.validation.status is ValidationStatus.NEEDS_CLARIFICATION
    assert any("local_minimum_support" in issue for issue in conflict.validation.issues)


def test_plan_identity_and_execution_tokens_are_not_interchangeable(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response, task = _message(
        runtime,
        conversation_id,
        "calculate SMILES CCO charge 0 multiplicity 1 only single point",
    )
    plan_token = response["pending_actions"][0]["token"]

    rejected_implicit = runtime.conversation.message(conversation_id, "不接受推荐")
    assert rejected_implicit["accepted"] is True
    assert runtime.task.get_task(conversation_id, task.task_id).state is TaskPhase.PLAN_READY

    runtime.conversation.message(conversation_id, "接受计划")
    identity_pending = runtime.task.get_task(conversation_id, task.task_id)
    assert identity_pending is not None
    assert identity_pending.state is TaskPhase.IDENTITY_PENDING

    repeated_plan = runtime.conversation.message(conversation_id, "接受计划")
    assert repeated_plan["pending_actions"] == []
    assert runtime.task.get_task(conversation_id, task.task_id).state is TaskPhase.IDENTITY_PENDING

    # The explicit plan token remains a valid token-level action, while the
    # natural-language command cannot be replayed against the identity card.
    assert plan_token


def test_next_turn_context_contains_chronological_feedback_records(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    _message(
        runtime,
        conversation_id,
        "calculate SMILES CCO charge 0 multiplicity 1 only optimization",
    )
    runtime.conversation.message(conversation_id, "请解释一下 Opt 和单点的区别")

    with SQLiteUnitOfWork(state_root=tmp_path) as uow:
        uow.begin()
        turns = P7RecordRepository(uow.connection).list_turns(conversation_id)
        uow.commit()
    assert [item.sequence_no for item in turns] == sorted(item.sequence_no for item in turns)
    assert len(turns) >= 2
    facts = thaw_json(turns[-1].context_snapshot.facts)
    feedback = facts.get("planning_feedback")
    assert isinstance(feedback, list) and feedback
    assert feedback[-1]["validation_status"] == "valid"


def test_explicit_null_output_quantities_is_rejected(tmp_path) -> None:
    intent = CalculationIntent(output_spec={"quantities": None})
    with pytest.raises(ValueError, match="quantities"):
        output_spec_from_intent(intent, operations=("sp",))
