"""Typed public commands for P6 source freezing and cancellation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import AssessmentId, CommandId, ConversationId, RunId, new_id
from orca_agent.orchestration.p6_versions import P6_ENGINE_VERSION, P6_SCHEMA_VERSION
from orca_agent.orchestration.state import KernelModel
from orca_agent.orchestration.temporal import ensure_utc


class P6CommandBase(KernelModel):
    command_id: CommandId
    schema_version: Literal[P6_SCHEMA_VERSION] = P6_SCHEMA_VERSION
    engine_version: Literal[P6_ENGINE_VERSION] = P6_ENGINE_VERSION
    command_type: str
    run_id: RunId
    requested_at_utc: datetime

    @field_validator("command_type")
    @classmethod
    def _command_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("P6 command_type must not be blank")
        return value.strip()

    @field_validator("requested_at_utc")
    @classmethod
    def _requested_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def command_hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


class AssessP6Run(P6CommandBase):
    command_type: Literal["p6.assess"] = "p6.assess"
    source_p5_run_id: RunId
    profile_id: Literal["p6.nonlinear.r2scan3c.v1"] = "p6.nonlinear.r2scan3c.v1"
    expected_source_revision: int | None = Field(default=None, ge=1)
    reference_assessment_id: AssessmentId | None = None

    @classmethod
    def create(
        cls,
        *,
        source_p5_run_id: RunId,
        requested_at_utc: datetime,
        run_id: RunId | None = None,
        command_id: CommandId | None = None,
        profile_id: str = "p6.nonlinear.r2scan3c.v1",
        expected_source_revision: int | None = None,
        reference_assessment_id: AssessmentId | None = None,
    ) -> AssessP6Run:
        return cls(
            command_id=command_id or new_id(CommandId),
            run_id=run_id or new_id(RunId),
            source_p5_run_id=source_p5_run_id,
            profile_id=profile_id,
            expected_source_revision=expected_source_revision,
            reference_assessment_id=reference_assessment_id,
            requested_at_utc=requested_at_utc,
        )


class CancelP6Run(P6CommandBase):
    command_type: Literal["p6.cancel"] = "p6.cancel"
    conversation_id: ConversationId
    expected_revision: int = Field(ge=1)
    reason_code: str = "user_cancelled"

    @field_validator("reason_code")
    @classmethod
    def _reason(cls, value: str) -> str:
        if not value.strip() or len(value) > 128:
            raise ValueError("P6 cancellation reason is invalid")
        return value.strip()

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        expected_revision: int,
        requested_at_utc: datetime,
        reason_code: str = "user_cancelled",
        command_id: CommandId | None = None,
    ) -> CancelP6Run:
        return cls(
            command_id=command_id or new_id(CommandId),
            run_id=run_id,
            conversation_id=conversation_id,
            expected_revision=expected_revision,
            reason_code=reason_code,
            requested_at_utc=requested_at_utc,
        )


__all__ = ["AssessP6Run", "CancelP6Run", "P6CommandBase"]
