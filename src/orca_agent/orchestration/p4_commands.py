"""Strict external command envelopes for the P4 identity workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    CommandId,
    ConversationId,
    InterruptId,
    RunId,
    WorkflowRecordId,
    new_id,
)
from orca_agent.domain.p4 import (
    IdentityDecision,
    IdentityProvider,
    MoleculeInputKind,
    P4Model,
)
from orca_agent.orchestration.p4_versions import P4_ENGINE_VERSION, P4_SCHEMA_VERSION
from orca_agent.orchestration.temporal import ensure_utc


class P4Command(P4Model):
    """Command envelope whose schema and engine are never inferred on load."""

    command_id: CommandId
    schema_version: int
    engine_version: str
    requested_at_utc: datetime

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P4_SCHEMA_VERSION:
            raise ValueError("P4 command schema_version must be 3")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P4_ENGINE_VERSION:
            raise ValueError("P4 command engine_version is unsupported")
        return value

    @field_validator("requested_at_utc")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def command_hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


class StartPlanningRun(P4Command):
    command_type: Literal["p4.prepare"] = "p4.prepare"
    run_id: RunId
    conversation_id: ConversationId
    input_kind: MoleculeInputKind
    raw_input: str
    charge: int
    multiplicity: int = Field(ge=1)
    provider: IdentityProvider
    protocol_id: str
    new_conversation: bool

    @field_validator("raw_input", "protocol_id")
    @classmethod
    def _text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("P4 command text is blank or contains NUL")
        return value.strip()

    @classmethod
    def create(
        cls,
        *,
        input_kind: MoleculeInputKind,
        raw_input: str,
        charge: int,
        multiplicity: int,
        provider: IdentityProvider,
        protocol_id: str = "ground_state_baseline_r2scan3c_v1",
        run_id: RunId | None = None,
        conversation_id: ConversationId | None = None,
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
        new_conversation: bool = True,
    ) -> StartPlanningRun:
        return cls(
            command_id=command_id or new_id(CommandId),
            schema_version=P4_SCHEMA_VERSION,
            engine_version=P4_ENGINE_VERSION,
            requested_at_utc=requested_at_utc,
            run_id=run_id or new_id(RunId),
            conversation_id=conversation_id or new_id(ConversationId),
            input_kind=input_kind,
            raw_input=raw_input,
            charge=charge,
            multiplicity=multiplicity,
            provider=provider,
            protocol_id=protocol_id,
            new_conversation=new_conversation,
        )


class ConfirmMoleculeIdentity(P4Command):
    command_type: Literal["p4.confirm_identity"] = "p4.confirm_identity"
    run_id: RunId
    conversation_id: ConversationId
    interrupt_id: InterruptId
    expected_revision: int = Field(ge=1)
    query_id: WorkflowRecordId
    query_hash: str
    candidate_bundle_id: WorkflowRecordId
    candidate_bundle_hash: str
    candidate_set_hash: str
    candidate_id: str
    candidate_hash: str
    decision: IdentityDecision

    @field_validator("query_hash", "candidate_bundle_hash", "candidate_set_hash", "candidate_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        if (
            len(value) != 64
            or value.casefold() != value
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("P4 command hash must be lowercase SHA-256 hex")
        return value

    @field_validator("candidate_id")
    @classmethod
    def _candidate(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("candidate_id must be non-blank")
        return value.strip()

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        interrupt_id: InterruptId,
        expected_revision: int,
        query_id: WorkflowRecordId,
        query_hash: str,
        candidate_bundle_id: WorkflowRecordId,
        candidate_bundle_hash: str,
        candidate_set_hash: str,
        candidate_id: str,
        candidate_hash: str,
        decision: IdentityDecision,
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> ConfirmMoleculeIdentity:
        return cls(
            command_id=command_id or new_id(CommandId),
            schema_version=P4_SCHEMA_VERSION,
            engine_version=P4_ENGINE_VERSION,
            requested_at_utc=requested_at_utc,
            run_id=run_id,
            conversation_id=conversation_id,
            interrupt_id=interrupt_id,
            expected_revision=expected_revision,
            query_id=query_id,
            query_hash=query_hash,
            candidate_bundle_id=candidate_bundle_id,
            candidate_bundle_hash=candidate_bundle_hash,
            candidate_set_hash=candidate_set_hash,
            candidate_id=candidate_id,
            candidate_hash=candidate_hash,
            decision=decision,
        )


class CancelPlanningRun(P4Command):
    command_type: Literal["p4.cancel"] = "p4.cancel"
    run_id: RunId
    conversation_id: ConversationId
    expected_revision: int = Field(ge=1)
    reason_code: Literal["user_cancelled", "workflow_failed"]

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        expected_revision: int,
        reason_code: Literal["user_cancelled", "workflow_failed"] = "user_cancelled",
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> CancelPlanningRun:
        return cls(
            command_id=command_id or new_id(CommandId),
            schema_version=P4_SCHEMA_VERSION,
            engine_version=P4_ENGINE_VERSION,
            requested_at_utc=requested_at_utc,
            run_id=run_id,
            conversation_id=conversation_id,
            expected_revision=expected_revision,
            reason_code=reason_code,
        )


__all__ = [
    "CancelPlanningRun",
    "ConfirmMoleculeIdentity",
    "P4Command",
    "StartPlanningRun",
]
