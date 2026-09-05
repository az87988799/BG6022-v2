"""Typed, prefixed UUID4 identifiers."""

from __future__ import annotations

import re
import uuid
from typing import ClassVar, TypeVar

from pydantic_core import core_schema

from .errors import InvalidIdentifierError


class PrefixedId(str):
    """A string ID whose prefix identifies its domain entity type."""

    prefix: ClassVar[str] = "id"
    _pattern: ClassVar[re.Pattern[str]] = re.compile(r"^id_[0-9a-f]{32}$")

    def __new__(cls, value: str) -> PrefixedId:
        if not isinstance(value, str) or cls._pattern.fullmatch(value) is None:
            raise InvalidIdentifierError(
                f"invalid {cls.__name__}",
                details={"expected_prefix": cls.prefix},
            )
        return str.__new__(cls, value)

    @classmethod
    def _validate_pydantic(cls, value: str) -> PrefixedId:
        return cls(value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source: type[object],
        _handler: object,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls._validate_pydantic,
            core_schema.str_schema(strict=True),
        )


class ProblemSpecId(PrefixedId):
    prefix = "problem"
    _pattern = re.compile(r"^problem_[0-9a-f]{32}$")


class PlanProposalId(PrefixedId):
    prefix = "plan"
    _pattern = re.compile(r"^plan_[0-9a-f]{32}$")


class PrimitiveId(PrefixedId):
    prefix = "primitive"
    _pattern = re.compile(r"^primitive_[0-9a-f]{32}$")


class ActionId(PrefixedId):
    prefix = "action"
    _pattern = re.compile(r"^action_[0-9a-f]{32}$")


class EvidenceId(PrefixedId):
    prefix = "evidence"
    _pattern = re.compile(r"^evidence_[0-9a-f]{32}$")


class ClaimId(PrefixedId):
    prefix = "claim"
    _pattern = re.compile(r"^claim_[0-9a-f]{32}$")


class ArtifactId(PrefixedId):
    prefix = "artifact"
    _pattern = re.compile(r"^artifact_[0-9a-f]{32}$")


class RunId(PrefixedId):
    prefix = "run"
    _pattern = re.compile(r"^run_[0-9a-f]{32}$")


class CommandId(PrefixedId):
    prefix = "command"
    _pattern = re.compile(r"^command_[0-9a-f]{32}$")


class EventId(PrefixedId):
    prefix = "event"
    _pattern = re.compile(r"^event_[0-9a-f]{32}$")


class EffectId(PrefixedId):
    prefix = "effect"
    _pattern = re.compile(r"^effect_[0-9a-f]{32}$")


class InterruptId(PrefixedId):
    prefix = "interrupt"
    _pattern = re.compile(r"^interrupt_[0-9a-f]{32}$")


class WorkerId(PrefixedId):
    prefix = "worker"
    _pattern = re.compile(r"^worker_[0-9a-f]{32}$")


class ConversationId(PrefixedId):
    prefix = "conversation"
    _pattern = re.compile(r"^conversation_[0-9a-f]{32}$")


class ApprovalGrantId(PrefixedId):
    prefix = "approval"
    _pattern = re.compile(r"^approval_[0-9a-f]{32}$")


class ExecutionId(PrefixedId):
    prefix = "execution"
    _pattern = re.compile(r"^execution_[0-9a-f]{32}$")


class JobId(PrefixedId):
    prefix = "job"
    _pattern = re.compile(r"^job_[0-9a-f]{32}$")


class WorkflowRecordId(PrefixedId):
    prefix = "workflow"
    _pattern = re.compile(r"^workflow_[0-9a-f]{32}$")


class AssessmentId(PrefixedId):
    prefix = "assessment"
    _pattern = re.compile(r"^assessment_[0-9a-f]{32}$")


class ReportManifestId(PrefixedId):
    prefix = "report"
    _pattern = re.compile(r"^report_[0-9a-f]{32}$")


EFFECT_NAMESPACE = uuid.UUID("9d5c0f3e-3b24-4f3f-9bde-7f07bb3f9473")


IdT = TypeVar("IdT", bound=PrefixedId)


def new_id(identifier_type: type[IdT]) -> IdT:
    """Create a new UUID4 ID for the requested typed entity."""

    return identifier_type(f"{identifier_type.prefix}_{uuid.uuid4().hex}")


def effect_id_for(event_id: EventId, index: int) -> EffectId:
    """Create the stable effect ID for an event and zero-based effect index."""

    if type(index) is not int or index < 0:
        raise ValueError("effect index must be a non-negative integer")
    value = uuid.uuid5(EFFECT_NAMESPACE, f"{event_id}:{index}")
    return EffectId(f"effect_{value.hex}")


def completion_command_id(
    effect_id: EffectId,
    generation: int,
    outcome: str,
) -> CommandId:
    """Derive the stable command ID for one terminal completion attempt."""

    if type(generation) is not int or generation < 1:
        raise ValueError("completion generation must be a positive integer")
    if not isinstance(outcome, str) or not outcome.strip():
        raise ValueError("completion outcome must not be blank")
    value = uuid.uuid5(EFFECT_NAMESPACE, f"completion:{effect_id}:{generation}:{outcome}")
    return CommandId(f"command_{value.hex}")


def is_new_external_command_id(value: CommandId) -> bool:
    """Only new external writes require UUID4; historical reads stay permissive."""
    return uuid.UUID(hex=str(value).removeprefix("command_")).version == 4


__all__ = [
    "ActionId",
    "ApprovalGrantId",
    "AssessmentId",
    "ArtifactId",
    "ClaimId",
    "CommandId",
    "ConversationId",
    "EFFECT_NAMESPACE",
    "EffectId",
    "EvidenceId",
    "EventId",
    "ExecutionId",
    "InterruptId",
    "JobId",
    "PlanProposalId",
    "PrimitiveId",
    "ProblemSpecId",
    "ReportManifestId",
    "RunId",
    "WorkerId",
    "WorkflowRecordId",
    "completion_command_id",
    "effect_id_for",
    "new_id",
]
