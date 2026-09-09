"""Focused offline acceptance tests for the P7 conversation vertical slice."""

from __future__ import annotations

import sqlite3

import pytest

from orca_agent.bootstrap.p7_modules import build_p7_runtime
from orca_agent.domain.p7_conversation import IntentKind
from orca_agent.llm.baseline import BaselinePlanner
from orca_agent.llm.ports import strict_json_loads


def _complete_fake_task(runtime, conversation_id: str) -> dict[str, object]:
    proposal = runtime.conversation.message(
        conversation_id,
        "calculate SMILES CCO charge 0 multiplicity 1 independent single point energy",
    )
    plan_token = proposal["pending_actions"][0]["token"]
    runtime.task.accept_action(conversation_id, plan_token, decision="accept")
    for _ in range(16):
        progress = runtime.task.progress(conversation_id, max_effects=24, max_seconds=3.0)
        task_view = progress["tasks"][0]
        pending = task_view["pending_actions"]
        if pending:
            runtime.task.accept_action(conversation_id, pending[0]["token"], decision="accept")
            continue
        if task_view["task"]["state"] == "result_ready":
            return task_view
    raise AssertionError("fake P7 task did not reach result_ready")


def test_baseline_routes_four_intents_and_preserves_mixed_order() -> None:
    planner = BaselinePlanner()
    mixed = planner.interpret_text(
        "解释优化和单点的区别，然后 calculate SMILES CCO charge 0 multiplicity 1"
    )
    assert [item.intent for item in mixed.subrequests] == [
        IntentKind.CHEMISTRY_QA,
        IntentKind.CHEMICAL_CALCULATION,
    ]
    assert planner.interpret_text("hello").subrequests[0].intent is IntentKind.GENERAL_QA
    assert (
        planner.interpret_text("show status of the current task").subrequests[0].intent
        is IntentKind.CONTEXT_QUERY
    )


def test_qa_does_not_create_a_calculation_task(tmp_path) -> None:
    runtime = build_p7_runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(conversation_id, "优化和单点有什么区别？")
    assert response["accepted"] is True
    assert response["responses"][0]["intent"] == IntentKind.CHEMISTRY_QA.value
    assert runtime.task.list_tasks(conversation_id) == ()


def test_calculation_waits_for_plan_acceptance_and_implicit_ack_is_bound(tmp_path) -> None:
    runtime = build_p7_runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    proposal = runtime.conversation.message(
        conversation_id,
        "calculate SMILES CCO charge 0 multiplicity 1 independent single point energy",
    )
    task_id = proposal["task_ids"][0]
    assert proposal["pending_actions"][0]["action_type"] == "accept_plan"
    assert runtime.task.get_task(conversation_id, task_id).state.value == "plan_ready"
    accepted = runtime.conversation.message(conversation_id, "接受计划")
    assert accepted["responses"][0]["payload"]["task"]["state"] == "identity_pending"
    task = runtime.task.get_task(conversation_id, task_id)
    assert task is not None and task.p5_run_id is None


def test_full_fake_chain_delivery_query_and_display_change(tmp_path) -> None:
    runtime = build_p7_runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    view = _complete_fake_task(runtime, conversation_id)
    task_id = view["task"]["task_id"]
    delivery = view["delivery"]
    assert delivery["overall_status"] == "fulfilled"
    fulfillment = delivery["fulfillment"][0]
    assert fulfillment["kind"] == "independent_sp_electronic_energy"
    assert fulfillment["raw_value_token"]
    original_hash = delivery["delivery_hash"]

    query = runtime.query.query(
        conversation_id,
        task_id=task_id,
        request={"kind": "result", "output_spec": {"layout": "table"}},
    )
    assert query["delivery"]["output_spec"]["layout"] == "prose"
    assert query["view"]["output_spec"]["layout"] == "table"
    assert query["view"]["source_delivery_id"] == delivery["delivery_id"]
    assert query["view"]["source_delivery_hash"] == original_hash
    assert query["delivery"]["delivery_hash"] == original_hash
    assert runtime.query.verify(conversation_id, task_id=task_id)["valid"] is True


def test_ethanol_recommendation_requires_and_records_acceptance(tmp_path) -> None:
    runtime = build_p7_runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    clarification = runtime.conversation.message(conversation_id, "calculate ethanol energy")
    task_id = clarification["task_ids"][0]
    request = clarification["responses"][0]["payload"]["task"]["request"]
    assert clarification["responses"][0]["payload"]["task"]["state"] == "needs_clarification"
    assert request["charge"]["source"] == "policy_recommended"
    assert request["multiplicity"]["source"] == "policy_recommended"

    accepted = runtime.conversation.message(conversation_id, "接受推荐")
    task = accepted["responses"][0]["payload"]["task"]
    assert task["task_id"] == task_id
    assert task["state"] == "plan_ready"
    assert task["request"]["charge"]["source"] == "user_accepted_recommendation"
    assert task["request"]["multiplicity"]["source"] == "user_accepted_recommendation"


@pytest.mark.parametrize(
    "text",
    [
        "calculate SMILES CCO charge 0 multiplicity 1 only sp",
        "calculate SMILES CCO charge 0 multiplicity 1 Gibbs free energy",
        "calculate SMILES CCO charge 0 multiplicity 1 solvent energy",
        "calculate SMILES CCO charge 0 multiplicity 1 B3LYP energy",
        "calculate SMILES O=O charge 0 multiplicity 1 energy",
    ],
)
def test_unsupported_boundary_never_creates_executable_plan(tmp_path, text: str) -> None:
    runtime = build_p7_runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(conversation_id, text)
    task = response["responses"][0]["payload"]["task"]
    assert task["state"] == "ended_without_result"
    assert response["pending_actions"] == []
    assert task["p4_run_id"] is None
    assert task["p5_run_id"] is None


def test_fake_model_attempt_receipt_is_durable_and_query_needs_no_model(tmp_path) -> None:
    runtime = build_p7_runtime(tmp_path, planner_name="fake")
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(conversation_id, "hello")
    assert response["accepted"] is True
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        attempt = connection.execute(
            "SELECT status, slot FROM p7_model_attempts WHERE turn_id=?",
            (response["turn_id"],),
        ).fetchone()
        receipt = connection.execute(
            "SELECT outcome FROM p7_model_receipts WHERE attempt_id=("
            "SELECT attempt_id FROM p7_model_attempts WHERE turn_id=?"
            ")",
            (response["turn_id"],),
        ).fetchone()
    assert attempt == ("receipted", "interpret")
    assert receipt == ("success",)
    assert runtime.conversation.get_state(conversation_id).model_calls == 1

    reopened = build_p7_runtime(tmp_path)
    query = reopened.query.query(
        conversation_id, request=strict_json_loads('{"kind":"conversation"}')
    )
    assert query["conversation_id"] == conversation_id
    assert query["tasks"] == []


def test_strict_query_rejects_duplicate_json_keys() -> None:
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        strict_json_loads('{"kind":"result","kind":"conversation"}')
