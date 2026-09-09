"""Planning-layer contracts for P7 candidate workflows.

The planning contract deliberately sits beside the historical P7 task models.
Models may propose local step keys and logical input references, while the
application/compiler owns protocol IDs, record IDs, hashes, and execution
approval boundaries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..orchestration.temporal import ensure_utc
from .hashing import sha256_hex, verify_sha256
from .json_types import FrozenJsonObject, freeze_json_object
from .p7_task import ValidationStatus

PLANNING_SCHEMA_VERSION = "p7.plan-proposal.v1"
PLANNING_RECORD_SCHEMA_VERSION = "p7.planning-record.v1"


class PlanningModel(BaseModel):
    """Strict immutable planning value object."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


def _text(value: str, name: str, *, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank and contain no NUL")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value


def _optional_text(value: str | None, name: str, *, maximum: int = 2048) -> str | None:
    return None if value is None else _text(value, name, maximum=maximum)


def _hash(value: str, name: str = "hash") -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.casefold()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _optional_hash(value: str | None) -> str | None:
    return None if value is None else _hash(value)


def _record_hash(model_type: type[BaseModel], values: dict[str, object], field_name: str) -> str:
    candidate = dict(values)
    candidate[field_name] = "0" * 64
    probe = model_type.model_construct(**candidate)
    return sha256_hex(probe.model_dump(mode="json", exclude={field_name}))


class PlanProposalStep(PlanningModel):
    """One model-proposed node before the compiler creates trusted IDs."""

    key: str
    capability_id: str
    input_ref: str
    depends_on: tuple[str, ...] = ()
    purpose: str
    why: str

    @field_validator("key", "capability_id", "input_ref", "purpose", "why")
    @classmethod
    def _step_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "step field"), maximum=512)

    @field_validator("depends_on")
    @classmethod
    def _dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_text(item, "depends_on", maximum=128) for item in value)
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("step dependencies must not repeat")
        return cleaned


class PlanProposal(PlanningModel):
    """Candidate graph emitted by a planner and checked by P7."""

    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    action: Literal["plan_new", "revise_draft"] = "plan_new"
    goal: str
    strategy_summary: str
    steps: tuple[PlanProposalStep, ...] = Field(default=(), max_length=3)
    requested_outputs: tuple[str, ...] = ()
    optional_suggestions: tuple[str, ...] = ()
    clarification_fields: tuple[str, ...] = ()

    @field_validator("goal", "strategy_summary")
    @classmethod
    def _proposal_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "proposal text"), maximum=8192)

    @field_validator("requested_outputs", "optional_suggestions", "clarification_fields")
    @classmethod
    def _proposal_lists(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        cleaned = tuple(
            _text(item, getattr(info, "field_name", "proposal item"), maximum=2048)
            for item in value
        )
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("proposal collections must not repeat")
        return cleaned

    @model_validator(mode="after")
    def _unique_keys(self) -> PlanProposal:
        keys = tuple(item.key for item in self.steps)
        if len(keys) != len(set(keys)):
            raise ValueError("proposal step keys must be unique")
        return self

    @property
    def proposal_hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


class PlanFeedback(PlanningModel):
    """Program-generated feedback passed to a later planning turn."""

    failed_nodes: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()
    reusable_artifacts: tuple[FrozenJsonObject, ...] = ()
    unsatisfied_outputs: tuple[str, ...] = ()
    prohibited_requests: tuple[str, ...] = ()
    remaining_budget: FrozenJsonObject = {}

    @field_validator(
        "failed_nodes",
        "error_codes",
        "unsatisfied_outputs",
        "prohibited_requests",
    )
    @classmethod
    def _feedback_lists(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        cleaned = tuple(
            _text(item, getattr(info, "field_name", "feedback item"), maximum=1024)
            for item in value
        )
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("feedback collections must not repeat")
        return cleaned

    @field_validator("reusable_artifacts")
    @classmethod
    def _artifacts(cls, value: tuple[FrozenJsonObject, ...]) -> tuple[FrozenJsonObject, ...]:
        return tuple(freeze_json_object(item) for item in value)

    @field_validator("remaining_budget", mode="before")
    @classmethod
    def _budget(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)


class PlanningRecord(PlanningModel):
    """Immutable evidence binding a candidate to one task revision."""

    schema_version: Literal[PLANNING_RECORD_SCHEMA_VERSION] = PLANNING_RECORD_SCHEMA_VERSION
    proposal_id: str
    conversation_id: str
    task_id: str
    source_turn_id: str
    task_revision: int = Field(ge=1)
    action: Literal["plan_new", "revise_draft"]
    original_goal: str
    normalized_constraints: FrozenJsonObject = {}
    candidate: PlanProposal
    validation_status: ValidationStatus
    validation_issues: tuple[str, ...] = ()
    feedback: PlanFeedback = Field(default_factory=PlanFeedback)
    request_hash: str
    compiled_plan_hash: str | None = None
    compiled_protocol_id: str | None = None
    source_task_id: str | None = None
    external_opt_result_id: str | None = None
    model_attempt_id: str | None = None
    created_at_utc: datetime
    record_hash: str

    @field_validator("proposal_id", "conversation_id", "task_id", "source_turn_id")
    @classmethod
    def _record_ids(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "record id"), maximum=256)

    @field_validator("original_goal")
    @classmethod
    def _original_goal(cls, value: str) -> str:
        return _text(value, "original_goal", maximum=8192)

    @field_validator("normalized_constraints", mode="before")
    @classmethod
    def _constraints(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)

    @field_validator("validation_issues")
    @classmethod
    def _issues(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_text(item, "validation issue", maximum=2048) for item in value)
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("validation issues must not repeat")
        return cleaned

    _request_hash = field_validator("request_hash")(_hash)
    _compiled_plan_hash = field_validator("compiled_plan_hash")(_optional_hash)

    @field_validator(
        "compiled_protocol_id", "source_task_id", "external_opt_result_id", "model_attempt_id"
    )
    @classmethod
    def _optional_record_fields(cls, value: str | None, info: object) -> str | None:
        return _optional_text(value, getattr(info, "field_name", "record field"), maximum=512)

    @field_validator("created_at_utc")
    @classmethod
    def _created(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("record_hash")
    @classmethod
    def _record_hash(cls, value: str) -> str:
        return _hash(value, "record_hash")

    @model_validator(mode="after")
    def _hash_matches(self) -> PlanningRecord:
        verify_sha256(self.model_dump(mode="json", exclude={"record_hash"}), self.record_hash)
        if self.candidate.action != self.action:
            raise ValueError("planning record action does not match candidate action")
        return self

    @classmethod
    def create(cls, **values: object) -> PlanningRecord:
        values.setdefault("schema_version", PLANNING_RECORD_SCHEMA_VERSION)
        values["record_hash"] = _record_hash(cls, values, "record_hash")
        return cls(**values)


# Names used by callers that describe the candidate as a graph rather than a
# proposal.  They remain aliases, so there is only one serialized contract.
CandidatePlanStep = PlanProposalStep
CandidatePlan = PlanProposal

__all__ = [
    "CandidatePlan",
    "CandidatePlanStep",
    "PLANNING_RECORD_SCHEMA_VERSION",
    "PLANNING_SCHEMA_VERSION",
    "PlanFeedback",
    "PlanProposal",
    "PlanProposalStep",
    "PlanningRecord",
]
