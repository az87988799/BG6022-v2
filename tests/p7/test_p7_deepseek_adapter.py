"""Boundary checks for the opt-in DeepSeek Chat Completions adapter."""

from __future__ import annotations

import json

from orca_agent.domain.ids import ConversationId, new_id
from orca_agent.domain.p7_conversation import ContextSnapshot
from orca_agent.llm.deepseek_chat import DeepSeekChatAdapter
from orca_agent.llm.ports import ModelCallResponse


def _context() -> ContextSnapshot:
    return ContextSnapshot.create(
        conversation_id=new_id(ConversationId),
        turn_sequence_no=1,
        current_message="hello",
        active_task_alias=None,
        tasks=(),
        recent_turns=(),
        facts={},
        encoded_size_bytes=0,
    )


def test_deepseek_request_uses_bounded_json_chat_contract() -> None:
    captured: dict[str, object] = {}

    def transport(request, headers):
        captured["request"] = request.model_dump(mode="json")
        captured["headers"] = headers
        return ModelCallResponse(
            provider="deepseek",
            model="test-model",
            content=json.dumps(
                {
                    "schema_version": "p7.turn.v1",
                    "prompt_version": "p7.intake-plan.v1",
                    "subrequests": [
                        {
                            "intent": "general_qa",
                            "question": "hello",
                            "answer_draft": "hi",
                        }
                    ],
                }
            ),
        )

    adapter = DeepSeekChatAdapter(
        api_key="test-key",
        model="test-model",
        transport=transport,
    )
    response = adapter.interpret(_context())
    assert response.error_code is None
    request = captured["request"]
    assert request["max_tokens"] == 2048
    assert request["stream"] is False
    assert request["thinking_enabled"] is False
    assert request["response_format"] == {"type": "json_object"}
    headers = captured["headers"]
    assert headers["Authorization"] == "Bearer test-key"
    assert adapter.last_request_body["thinking"] == {"type": "disabled"}


def test_deepseek_missing_configuration_is_non_network_error() -> None:
    response = DeepSeekChatAdapter(environ={}).interpret(_context())
    assert response.error_code == "configuration_missing"
