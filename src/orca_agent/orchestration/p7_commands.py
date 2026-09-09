"""Strict external command envelopes used by the P7 CLI and replay path."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import CommandId, ConversationId, new_id
from orca_agent.domain.json_types import FrozenJsonObject, freeze_json_object
from orca_agent.domain.p7_conversation import P7Model
from orca_agent.orchestration.p7_versions import P7_CONVERSATION_ENGINE, P7_SCHEMA_VERSION
from orca_agent.orchestration.temporal import ensure_utc


class P7CommandType(StrEnum):
    CREATE_CONVERSATION = "p7.create_conversation"
    MESSAGE = "p7.message"
    ACTION = "p7.action"
    CANCEL_TASK = "p7.cancel_task"
    INTERRUPT_TURN = "p7.interrupt_turn"
    LINK_RESULT = "p7.link_result"


class PendingActionType(StrEnum):
    ACCEPT_PLAN = "accept_plan"
    CONFIRM_IDENTITY = "confirm_identity"
    APPROVE_EXECUTION = "approve_execution"
    REJECT = "reject"


class ActionDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


class P7CommandBase(P7Model):
    command_id: CommandId
    schema_version: Literal[P7_SCHEMA_VERSION] = P7_SCHEMA_VERSION
    engine_version: Literal[P7_CONVERSATION_ENGINE] = P7_CONVERSATION_ENGINE
    command_type: P7CommandType
    requested_at_utc: datetime

    @field_validator("requested_at_utc")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def command_hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


class CreateConversation(P7CommandBase):
    command_type: Literal[P7CommandType.CREATE_CONVERSATION] = P7CommandType.CREATE_CONVERSATION
    conversation_id: ConversationId
    model_call_budget: int = Field(default=120, ge=0, le=10_000)

    @classmethod
    def create(
        cls,
        *,
        conversation_id: ConversationId | None = None,
        model_call_budget: int = 120,
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> CreateConversation:
        return cls(
            command_id=command_id or new_id(CommandId),
            conversation_id=conversation_id or new_id(ConversationId),
            model_call_budget=model_call_budget,
            requested_at_utc=requested_at_utc,
        )


class MessageCommand(P7CommandBase):
    command_type: Literal[P7CommandType.MESSAGE] = P7CommandType.MESSAGE
    conversation_id: ConversationId
    text: str

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value or len(value.strip()) > 65_536:
            raise ValueError("message text is invalid")
        return value.strip()

    @classmethod
    def create(
        cls,
        *,
        conversation_id: ConversationId,
        text: str,
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> MessageCommand:
        return cls(
            command_id=command_id or new_id(CommandId),
            conversation_id=conversation_id,
            text=text,
            requested_at_utc=requested_at_utc,
        )


class ActionCommand(P7CommandBase):
    command_type: Literal[P7CommandType.ACTION] = P7CommandType.ACTION
    conversation_id: ConversationId
    token: str
    decision: ActionDecision
    payload: FrozenJsonObject = {}

    @field_validator("token")
    @classmethod
    def _token(cls, value: str) -> str:
        if not value.strip() or "\x00" in value or len(value.strip()) > 256:
            raise ValueError("action token is invalid")
        return value.strip()

    @field_validator("payload", mode="before")
    @classmethod
    def _payload(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)

    @classmethod
    def create(
        cls,
        *,
        conversation_id: ConversationId,
        token: str,
        decision: ActionDecision,
        payload: dict[str, object] | None = None,
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> ActionCommand:
        return cls(
            command_id=command_id or new_id(CommandId),
            conversation_id=conversation_id,
            token=token,
            decision=decision,
            payload={} if payload is None else payload,
            requested_at_utc=requested_at_utc,
        )


class CancelTaskCommand(P7CommandBase):
    command_type: Literal[P7CommandType.CANCEL_TASK] = P7CommandType.CANCEL_TASK
    conversation_id: ConversationId
    task_id: str
    expected_revision: int = Field(ge=1)
    reason_code: str = "cancelled"

    @field_validator("task_id", "reason_code")
    @classmethod
    def _cancel_text(cls, value: str, info: object) -> str:
        if not value.strip() or "\x00" in value or len(value.strip()) > 256:
            raise ValueError(f"{getattr(info, 'field_name', 'field')} is invalid")
        return value.strip()

    @classmethod
    def create(
        cls,
        *,
        conversation_id: ConversationId,
        task_id: str,
        expected_revision: int,
        reason_code: str = "cancelled",
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> CancelTaskCommand:
        return cls(
            command_id=command_id or new_id(CommandId),
            conversation_id=conversation_id,
            task_id=task_id,
            expected_revision=expected_revision,
            reason_code=reason_code,
            requested_at_utc=requested_at_utc,
        )


class InterruptTurnCommand(P7CommandBase):
    command_type: Literal[P7CommandType.INTERRUPT_TURN] = P7CommandType.INTERRUPT_TURN
    conversation_id: ConversationId
    turn_id: str

    @field_validator("turn_id")
    @classmethod
    def _turn_id(cls, value: str) -> str:
        if not value.strip() or "\x00" in value or len(value.strip()) > 256:
            raise ValueError("turn_id is invalid")
        return value.strip()

    @classmethod
    def create(
        cls,
        *,
        conversation_id: ConversationId,
        turn_id: str,
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> InterruptTurnCommand:
        return cls(
            command_id=command_id or new_id(CommandId),
            conversation_id=conversation_id,
            turn_id=turn_id,
            requested_at_utc=requested_at_utc,
        )


__all__ = [
    "ActionCommand",
    "ActionDecision",
    "CancelTaskCommand",
    "CreateConversation",
    "InterruptTurnCommand",
    "MessageCommand",
    "P7CommandBase",
    "P7CommandType",
    "PendingActionType",
]
