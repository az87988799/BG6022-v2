"""Reducer transition and interrupt projection contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import field_validator, model_validator

from orca_agent.domain.ids import InterruptId, RunId
from orca_agent.domain.json_types import (
    FrozenJsonObject,
    FrozenJsonValue,
    JsonObject,
    freeze_json_object,
    freeze_json_value,
)

from .effects import EffectSpec
from .state import KernelModel, KernelState, RunStatus


class InterruptStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


class InterruptProjectionOperation(StrEnum):
    INSERT_PENDING = "insert_pending"
    FINALIZE = "finalize"
    SUPERSEDE = "supersede"


class InterruptProjectionOp(KernelModel):
    """Database-neutral operation applied inside the command transaction."""

    operation: InterruptProjectionOperation
    run_id: RunId
    interrupt_id: InterruptId
    status: InterruptStatus
    kind: str | None
    payload: FrozenJsonObject | None
    expires_at_utc: datetime | None
    response: FrozenJsonValue | None
    superseded_by: InterruptId | None

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_is_json_object(cls, value: JsonObject | None) -> FrozenJsonObject | None:
        if value is None:
            return None
        try:
            return freeze_json_object(value)
        except ValueError as error:
            raise ValueError("interrupt payload must be a JSON object") from error

    @field_validator("response", mode="before")
    @classmethod
    def _response_is_json(cls, value: object | None) -> FrozenJsonValue | None:
        if value is None:
            return None
        try:
            return freeze_json_value(value)
        except ValueError as error:
            raise ValueError("interrupt response must be JSON") from error

    @field_validator("expires_at_utc")
    @classmethod
    def _expiry_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expires_at_utc must be timezone-aware UTC")
        if value is not None and value.utcoffset().total_seconds() != 0:
            raise ValueError("expires_at_utc must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def _operation_shape(self) -> InterruptProjectionOp:
        if self.operation is InterruptProjectionOperation.INSERT_PENDING:
            if self.status is not InterruptStatus.PENDING:
                raise ValueError("insert_pending must have pending status")
            if not self.kind or self.payload is None or self.expires_at_utc is None:
                raise ValueError("insert_pending requires kind, payload, and expiry")
        elif self.operation is InterruptProjectionOperation.SUPERSEDE:
            if self.status is not InterruptStatus.SUPERSEDED or self.superseded_by is None:
                raise ValueError("supersede requires superseded status and replacement ID")
        elif self.operation is InterruptProjectionOperation.FINALIZE:
            if self.status is InterruptStatus.PENDING:
                raise ValueError("finalize requires terminal status")
        return self


class ApplicationOutcome(KernelModel):
    accepted: bool
    code: str
    details: FrozenJsonObject

    @field_validator("code")
    @classmethod
    def _code_is_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("outcome code must not be blank")
        return value.strip()

    @field_validator("details", mode="before")
    @classmethod
    def _details_are_json_object(cls, value: JsonObject) -> FrozenJsonObject:
        try:
            return freeze_json_object(value)
        except ValueError as error:
            raise ValueError("outcome details must be a JSON object") from error


class Transition(KernelModel):
    """Validated output of the pure reducer."""

    next_status: RunStatus
    next_state: KernelState
    effects: tuple[EffectSpec, ...]
    interrupt_operations: tuple[InterruptProjectionOp, ...]
    outcome: ApplicationOutcome

    @model_validator(mode="after")
    def _invariants(self) -> Transition:
        if self.next_state.status is not self.next_status:
            raise ValueError("next_status must match next_state.status")
        indexes = tuple(effect.effect_index for effect in self.effects)
        if indexes != tuple(range(len(indexes))):
            raise ValueError("effect indexes must be contiguous and ordered")
        if sum(effect.effect_class.value == "external" for effect in self.effects) > 1:
            raise ValueError("a transition may contain at most one external effect")

        pending_ops = tuple(
            operation
            for operation in self.interrupt_operations
            if operation.status is InterruptStatus.PENDING
        )
        if len(pending_ops) > 1:
            raise ValueError("a transition may create at most one pending interrupt")
        pending_id = self.next_state.pending_interrupt_id
        if pending_id is None and pending_ops:
            raise ValueError("pending projection operation must be reflected in next state")
        if pending_id is not None:
            if not pending_ops or pending_ops[0].interrupt_id != pending_id:
                raise ValueError("state pending interrupt must match its projection operation")
        if self.next_status.is_terminal and pending_id is not None:
            raise ValueError("terminal state cannot retain a pending interrupt")
        return self


__all__ = [
    "ApplicationOutcome",
    "InterruptProjectionOp",
    "InterruptProjectionOperation",
    "InterruptStatus",
    "Transition",
]
