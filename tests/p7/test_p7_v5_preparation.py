"""Regression coverage for the v5 preparation and final-confirmation boundary."""

from __future__ import annotations

import json
from pathlib import Path

from orca_agent.application.p7_runtime_config import P7RuntimeConfig
from orca_agent.bootstrap.p7_modules import build_p7_runtime
from orca_agent.domain.p7_preparation import PreparationStatus
from orca_agent.domain.p7_task import TaskPhase
from orca_agent.llm.ports import ModelCallResponse
from orca_agent.orchestration.p7_versions import PROMPT_VERSION_V5, TURN_SCHEMA_V5


class _V5Planner:
    adapter_id = "deepseek_chat"
    intake_schema_version = TURN_SCHEMA_V5
    model = "test-v5"

    def interpret(self, _context):
        return ModelCallResponse(
            provider="test",
            model=self.model,
            content=json.dumps(
                {
                    "schema_version": TURN_SCHEMA_V5,
                    "prompt_version": PROMPT_VERSION_V5,
                    "subrequests": [
                        {
                            "intent": "chemical_calculation",
                            "action": "plan_new",
                            "goal": "优化水分子并返回最终电子能量",
                            "molecule": {"kind": "smiles", "value": "O"},
                            "changes": [],
                            "operations": ["opt"],
                            "steps": [
                                {
                                    "key": "opt",
                                    "operation": "opt",
                                    "input": {"source": "initial"},
                                    "depends_on": [],
                                    "purpose": "优化分子几何",
                                    "why": "用户明确要求优化",
                                }
                            ],
                            "requested_outputs": [
                                "opt_final_electronic_energy",
                                "optimized_geometry",
                            ],
                            "plan_proposal": None,
                            "output_patch": None,
                            "requested_execution": False,
                        }
                    ],
                    "confidence": 0.99,
                    "source": "deepseek_chat",
                }
            ),
        )


class _V5SequencePlanner:
    adapter_id = "deepseek_chat"
    intake_schema_version = TURN_SCHEMA_V5
    model = "test-v5-sequence"

    def __init__(self) -> None:
        self._responses = [self._opt_response(), self._freq_response()]

    @staticmethod
    def _calculation(
        *,
        goal: str,
        operations: list[str],
        steps: list[dict[str, object]],
        outputs: list[str],
        molecule: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "intent": "chemical_calculation",
            "action": "plan_new",
            "goal": goal,
            "molecule": molecule,
            "changes": [],
            "operations": operations,
            "steps": steps,
            "requested_outputs": outputs,
            "plan_proposal": None,
            "output_patch": None,
            "requested_execution": False,
        }

    @classmethod
    def _turn(cls, calculation: dict[str, object]) -> ModelCallResponse:
        return ModelCallResponse(
            provider="test",
            model=cls.model,
            content=json.dumps(
                {
                    "schema_version": TURN_SCHEMA_V5,
                    "prompt_version": PROMPT_VERSION_V5,
                    "subrequests": [calculation],
                    "confidence": 0.99,
                    "source": "deepseek_chat",
                }
            ),
        )

    @classmethod
    def _opt_response(cls) -> ModelCallResponse:
        return cls._turn(
            cls._calculation(
                goal="优化水分子并返回最终电子能量",
                operations=["opt"],
                steps=[
                    {
                        "key": "opt",
                        "operation": "opt",
                        "input": {"source": "initial"},
                        "depends_on": [],
                        "purpose": "优化分子几何",
                        "why": "用户明确要求优化",
                    }
                ],
                outputs=["opt_final_electronic_energy", "optimized_geometry"],
                molecule={"kind": "smiles", "value": "O"},
            )
        )

    @classmethod
    def _freq_response(cls) -> ModelCallResponse:
        return cls._turn(
            cls._calculation(
                goal="在当前任务的已优化几何上补做频率",
                operations=["freq"],
                steps=[
                    {
                        "key": "freq",
                        "operation": "freq",
                        "input": {
                            "source": "history",
                            "task_selector": "current_task",
                            "output": "optimized_geometry",
                        },
                        "depends_on": [],
                        "purpose": "检查频率与局部极小支持",
                        "why": "复用已完成 Opt 的优化几何",
                    }
                ],
                outputs=["vibrational_frequencies", "local_minimum_support"],
                molecule=None,
            )
        )

    def interpret(self, _context):
        if not self._responses:
            raise AssertionError("v5 sequence planner received an unexpected turn")
        return self._responses.pop(0)


def _runtime(tmp_path: Path):
    config = P7RuntimeConfig.for_profile(
        "deepseek_fake",
        tmp_path,
        project_root=tmp_path,
    )
    return build_p7_runtime(tmp_path, runtime_config=config, planner=_V5Planner())


