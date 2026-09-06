"""Command and event labels used by the schema-4 P5 event stream."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    CommandId,
    ConversationId,
    RunId,
    WorkflowRecordId,
    new_id,
)
from orca_agent.domain.p5 import P5Model
from orca_agent.orchestration.p5_versions import P5_ENGINE_VERSION, P5_SCHEMA_VERSION


class P5CommandType(StrEnum):
    CREATE_EXECUTION = "p5.create_execution"
    PREPARE_NODE = "p5.prepare_node"
    APPROVE_ACTION = "p5.approve_action"
    LAUNCH_ORCA = "p5.launch_orca"
    OBSERVE_JOB = "p5.observe_job"
    COLLECT_RESULT = "p5.collect_result"
    REQUEST_CANCEL = "p5.request_cancel"
    RECONCILE_EXECUTION = "p5.reconcile_execution"
    COMPLETE_CANCEL = "p5.complete_cancel"


class P5EventType(StrEnum):
    RUN_CREATED = "p5.run_created"
    NODE_PREPARED = "p5.node_prepared"
    ACTION_APPROVED = "p5.action_approved"
    JOB_RESERVED = "p5.job_reserved"
    JOB_OBSERVED = "p5.job_observed"
    RESULT_COLLECTED = "p5.result_collected"
    CANCEL_REQUESTED = "p5.cancel_requested"
    RUN_CANCELLED = "p5.run_cancelled"
    RECONCILED = "p5.reconciled"
    RUN_FAILED = "p5.run_failed"


class P5CommandBase(P5Model):
    command_id: CommandId
    schema_version: Literal[P5_SCHEMA_VERSION] = P5_SCHEMA_VERSION
    engine_version: Literal[P5_ENGINE_VERSION] = P5_ENGINE_VERSION
    command_type: P5CommandType
    run_id: RunId
    requested_at_utc: datetime

    @classmethod
    def _base(
        cls,
        *,
        run_id: RunId,
        command_type: P5CommandType,
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> dict[str, object]:
        return {
            "command_id": command_id or new_id(CommandId),
            "command_type": command_type,
            "run_id": run_id,
            "requested_at_utc": requested_at_utc,
        }

    def command_hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


class PrepareExecution(P5CommandBase):
    command_type: Literal[P5CommandType.CREATE_EXECUTION] = P5CommandType.CREATE_EXECUTION
    source_run_id: RunId
    protocol_id: str
    external_opt_result_id: WorkflowRecordId | None = None

    @classmethod
    def create(
        cls,
        *,
        source_run_id: RunId,
        protocol_id: str,
        run_id: RunId | None = None,
        external_opt_result_id: WorkflowRecordId | None = None,
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> PrepareExecution:
        return cls(
            **cls._base(
                run_id=run_id or new_id(RunId),
                command_type=P5CommandType.CREATE_EXECUTION,
                command_id=command_id,
                requested_at_utc=requested_at_utc,
            ),
            source_run_id=source_run_id,
            protocol_id=protocol_id,
            external_opt_result_id=external_opt_result_id,
        )


class ApproveP5Action(P5CommandBase):
    command_type: Literal[P5CommandType.APPROVE_ACTION] = P5CommandType.APPROVE_ACTION
    conversation_id: ConversationId
    action_id: ActionId
    action_hash: str
    binding_hash: str
    envelope_hash: str
    budget_hash: str
    expected_revision: int = Field(ge=1)

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        action_id: ActionId,
        action_hash: str,
        binding_hash: str,
        envelope_hash: str,
        budget_hash: str,
        expected_revision: int,
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> ApproveP5Action:
        return cls(
            **cls._base(
                run_id=run_id,
                command_type=P5CommandType.APPROVE_ACTION,
                command_id=command_id,
                requested_at_utc=requested_at_utc,
            ),
            conversation_id=conversation_id,
            action_id=action_id,
            action_hash=action_hash,
            binding_hash=binding_hash,
            envelope_hash=envelope_hash,
            budget_hash=budget_hash,
            expected_revision=expected_revision,
        )


class CancelP5Execution(P5CommandBase):
    command_type: Literal[P5CommandType.REQUEST_CANCEL] = P5CommandType.REQUEST_CANCEL
    conversation_id: ConversationId
    expected_revision: int = Field(ge=1)
    reason_code: str = "user_cancelled"

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        expected_revision: int,
        reason_code: str = "user_cancelled",
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> CancelP5Execution:
        return cls(
            **cls._base(
                run_id=run_id,
                command_type=P5CommandType.REQUEST_CANCEL,
                command_id=command_id,
                requested_at_utc=requested_at_utc,
            ),
            conversation_id=conversation_id,
            expected_revision=expected_revision,
            reason_code=reason_code,
        )


class ReconcileP5Execution(P5CommandBase):
    command_type: Literal[P5CommandType.RECONCILE_EXECUTION] = P5CommandType.RECONCILE_EXECUTION
    expected_revision: int = Field(ge=1)

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        expected_revision: int,
        command_id: CommandId | None = None,
        requested_at_utc: datetime,
    ) -> ReconcileP5Execution:
        return cls(
            **cls._base(
                run_id=run_id,
                command_type=P5CommandType.RECONCILE_EXECUTION,
                command_id=command_id,
                requested_at_utc=requested_at_utc,
            ),
            expected_revision=expected_revision,
        )


__all__ = [
    "ApproveP5Action",
    "CancelP5Execution",
    "P5CommandBase",
    "P5CommandType",
    "P5EventType",
    "PrepareExecution",
    "ReconcileP5Execution",
]
