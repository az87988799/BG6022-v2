"""Offline guards for the P7 real-profile closure boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orca_agent.application.p7_runtime_config import P7RuntimeConfig
from orca_agent.bootstrap.p7_modules import build_p7_runtime
from orca_agent.llm.baseline import BaselinePlanner
from orca_agent.llm.ports import ModelCallResponse
from orca_agent.presentation.p7_results import P7ResultPresenter


def test_real_profile_requires_exact_fallback_boundary(tmp_path) -> None:
    config = P7RuntimeConfig.for_profile("real", tmp_path, project_root=tmp_path)
    assert config.model == "deepseek-v4-flash"

    with pytest.raises(ValueError, match="fallback=none"):
        build_p7_runtime(
            tmp_path,
            runtime_config=config,
            fallback="baseline",
        )


def test_profile_rejects_conflicting_project_planner(tmp_path) -> None:
    (tmp_path / ".env").write_text("BG6022_P7_PLANNER=baseline\n", encoding="utf-8")

    with pytest.raises(ValueError, match="conflicts"):
        P7RuntimeConfig.for_profile("real", tmp_path, project_root=tmp_path)


def test_real_profile_without_orca_does_not_issue_executable_token(tmp_path) -> None:
    config = P7RuntimeConfig.for_profile("real", tmp_path, project_root=tmp_path)
    runtime = build_p7_runtime(
        tmp_path,
        runtime_config=config,
        planner=BaselinePlanner(),
    )
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]

    response = runtime.conversation.message(
        conversation_id,
        "calculate SMILES CCO charge 0 multiplicity 1 independent single point energy",
    )

    assert response["accepted"] is True
    assert response["pending_actions"] == []
    view = response["responses"][0]["payload"]
    assert view["execution_blocked"]["code"] == "execution_not_ready"
    assert view["task"]["state"] == "plan_ready"


class _LegacyDeepSeekPlanner:
    adapter_id = "deepseek_chat"
    model = "test-model"

    def interpret(self, _context):
        return ModelCallResponse(
            provider="deepseek",
            model=self.model,
            content=json.dumps(
                {
                    "schema_version": "p7.turn.v1",
                    "prompt_version": "p7.intake-plan.v1",
                    "subrequests": [
                        {
                            "intent": "general_qa",
                            "question": "hello",
                            "answer_draft": "hello",
                        }
                    ],
                }
            ),
        )


class _TaskContextDeepSeekPlanner:
    adapter_id = "deepseek_chat"
    model = "test-model"

    def interpret(self, _context):
        return ModelCallResponse(
            provider="deepseek",
            model=self.model,
            content=json.dumps(
                {
                    "schema_version": "p7.turn.v2",
                    "prompt_version": "p7.intake-plan.v2",
                    "subrequests": [
                        {
                            "intent": "chemistry_qa",
                            "question": "当前任务的能量是多少？",
                            "answer_draft": "模型不应直接作为结果的虚构能量 999",
                            "requires_task_context": True,
                            "cited_fact_aliases": [],
                        }
                    ],
                }
            ),
        )


def test_live_deepseek_v1_response_is_rejected_without_fallback(tmp_path) -> None:
    config = P7RuntimeConfig.for_profile("deepseek_fake", tmp_path, project_root=tmp_path)
    runtime = build_p7_runtime(
        tmp_path,
        runtime_config=config,
        planner=_LegacyDeepSeekPlanner(),
    )
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]

    response = runtime.conversation.message(conversation_id, "hello")

    assert response["accepted"] is False
    assert response["code"] == "invalid_model_output"


def test_task_context_qa_uses_program_data_not_model_draft(tmp_path) -> None:
    config = P7RuntimeConfig.for_profile("deepseek_fake", tmp_path, project_root=tmp_path)
    runtime = build_p7_runtime(
        tmp_path,
        runtime_config=config,
        planner=_TaskContextDeepSeekPlanner(),
    )
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]

    response = runtime.conversation.message(conversation_id, "查当前任务能量")

    assert response["accepted"] is True
    item = response["responses"][0]
    assert "999" not in item["text"]
    assert item["payload"]["answer_source"] == "program"
    assert runtime.task.list_tasks(conversation_id) == ()


def test_result_source_ambiguity_is_reported_explicitly() -> None:
    first = SimpleNamespace(primitive="sp", result_id="sp-1")
    second = SimpleNamespace(primitive="sp", result_id="sp-2")

    assert P7ResultPresenter._find_source("sp", None, (), (first, second)) is None
    reason = P7ResultPresenter._source_selection_reason("sp", None, (), (first, second))
    assert "多个" in reason
    assert "selector" in reason