def test_v5_prepares_frozen_snapshot_before_single_final_confirmation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]

    response = runtime.conversation.message(
        conversation_id,
        "calculate SMILES O charge 0 multiplicity 1 optimization and final electronic energy",
    )

    assert response["accepted"] is True, response
    assert [action["action_type"] for action in response["pending_actions"]] == [
        "confirm_execution"
    ], response
    task_id = response["task_ids"][0]
    before = runtime.task.task_view(conversation_id, task_id)
    assert before["preparation"]["status"] == PreparationStatus.READY.value
    assert before["task"]["p4_run_id"]
    assert before["task"]["p5_run_id"] is None
    assert before["execution_authorization"] is None

    replayed = runtime.task.prepare_v5(conversation_id, task_id)
    assert replayed["preparation"]["prepared_id"] == before["preparation"]["prepared_id"]
    assert replayed["task"]["preparation_generation"] == 1

    accepted = runtime.task.accept_action(
        conversation_id,
        response["pending_actions"][0]["token"],
        decision="accept",
    )
    assert accepted["accepted"] is True

    after = runtime.task.task_view(conversation_id, task_id)
    assert after["execution_authorization"]["status"] == "issued"
    assert (
        after["execution_authorization"]["prepared_snapshot_hash"]
        == before["preparation"]["snapshot_hash"]
    )
    assert after["task"]["p5_run_id"]
    assert after["p5"]["binding"]["parent_authorization_credential"]


def test_explicit_missing_history_cannot_fall_back_to_fresh_geometry(tmp_path: Path) -> None:
    config = P7RuntimeConfig.for_profile("offline", tmp_path, project_root=tmp_path)
    runtime = build_p7_runtime(tmp_path, runtime_config=config)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(conversation_id, "优化水分子")
    task = runtime.task.get_task(conversation_id, response["task_ids"][0])
    assert task is not None

    snapshot = runtime.task.preparation.prepare(
        task,
        generation=1,
        source_required=True,
        source_reference="missing-history-task",
    )

    assert snapshot.status is PreparationStatus.FAILED
    assert snapshot.readiness["geometry"] == "source_unavailable"
    assert snapshot.error_code == "source_geometry_unavailable"
    assert snapshot.dependencies["source_required"] is True


def test_v5_historical_opt_preparation_reuses_exact_source_without_reembedding(
    tmp_path: Path, monkeypatch
) -> None:
    config = P7RuntimeConfig.for_profile("deepseek_fake", tmp_path, project_root=tmp_path)
    runtime = build_p7_runtime(
        tmp_path,
        runtime_config=config,
        planner=_V5SequencePlanner(),
    )
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]

    initial = runtime.conversation.message(
        conversation_id,
        "calculate SMILES O charge 0 multiplicity 1 optimization and final electronic energy",
    )
    initial_token = initial["pending_actions"][0]["token"]
    runtime.task.accept_action(conversation_id, initial_token, decision="accept")
    source_task_id = initial["task_ids"][0]
    for _ in range(48):
        progress = runtime.task.progress(conversation_id, max_effects=24, max_seconds=3.0)
        source_view = next(
            item for item in progress["tasks"] if item["task"]["task_id"] == source_task_id
        )
        pending = source_view["pending_actions"]
        if pending:
            runtime.task.accept_action(conversation_id, pending[0]["token"], decision="accept")
            continue
        if source_view["task"]["state"] == TaskPhase.RESULT_READY.value:
            break
    else:
        raise AssertionError("offline v5 Opt source did not reach result_ready")

    def fail_if_reembedded(*_args, **_kwargs):
        raise AssertionError("historical Opt input attempted a fresh RDKit embedding")

    monkeypatch.setattr(
        "orca_agent.application.p5_service.generate_initial_geometry",
        fail_if_reembedded,
    )
    follow_up = runtime.conversation.message(conversation_id, "在刚才优化结构上补做频率")
    assert follow_up["pending_actions"][0]["action_type"] == "confirm_execution"
    follow_up_task_id = follow_up["task_ids"][0]
    prepared = runtime.task.task_view(conversation_id, follow_up_task_id)
    assert prepared["preparation"]["geometry_source"] == "history_opt"
    assert prepared["preparation"]["geometry_hash"]

    runtime.task.accept_action(
        conversation_id,
        follow_up["pending_actions"][0]["token"],
        decision="accept",
    )
    result = runtime.task.task_view(conversation_id, follow_up_task_id)
    assert result["p5"]["binding"]["geometry_draft_hash"] is None
    assert result["p5"]["binding"]["upstream_result_id"]
    assert result["p5"]["binding"]["geometry_hash"] == prepared["preparation"]["geometry_hash"]
