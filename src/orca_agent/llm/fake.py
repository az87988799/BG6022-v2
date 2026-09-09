"""Deterministic fake planner for the complete offline P7 test chain."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from orca_agent.domain.p7_conversation import ContextSnapshot, TurnInterpretation
from orca_agent.llm.baseline import BaselinePlanner
from orca_agent.llm.ports import ModelCallResponse


class FakePlanner:
    adapter_id = "fake"
    model = "fake-p7-v1"

    def __init__(
        self,
        responses: Mapping[str, TurnInterpretation | str | bytes] | None = None,
        *,
        callback: Callable[[ContextSnapshot], TurnInterpretation | ModelCallResponse] | None = None,
        fail_with: str | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.callback = callback
        self.fail_with = fail_with
        self.calls = 0
        self._baseline = BaselinePlanner()

    def interpret(self, context: ContextSnapshot) -> TurnInterpretation | ModelCallResponse:
        self.calls += 1
        if self.fail_with is not None:
            return ModelCallResponse(
                provider="fake",
                model=self.model,
                error_code="fake_failure",
                error_message=self.fail_with,
            )
        if self.callback is not None:
            return self.callback(context)
        value = self.responses.get(context.current_message)
        if value is None:
            return self._baseline.interpret(context)
        if isinstance(value, TurnInterpretation):
            return value
        return ModelCallResponse(
            provider="fake",
            model=self.model,
            content=value.decode("utf-8") if isinstance(value, bytes) else value,
            raw_bytes=value.encode("utf-8") if isinstance(value, str) else value,
        )


__all__ = ["FakePlanner"]
