"""Regression tests for the P7 repair and direct-chat contract."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from orca_agent.bootstrap.p7_modules import build_p7_runtime
from orca_agent.interfaces.p7_chat import P7ChatDriver
from orca_agent.llm.baseline import BaselinePlanner
from orca_agent.llm.deepseek_chat import _runtime_environment, _system_prompt
from orca_agent.llm.ports import ModelCallResponse


def _proposal(runtime, conversation_id: str) -> tuple[str, str]:
    response = runtime.conversation.message(
        conversation_id,
        "calculate SMILES CCO charge 0 multiplicity 1 independent single point energy",
    )
    return response["task_ids"][0], response["pending_actions"][0]["token"]


def _identity_token(runtime, conversation_id: str, plan_token: str) -> str:
    runtime.task.accept_action(conversation_id, plan_token, decision="accept")
    for _ in range(8):
        progress = runtime.task.progress(conversation_id, max_effects=8, max_seconds=2.0)
        pending = progress["tasks"][0]["pending_actions"]
        if pending:
            return pending[0]["token"]
    raise AssertionError("identity token was not created")


def test_p4_handoff_retry_reuses_command_after_response_loss(tmp_path) -> None:
    runtime = build_p7_runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    task_id, plan_token = _proposal(runtime, conversation_id)
    original = runtime.task.p4.start
    command_ids: list[str] = []

    def crash(command):
        command_ids.append(str(command.command_id))
        raise RuntimeError("simulated response loss")

    runtime.task.p4.start = crash
    try:
        runtime.task.accept_action(conversation_id, plan_token, decision="accept")
    except RuntimeError:
        pass
    finally:
        runtime.task.p4.start = original

    replayed = runtime.task.accept_action(conversation_id, plan_token, decision="accept")
    assert replayed["replayed"] is True
    assert replayed["task"]["task_id"] == task_id
    assert command_ids

    with sqlite3.connect(Path(tmp_path) / "state.sqlite3") as connection:
        rows = connection.execute(
            "SELECT command_id, status FROM p7_handoffs WHERE task_id=? AND target='p4'",
            (task_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == command_ids[0]
    assert rows[0][1] == "linked"


def test_confirm_identity_retry_reuses_consumed_payload(tmp_path) -> None:
    runtime = build_p7_runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    task_id, plan_token = _proposal(runtime, conversation_id)
    identity_token = _identity_token(runtime, conversation_id, plan_token)
    original = runtime.task.p4.confirm
    command_ids: list[str] = []

    def crash(command):
        command_ids.append(str(command.command_id))
        raise RuntimeError("simulated confirm response loss")

    runtime.task.p4.confirm = crash
    try:
        runtime.task.accept_action(conversation_id, identity_token, decision="accept")
    except RuntimeError:
        pass
    finally:
        runtime.task.p4.confirm = original

    replayed = runtime.task.accept_action(conversation_id, identity_token, decision="accept")
    assert replayed["replayed"] is True
    assert replayed["task"]["task_id"] == task_id
    assert replayed["task"]["state"] == "execution_pending"
    assert len(command_ids) == 1

    with sqlite3.connect(Path(tmp_path) / "state.sqlite3") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM p7_pending_actions "
            "WHERE task_id=? AND action_type='confirm_identity'",
            (task_id,),
        ).fetchone()[0]
    assert count == 1


def test_cancelled_turn_cannot_publish_late_response_or_task(tmp_path) -> None:
    runtime = build_p7_runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    _state, turn, context = runtime.conversation._receive_turn(
        conversation_id, "calculate SMILES CCO charge 0 multiplicity 1"
    )
    interrupted = runtime.conversation.interrupt_turn(conversation_id, turn.turn_id)
    assert interrupted["code"] == "turn_cancelled"
    late = runtime.conversation._finish_completed(
        conversation_id,
        turn,
        BaselinePlanner().interpret(context),
        {"text": "late", "responses": [], "task_ids": []},
        source=BaselinePlanner().interpret(context).source,
    )
    assert late["code"] == "turn_cancelled"
    assert runtime.task.list_tasks(conversation_id) == ()


class _RepairPlanner:
    adapter_id = "fake"
    model = "repair-test"

    def __init__(self) -> None:
        self.repair_calls = 0

    def interpret(self, _context):
        return ModelCallResponse(provider="fake", model=self.model, content='{"invalid":true}')

    def format_repair(self, _context, _invalid):
        self.repair_calls += 1
        return ModelCallResponse(
            provider="fake",
            model=self.model,
            content=json.dumps(
                {
                    "schema_version": "p7.turn.v1",
                    "prompt_version": "p7.intake-plan.v1",
                    "subrequests": [
                        {"intent": "general_qa", "question": "hello", "answer_draft": "hi"}
                    ],
                }
            ),
        )


def test_invalid_model_output_uses_one_durable_format_repair(tmp_path) -> None:
    planner = _RepairPlanner()
    runtime = build_p7_runtime(tmp_path, planner=planner)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(conversation_id, "hello")
    assert response["accepted"] is True
    assert planner.repair_calls == 1
    with sqlite3.connect(Path(tmp_path) / "state.sqlite3") as connection:
        slots = connection.execute(
            "SELECT slot, status FROM p7_model_attempts WHERE turn_id=? ORDER BY slot",
            (response["turn_id"],),
        ).fetchall()
    assert slots == [("format_repair", "receipted"), ("interpret", "receipted")]


def test_context_queries_keep_task_and_display_records_separate(tmp_path) -> None:
    runtime = build_p7_runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    task_id, plan_token = _proposal(runtime, conversation_id)
    runtime.task.accept_action(conversation_id, plan_token, decision="accept")

    status = runtime.conversation.message(conversation_id, "刚才算完了吗？")
    assert status["responses"][0]["intent"] == "context_query"
    assert status["responses"][0]["payload"]["selected_task_id"] == task_id
    assert len(runtime.task.list_tasks(conversation_id)) == 1

    display = runtime.conversation.message(conversation_id, "只显示独立单点能量，保留六位小数")
    assert display["responses"][0]["intent"] == "context_query"
    assert display["responses"][0]["payload"]["selected_task_id"] == task_id
    assert len(runtime.task.list_tasks(conversation_id)) == 1


def test_env_loader_accepts_utf8_bom_and_process_wins(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "\ufeffDEEPSEEK_API_KEY=file-key\nBG6022_P7_MODEL=file-model\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("BG6022_P7_MODEL", raising=False)
    loaded = _runtime_environment(None)
    assert loaded["DEEPSEEK_API_KEY"] == "file-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "process-key")
    assert _runtime_environment(None)["DEEPSEEK_API_KEY"] == "process-key"
    assert "Published JSON Schema" in _system_prompt()


def test_shared_driver_requires_explicit_token_for_generic_ack(tmp_path) -> None:
    runtime = build_p7_runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    driver = P7ChatDriver(runtime, conversation_id, input_stream=None)
    proposal = driver.handle_text("calculate SMILES CCO charge 0 multiplicity 1")
    assert proposal["accepted"] is True
    blocked = driver.handle_text("好")
    assert blocked["code"] == "explicit_token_required"
    assert runtime.task.list_tasks(conversation_id)[0].state.value == "plan_ready"
