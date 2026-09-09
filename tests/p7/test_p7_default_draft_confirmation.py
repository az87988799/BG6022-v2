"""Offline coverage for the P7 v4 default-draft and confirmation contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_agent.application.p7_runtime_config import P7RuntimeConfig
from orca_agent.bootstrap.p7_modules import build_p7_runtime
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p7_conversation import ParameterSource
from orca_agent.domain.p7_intake import (
    CalculationIntentV4,
    FieldChangeV4,
    ParameterEvidenceV4,
    TurnInterpretationV4,
)
from orca_agent.domain.p7_task import TaskPhase, ValidationStatus
from orca_agent.llm.baseline import BaselinePlanner
from orca_agent.llm.deepseek_chat import DeepSeekChatAdapter
from orca_agent.llm.ports import ModelCallResponse
from orca_agent.orchestration.p7_versions import PROMPT_VERSION_V4, TURN_SCHEMA_V4
from orca_agent.planning.p7_draft import resolve_draft
from orca_agent.planning.p7_parameter_policy import default_parameter_policy


def _runtime(tmp_path):
    config = P7RuntimeConfig.for_profile("offline", tmp_path, project_root=Path.cwd())
    return build_p7_runtime(tmp_path, runtime_config=config)


def _task(runtime, conversation_id: str, response: dict[str, object]):
    task_ids = response.get("task_ids", [])
    assert isinstance(task_ids, list) and task_ids
    task = runtime.task.get_task(conversation_id, str(task_ids[0]))
    assert task is not None
    return task


def _complete_opt(runtime, conversation_id: str):
    proposal = runtime.conversation.message(conversation_id, "优化水分子")
    task = _task(runtime, conversation_id, proposal)
    runtime.task.accept_action(
        conversation_id, proposal["pending_actions"][0]["token"], decision="accept"
    )
    for _ in range(32):
        progress = runtime.task.progress(conversation_id, max_effects=24, max_seconds=3.0)
        view = next(item for item in progress["tasks"] if item["task"]["task_id"] == task.task_id)
        pending = view["pending_actions"]
        if pending:
            runtime.task.accept_action(conversation_id, pending[0]["token"], decision="accept")
            continue
        if view["task"]["state"] == TaskPhase.RESULT_READY.value:
            return task.task_id
    raise AssertionError("offline Opt task did not reach result_ready")


def test_v4_schema_is_valid_strict_and_versioned() -> None:
    schema_path = Path("src/orca_agent/resources/p7/intake.v4.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    value = {
        "schema_version": TURN_SCHEMA_V4,
        "prompt_version": PROMPT_VERSION_V4,
        "subrequests": [
            {
                "intent": "chemical_calculation",
                "action": "revise_draft",
                "task_alias": "当前任务",
                "changes": [
                    {
                        "field": "environment",
                        "op": "set",
                        "value": "gas",
                        "evidence": {"quote": "气相"},
                    }
                ],
                "plan_proposal": None,
                "output_patch": None,
                "requested_execution": False,
            }
        ],
    }
    interpretation = TurnInterpretationV4.model_validate_json(json.dumps(value), strict=True)
    assert interpretation.subrequests[0].changes[0].field == "environment"
    with pytest.raises(ValueError):
        TurnInterpretationV4.model_validate({**value, "unexpected": True}, strict=True)
    with pytest.raises(ValueError, match="integer"):
        FieldChangeV4(
            field="charge",
            value=True,
            evidence=ParameterEvidenceV4(quote="电荷 True"),
        )


def test_default_water_is_complete_opt_draft_without_execution(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(conversation_id, "优化水分子")
    task = _task(runtime, conversation_id, response)

    assert task.state is TaskPhase.PLAN_READY
    assert task.request is not None
    assert task.request.operations == ("opt",)
    assert task.request.method is not None
    assert task.request.method.value == "r2SCAN-3c"
    assert task.request.method.source is ParameterSource.POLICY_RECOMMENDED
    assert task.request.environment is not None
    assert task.request.environment.value == "gas"
    assert task.request.environment.source is ParameterSource.POLICY_RECOMMENDED
    assert task.request.charge is not None and task.request.charge.value == 0
    assert task.request.multiplicity is not None and task.request.multiplicity.value == 1
    assert task.plan is not None
    assert task.plan.protocol_id == "p5.opt_only.r2scan3c.4core2048.v1"
    assert task.plan.resources["nprocs"] == 4
    assert task.plan.resources["total_memory_mb"] == 2048
    assert task.plan.resources["maxcore_mb"] == 384
    assert tuple(item.kind.value for item in task.request.output_spec.quantities) == (
        "opt_final_electronic_energy",
        "optimized_geometry",
    )
    assert task.p4_run_id is None and task.p5_run_id is None
    assert response["pending_actions"][0]["action_type"] == "accept_plan"
    display = runtime.task.task_view(conversation_id, task.task_id)["plan_display"]
    assert display["draft_semantics_version"] == "p7.draft.v4"
    assert display["science"]["scope"] == ["opt"]
    assert display["science"]["parameters"]["method"]["source"] == "policy_recommended"


@pytest.mark.parametrize("text", ["water", "水", "水分子"])
def test_registered_water_aliases_keep_the_raw_fragment(tmp_path, text: str) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(conversation_id, f"优化{text}")
    task = _task(runtime, conversation_id, response)
    assert task.request is not None
    assert task.request.molecule_value == "water"
    record = runtime.task.planning_record(task)
    assert record is not None
    constraints = thaw_json(record.normalized_constraints)
    assert constraints["raw_molecule_fragment"] == text
    view = runtime.task.task_view(conversation_id, task.task_id)
    assert view["plan_display"]["identity"]["raw_fragment"] == text


def test_solvent_water_word_is_not_misread_as_the_target(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(conversation_id, "计算苯在水溶液中")
    task = _task(runtime, conversation_id, response)
    assert task.request is not None
    assert task.request.molecule_value == "benzene"
    assert task.request.environment is not None
    assert task.request.environment.value == "solvent"
    assert task.validation is not None
    assert task.validation.status is ValidationStatus.UNSUPPORTED
    assert task.state is TaskPhase.NEEDS_CLARIFICATION
    assert response["pending_actions"] == []


def test_method_typo_is_retained_until_explicit_correction(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(
        conversation_id, "优化水分子，方法R2san3c，环境气相，电荷0"
    )
    task = _task(runtime, conversation_id, response)
    assert task.state is TaskPhase.NEEDS_CLARIFICATION
    assert task.request is not None and task.request.method is not None
    assert task.request.method.value == "R2san3c"
    assert task.request.environment is not None
    assert task.request.environment.source is ParameterSource.USER_EXPLICIT
    assert task.request.charge is not None and task.request.charge.value == 0
    assert "是否指 r2SCAN-3c" in response["text"]

    corrected = runtime.conversation.message(conversation_id, "当前任务方法是 r2SCAN-3c")
    revised = runtime.task.get_task(conversation_id, task.task_id)
    assert revised is not None
    assert revised.revision == 2
    assert revised.state is TaskPhase.PLAN_READY
    assert revised.request is not None and revised.request.method is not None
    assert revised.request.method.value == "r2SCAN-3c"
    assert revised.request.method.source is ParameterSource.USER_EXPLICIT
    assert corrected["task_ids"] == [task.task_id]


def test_explicitly_repeating_a_policy_default_upgrades_provenance(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    initial = runtime.conversation.message(conversation_id, "优化水分子")
    task = _task(runtime, conversation_id, initial)
    revised_response = runtime.conversation.message(
        conversation_id, "当前任务方法是 r2SCAN-3c"
    )
    revised = runtime.task.get_task(conversation_id, task.task_id)
    assert revised is not None and revised.revision == 2
    assert revised.request is not None and revised.request.method is not None
    assert revised.request.method.source is ParameterSource.USER_EXPLICIT
    assert revised.request.environment is not None
    assert revised.request.environment.source is ParameterSource.POLICY_RECOMMENDED
    assert revised_response["pending_actions"]


def test_sparse_merge_preserves_unmentioned_values_and_clears_old_environment_issue(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    initial = runtime.conversation.message(conversation_id, "优化水分子")
    task = _task(runtime, conversation_id, initial)
    solvent = runtime.conversation.message(conversation_id, "当前任务改为水溶液")
    task = runtime.task.get_task(conversation_id, task.task_id)
    assert task is not None and task.request is not None
    assert solvent["pending_actions"] == []

    patch = CalculationIntentV4(
        action="revise_draft",
        task_alias="当前任务",
        changes=(
            FieldChangeV4(
                field="environment",
                value="gas",
                evidence=ParameterEvidenceV4(quote="气相"),
            ),
        ),
    )
    record = runtime.task.planning_record(task)
    resolution = resolve_draft(
        current_request=task.request,
        current_planning_record=record,
        user_patch=patch,
        current_message="当前任务改为气相；其他参数保持原样",
        turn_reference="turn_test_sparse",
        default_policy=default_parameter_policy(),
        capability_catalog=runtime.task.catalog,
    )
    assert [item.field for item in resolution.changes] == ["environment"]
    assert resolution.request.method is task.request.method
    assert resolution.request.charge is task.request.charge
    assert resolution.request.multiplicity is task.request.multiplicity
    assert resolution.request.environment is not task.request.environment
    assert resolution.request.environment is not None
    assert resolution.request.environment.value == "gas"
    assert not resolution.issues


@pytest.mark.parametrize(
    ("message", "quote", "expected_issue"),
    [
        (
            "当前任务方法改为 r2SCAN-3c，气相",
            "气相",
            "change_evidence_field_mismatch:method",
        ),
        (
            "当前任务不使用气相，电荷0",
            "气相",
            "negative_change_evidence:environment",
        ),
    ],
)
def test_unreliable_current_evidence_forces_an_editable_revision(
    tmp_path,
    message: str,
    quote: str,
    expected_issue: str,
) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    initial = runtime.conversation.message(conversation_id, "优化水分子")
    task = _task(runtime, conversation_id, initial)
    assert task.request is not None
    patch = CalculationIntentV4(
        action="revise_draft",
        task_alias="当前任务",
        changes=(
            FieldChangeV4(
                field="method" if "method" in expected_issue else "environment",
                value="r2SCAN-3c" if "method" in expected_issue else "gas",
                evidence=ParameterEvidenceV4(quote=quote),
            ),
        ),
    )
    resolution = resolve_draft(
        current_request=task.request,
        current_planning_record=runtime.task.planning_record(task),
        user_patch=patch,
        current_message=message,
        turn_reference="turn_test_unreliable",
        default_policy=default_parameter_policy(),
        capability_catalog=runtime.task.catalog,
    )
    assert resolution.changed is True
    assert expected_issue in resolution.issues
    assert resolution.request.method is not None
    assert resolution.request.method.source is ParameterSource.POLICY_RECOMMENDED


def test_reset_only_named_method_and_keeps_other_fields(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    initial = runtime.conversation.message(conversation_id, "优化水分子")
    task = _task(runtime, conversation_id, initial)
    original_environment = task.request.environment
    revised_response = runtime.conversation.message(conversation_id, "当前任务恢复默认方法")
    revised = runtime.task.get_task(conversation_id, task.task_id)
    assert revised is not None and revised.revision == 2
    assert revised.request is not None
    assert revised.request.method is not None
    assert revised.request.method.source is ParameterSource.POLICY_RECOMMENDED
    assert revised.request.environment == original_environment
    record = runtime.task.planning_record(revised)
    assert record is not None
    constraints = thaw_json(record.normalized_constraints)
    assert [item["field"] for item in constraints["draft_changes"]] == ["method"]
    assert revised_response["pending_actions"]


def test_initial_start_phrase_cannot_create_a_blank_task_or_spend_model_budget(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(conversation_id, "确认并开始本次计算")
    state = runtime.conversation.get_state(conversation_id)
    assert response["task_ids"] == []
    assert runtime.task.list_tasks(conversation_id) == ()
    assert state.model_calls == 0
    assert "没有可继续操作" in response["text"]


def test_completed_opt_follow_up_uses_the_exact_trusted_source(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    source_task_id = _complete_opt(runtime, conversation_id)

    display_response = runtime.conversation.message(
        conversation_id, "只显示优化能量，保留六位小数"
    )
    display_query = display_response["responses"][0]["payload"]
    assert display_response["responses"][0]["intent"] == "context_query"
    assert display_query["view"]["output_spec"]["precision"] == 6
    assert display_query["view"]["output_spec"]["quantities"][0]["kind"] == (
        "opt_final_electronic_energy"
    )

    response = runtime.conversation.message(conversation_id, "刚才优化结构上补做 Freq")
    assert len(runtime.task.list_tasks(conversation_id)) == 2
    follow_up = _task(runtime, conversation_id, response)
    source = runtime.task.get_task(conversation_id, source_task_id)
    assert source is not None
    assert follow_up.task_id != source_task_id
    assert follow_up.state is TaskPhase.PLAN_READY
    assert follow_up.request is not None and follow_up.request.operations == ("freq",)
    assert follow_up.p4_run_id == source.p4_run_id
    assert follow_up.plan is not None
    assert follow_up.plan.protocol_id == "p5.freq_from_opt.r2scan3c.4core2048.v1"
    record = runtime.task.planning_record(follow_up)
    assert record is not None
    assert record.source_task_id == source_task_id
    assert record.external_opt_result_id is not None


def test_v4_deepseek_transport_keeps_two_turns_sparse_and_durable(tmp_path) -> None:
    responses = [
        {
            "schema_version": TURN_SCHEMA_V4,
            "prompt_version": PROMPT_VERSION_V4,
            "subrequests": [
                {
                    "intent": "chemical_calculation",
                    "action": "plan_new",
                    "changes": [
                        {
                            "field": "molecule",
                            "value": {"kind": "name", "value": "water"},
                            "evidence": {"quote": "水分子"},
                        },
                        {
                            "field": "operations",
                            "value": ["opt"],
                            "evidence": {"quote": "优化"},
                        },
                    ],
                    "plan_proposal": None,
                    "output_patch": None,
                    "requested_execution": False,
                }
            ],
        },
        {
            "schema_version": TURN_SCHEMA_V4,
            "prompt_version": PROMPT_VERSION_V4,
            "subrequests": [
                {
                    "intent": "chemical_calculation",
                    "action": "revise_draft",
                    "task_alias": "当前任务",
                    "changes": [
                        {
                            "field": "environment",
                            "value": "gas",
                            "evidence": {"quote": "气相"},
                        }
                    ],
                    "plan_proposal": None,
                    "output_patch": None,
                    "requested_execution": False,
                }
            ],
        },
    ]

    def transport(_request, _headers):
        return ModelCallResponse(
            provider="deepseek",
            model="test-v4",
            content=json.dumps(responses.pop(0), ensure_ascii=False),
        )

    config = P7RuntimeConfig.for_profile("deepseek_fake", tmp_path, project_root=Path.cwd())
    adapter = DeepSeekChatAdapter(
        api_key="test-key",
        model="test-v4",
        transport=transport,
        intake_schema_version=TURN_SCHEMA_V4,
    )
    runtime = build_p7_runtime(tmp_path, runtime_config=config, planner=adapter)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    first = runtime.conversation.message(conversation_id, "优化水分子")
    second = runtime.conversation.message(conversation_id, "当前任务环境改为气相")

    task = _task(runtime, conversation_id, second)
    assert task.revision == 2
    assert task.state is TaskPhase.PLAN_READY
    assert task.request is not None and task.request.operations == ("opt",)
    assert task.request.environment is not None
    assert task.request.environment.source is ParameterSource.USER_EXPLICIT
    assert first["source"] == "deepseek_chat"
    assert second["source"] == "deepseek_chat"
    assert "p7.intake-plan.v4" in adapter.last_request_body["messages"][0]["content"]
    state = runtime.conversation.get_state(conversation_id)
    assert state.model_calls == 2
    assert not responses


def test_baseline_v4_emits_sparse_changes_only() -> None:
    interpretation = BaselinePlanner(TURN_SCHEMA_V4).interpret_text("当前任务改为气相")
    calculation = interpretation.subrequests[0]
    assert isinstance(calculation, CalculationIntentV4)
    assert [item.field for item in calculation.changes] == ["environment"]
    assert calculation.changes[0].evidence.quote == "气相"
    assert calculation.plan_proposal is None


def test_frequency_prohibition_and_local_minimum_requirement_stay_in_conflict(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)
    conversation_id = runtime.conversation.new_conversation()["conversation_id"]
    response = runtime.conversation.message(
        conversation_id, "优化水分子，但不要频率，必须证明局部极小值"
    )
    task = _task(runtime, conversation_id, response)

    assert task.state is TaskPhase.NEEDS_CLARIFICATION
    assert task.request is not None
    assert task.request.operations == ("opt", "freq")
    assert any("frequency is prohibited" in item for item in task.request.prohibited_requests)
    assert response["pending_actions"] == []
    assert "保留局部极小验证目标" in response["text"]
