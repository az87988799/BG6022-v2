"""Typed external commands for the P3 Water workflow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    ApprovalGrantId,
    CommandId,
    ConversationId,
    InterruptId,
    RunId,
    new_id,
)
from orca_agent.orchestration.p3_versions import P3_FIXTURE_ID, P3_SCHEMA_VERSION


class P3Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    command_id: CommandId
    schema_version: int
    requested_at_utc: datetime

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P3_SCHEMA_VERSION:
            raise ValueError("P3 command schema_version must be 2")
        return value

    @field_validator("requested_at_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("requested_at_utc must be UTC")
        return value

    def command_hash(self) -> str:
        return sha256_hex(self)


class StartWaterRun(P3Command):
    command_id: CommandId
    run_id: RunId
    conversation_id: ConversationId
    fixture_id: Literal["water_sp_v1"]
    new_conversation: bool

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId | None = None,
        conversation_id: ConversationId | None = None,
        command_id: CommandId | None = None,
        requested_at_utc: datetime | None = None,
        new_conversation: bool = True,
    ) -> StartWaterRun:
        return cls(
            command_id=command_id or new_id(CommandId),
            schema_version=P3_SCHEMA_VERSION,
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            run_id=run_id or new_id(RunId),
            conversation_id=conversation_id or new_id(ConversationId),
            fixture_id=P3_FIXTURE_ID,
            new_conversation=new_conversation,
        )


class ApproveAction(P3Command):
    run_id: RunId
    conversation_id: ConversationId
    interrupt_id: InterruptId
    action_id: ActionId
    action_hash: str
    envelope_hash: str
    budget_hash: str
    expected_revision: int = Field(ge=1)
    approval_grant_id: ApprovalGrantId | None = None

    @field_validator("action_hash", "envelope_hash", "budget_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        if (
            len(value) != 64
            or value.casefold() != value
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("hash must be lowercase SHA-256 hex")
        return value

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        interrupt_id: InterruptId,
        action_id: ActionId,
        action_hash: str,
        envelope_hash: str,
        budget_hash: str,
        expected_revision: int,
        command_id: CommandId | None = None,
        approval_grant_id: ApprovalGrantId | None = None,
        requested_at_utc: datetime | None = None,
    ) -> ApproveAction:
        return cls(
            command_id=command_id or new_id(CommandId),
            schema_version=P3_SCHEMA_VERSION,
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            run_id=run_id,
            conversation_id=conversation_id,
            interrupt_id=interrupt_id,
            action_id=action_id,
            action_hash=action_hash,
            envelope_hash=envelope_hash,
            budget_hash=budget_hash,
            expected_revision=expected_revision,
            approval_grant_id=approval_grant_id,
        )


class CancelWaterRun(P3Command):
    run_id: RunId
    conversation_id: ConversationId
    expected_revision: int = Field(ge=1)
    reason_code: Literal["user_cancelled", "approval_expired", "workflow_failed"]

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        expected_revision: int,
        reason_code: Literal[
            "user_cancelled", "approval_expired", "workflow_failed"
        ] = "user_cancelled",
        command_id: CommandId | None = None,
        requested_at_utc: datetime | None = None,
    ) -> CancelWaterRun:
        return cls(
            command_id=command_id or new_id(CommandId),
            schema_version=P3_SCHEMA_VERSION,
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            run_id=run_id,
            conversation_id=conversation_id,
            expected_revision=expected_revision,
            reason_code=reason_code,
        )


__all__ = ["ApproveAction", "CancelWaterRun", "P3Command", "StartWaterRun"]
