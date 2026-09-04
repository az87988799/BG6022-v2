"""Typed results returned by the kernel application service."""

from __future__ import annotations

from pydantic import field_validator

from orca_agent.domain.ids import EventId, InterruptId, RunId
from orca_agent.domain.json_types import FrozenJsonObject, JsonObject, freeze_json_object
from orca_agent.orchestration.state import KernelModel, RunStatus


class ApplicationResult(KernelModel):
    accepted: bool
    code: str
    run_id: RunId
    revision: int
    status: RunStatus
    event_id: EventId | None
    interrupt_id: InterruptId | None
    details: FrozenJsonObject

    @field_validator("details", mode="before")
    @classmethod
    def _details_are_frozen_json(cls, value: object) -> FrozenJsonObject:
        try:
            return freeze_json_object(value)
        except ValueError as error:
            raise ValueError("result details must be a JSON object") from error

    @classmethod
    def accepted_result(
        cls,
        *,
        code: str,
        run_id: RunId,
        revision: int,
        status: RunStatus,
        event_id: EventId | None,
        interrupt_id: InterruptId | None = None,
        details: JsonObject | None = None,
    ) -> ApplicationResult:
        return cls(
            accepted=True,
            code=code,
            run_id=run_id,
            revision=revision,
            status=status,
            event_id=event_id,
            interrupt_id=interrupt_id,
            details={} if details is None else details,
        )

    @classmethod
    def rejected_result(
        cls,
        *,
        code: str,
        run_id: RunId,
        revision: int,
        status: RunStatus,
        details: JsonObject | None = None,
        event_id: EventId | None = None,
        interrupt_id: InterruptId | None = None,
    ) -> ApplicationResult:
        return cls(
            accepted=False,
            code=code,
            run_id=run_id,
            revision=revision,
            status=status,
            event_id=event_id,
            interrupt_id=interrupt_id,
            details={} if details is None else details,
        )


__all__ = ["ApplicationResult"]
