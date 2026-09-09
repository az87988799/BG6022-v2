"""Versioned conversation, turn, intent, and provenance contracts for P7.

The language model is deliberately kept at the edge of these contracts.  A
model may propose an interpretation, but the application owns IDs, revisions,
source bindings, approvals, and every scientific value.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..orchestration.p7_versions import (
    P7_CONVERSATION_ENGINE,
    P7_SCHEMA_VERSION,
    PROMPT_VERSION,
    PROMPT_VERSION_V2,
    PROMPT_VERSION_V3,
    TURN_SCHEMA,
    TURN_SCHEMA_V2,
    TURN_SCHEMA_V3,
)
from ..orchestration.temporal import ensure_utc
from .hashing import sha256_hex, verify_sha256
from .ids import ConversationId, new_id
from .json_types import (
    FrozenJsonObject,
    FrozenJsonValue,
    freeze_json_object,
    freeze_json_value,
)


class P7Model(BaseModel):
    """Strict immutable P7 value object."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


class ConversationStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class TurnStatus(StrEnum):
    RECEIVED = "received"
    INTERPRETING = "interpreting"
    RESPONDING = "responding"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED = "cancelled"


class IntentKind(StrEnum):
    CHEMICAL_CALCULATION = "chemical_calculation"
    CHEMISTRY_QA = "chemistry_qa"
    GENERAL_QA = "general_qa"
    CONTEXT_QUERY = "context_query"


class CalculationAction(StrEnum):
    PLAN_NEW = "plan_new"
    REVISE_DRAFT = "revise_draft"
    REQUEST_EXECUTION = "request_execution"
    CANCEL_TASK = "cancel_task"


class ParameterSource(StrEnum):
    USER_EXPLICIT = "user_explicit"
    STRUCTURE_DERIVED = "structure_derived"
    POLICY_RECOMMENDED = "policy_recommended"
    USER_ACCEPTED_RECOMMENDATION = "user_accepted_recommendation"
    UNRESOLVED = "unresolved"


class MoleculeInputType(StrEnum):
    NAME = "name"
    CAS = "cas"
    CID = "cid"
    SMILES = "smiles"


class ResponseSource(StrEnum):
    PROGRAM = "program"
    BASELINE = "baseline"
    FAKE = "fake"
    DEEPSEEK = "deepseek_chat"
    USER = "user"


def _text(value: str, name: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank and contain no NUL")
    value = value.strip()
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value


def _hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("value must be lowercase SHA-256 hex")
    return value


def _record_hash(model_type: type[BaseModel], values: dict[str, object], field_name: str) -> str:
    probe = model_type.model_construct(**values, **{field_name: "0" * 64})
    return sha256_hex(probe.model_dump(mode="json", exclude={field_name}))


class ParameterValue(P7Model):
    """A value plus its explicit origin and the rule/structure it depends on."""

    name: str
    value: FrozenJsonValue
    value_type: str
    hard_constraint: bool = False
    source: ParameterSource
    source_reference: str
    source_fragment: str | None = None
    structure_hash: str | None = None
    rule_version: str | None = None
    recommendation_accepted: bool = False

    @field_validator("name", "value_type", "source_reference")
    @classmethod
    def _parameter_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "parameter"), maximum=256)

    @field_validator("source_fragment")
    @classmethod
    def _fragment(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "source_fragment", maximum=4096)

    @field_validator("structure_hash", "rule_version")
    @classmethod
    def _optional_metadata(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return (
            _hash(value)
            if getattr(info, "field_name", "") == "structure_hash"
            else _text(value, "rule_version")
        )

    @field_validator("value", mode="before")
    @classmethod
    def _freeze_value(cls, value: object) -> FrozenJsonValue:
        return freeze_json_value(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _source_invariants(self) -> ParameterValue:
        if self.source is ParameterSource.STRUCTURE_DERIVED and self.structure_hash is None:
            raise ValueError("structure-derived parameters require a structure hash")
        if self.source is ParameterSource.POLICY_RECOMMENDED and self.rule_version is None:
            raise ValueError("policy recommendations require a rule version")
        if self.source is ParameterSource.USER_ACCEPTED_RECOMMENDATION:
            if not self.recommendation_accepted or self.rule_version is None:
                raise ValueError("accepted recommendations require acceptance and a rule version")
        if self.source is ParameterSource.UNRESOLVED and self.recommendation_accepted:
            raise ValueError("unresolved parameters cannot be accepted")
        return self


class CalculationIntent(P7Model):
    intent: Literal[IntentKind.CHEMICAL_CALCULATION] = IntentKind.CHEMICAL_CALCULATION
    action: CalculationAction = CalculationAction.PLAN_NEW
    molecule_kind: MoleculeInputType | None = None
    molecule_value: str | None = None
    task_alias: str | None = None
    operations: tuple[str, ...] = ()
    charge: int | None = None
    multiplicity: int | None = None
    method: str | None = None
    environment: str | None = None
    hard_constraints: tuple[str, ...] = ()
    prohibited_requests: tuple[str, ...] = ()
    output_spec: FrozenJsonObject = {}
    missing_fields: tuple[str, ...] = ()
    requested_execution: bool = False
    parameter_evidence: FrozenJsonObject = {}
    # The planning proposal is an edge-contract payload.  ``None`` is
    # excluded from serialized legacy intents so historical request/turn
    # hashes remain byte-for-byte compatible.
    plan_proposal: FrozenJsonObject | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("molecule_value", "task_alias", "method", "environment")
    @classmethod
    def _optional_text(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _text(value, getattr(info, "field_name", "value"), maximum=4096)
        )

    @field_validator("hard_constraints", "prohibited_requests", "missing_fields")
    @classmethod
    def _text_collection(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        cleaned = tuple(
            _text(item, getattr(info, "field_name", "item"), maximum=512) for item in value
        )
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("intent text collections must not contain duplicates")
        return cleaned

    @field_validator("operations")
    @classmethod
    def _operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_text(item, "operation", maximum=64).casefold() for item in value)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("operations must not contain duplicates")
        return cleaned

    @field_validator("output_spec", mode="before")
    @classmethod
    def _output_object(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)

    @field_validator("parameter_evidence", mode="before")
    @classmethod
    def _parameter_evidence(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)

    @field_validator("plan_proposal", mode="before")
    @classmethod
    def _plan_proposal(cls, value: object) -> FrozenJsonObject | None:
        return None if value is None else freeze_json_object(value)


class ChemistryQAIntent(P7Model):
    intent: Literal[IntentKind.CHEMISTRY_QA] = IntentKind.CHEMISTRY_QA
    question: str
    task_alias: str | None = None
    answer_draft: str | None = None
    requires_task_context: bool = False
    cited_fact_aliases: tuple[str, ...] = ()

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        return _text(value, "question", maximum=8192)

    @field_validator("task_alias", "answer_draft")
    @classmethod
    def _optional_qa_text(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _text(value, getattr(info, "field_name", "text"), maximum=16_384)
        )

    @field_validator("cited_fact_aliases")
    @classmethod
    def _fact_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_text(item, "fact alias", maximum=128) for item in value)


class GeneralQAIntent(P7Model):
    intent: Literal[IntentKind.GENERAL_QA] = IntentKind.GENERAL_QA
    question: str
    answer_draft: str | None = None

    @field_validator("question")
    @classmethod
    def _general_question(cls, value: str) -> str:
        return _text(value, "question", maximum=8192)

    @field_validator("answer_draft")
    @classmethod
    def _general_answer(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "answer_draft", maximum=16_384)


class ContextQueryIntent(P7Model):
    intent: Literal[IntentKind.CONTEXT_QUERY] = IntentKind.CONTEXT_QUERY
    query: str
    task_alias: str | None = None
    output_spec: FrozenJsonObject = {}
    requested_display_change: bool = False

    @field_validator("query")
    @classmethod
    def _query(cls, value: str) -> str:
        return _text(value, "query", maximum=8192)

    @field_validator("task_alias")
    @classmethod
    def _query_alias(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "task_alias", maximum=128)

    @field_validator("output_spec", mode="before")
    @classmethod
    def _query_output(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)


Subrequest = Annotated[
    CalculationIntent | ChemistryQAIntent | GeneralQAIntent | ContextQueryIntent,
    Field(discriminator="intent"),
]


class TurnInterpretation(P7Model):
    """The only model output accepted by the application boundary."""

    schema_version: Literal[TURN_SCHEMA, TURN_SCHEMA_V2, TURN_SCHEMA_V3] = TURN_SCHEMA
    prompt_version: Literal[PROMPT_VERSION, PROMPT_VERSION_V2, PROMPT_VERSION_V3] = PROMPT_VERSION
    subrequests: tuple[Subrequest, ...] = Field(min_length=1, max_length=4)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: ResponseSource = ResponseSource.BASELINE

    @model_validator(mode="after")
    def _request_limits(self) -> TurnInterpretation:
        if (self.schema_version, self.prompt_version) not in {
            (TURN_SCHEMA, PROMPT_VERSION),
            (TURN_SCHEMA_V2, PROMPT_VERSION_V2),
            (TURN_SCHEMA_V3, PROMPT_VERSION_V3),
        }:
            raise ValueError("turn schema and prompt version are not a supported pair")
        new_calculations = sum(
            item.intent is IntentKind.CHEMICAL_CALCULATION
            and item.action in {CalculationAction.PLAN_NEW, CalculationAction.REVISE_DRAFT}
            for item in self.subrequests
        )
        if new_calculations > 1:
            raise ValueError("a turn may contain at most one new calculation request")
        if self.schema_version in {TURN_SCHEMA_V2, TURN_SCHEMA_V3}:
            for item in self.subrequests:
                if isinstance(item, (ChemistryQAIntent, GeneralQAIntent)) and not (
                    item.answer_draft and item.answer_draft.strip()
                ):
                    raise ValueError("v2 QA responses require a non-empty answer_draft")
        return self


class ContextTurn(P7Model):
    sequence_no: int = Field(ge=1)
    user_text: str
    response_text: str
    turn_id: str | None = None

    @field_validator("user_text", "response_text")
    @classmethod
    def _turn_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "turn text"), maximum=16_384)


class ContextTask(P7Model):
    task_alias: str
    task_id: str
    state: str
    revision: int = Field(ge=1)
    updated_at_utc: datetime
    request_summary: FrozenJsonObject = {}

    @field_validator("task_alias", "task_id", "state")
    @classmethod
    def _task_context_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "task field"), maximum=256)

    @field_validator("updated_at_utc")
    @classmethod
    def _task_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("request_summary", mode="before")
    @classmethod
    def _task_summary(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)


class ContextSnapshot(P7Model):
    """The bounded, deterministic input frozen for one model attempt."""

    schema_version: Literal[TURN_SCHEMA] = TURN_SCHEMA
    conversation_id: ConversationId
    turn_sequence_no: int = Field(ge=1)
    current_message: str
    active_task_alias: str | None = None
    tasks: tuple[ContextTask, ...] = Field(max_length=8)
    recent_turns: tuple[ContextTurn, ...] = Field(max_length=12)
    facts: FrozenJsonObject = {}
    encoded_size_bytes: int = Field(ge=0, le=65_536)
    snapshot_hash: str

    _hash = field_validator("snapshot_hash")(_hash)

    @field_validator("current_message")
    @classmethod
    def _message(cls, value: str) -> str:
        return _text(value, "current_message", maximum=65_536)

    @field_validator("active_task_alias")
    @classmethod
    def _active_alias(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "active_task_alias", maximum=128)

    @field_validator("facts", mode="before")
    @classmethod
    def _facts(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)

    @model_validator(mode="after")
    def _snapshot_hash_matches(self) -> ContextSnapshot:
        verify_sha256(self.model_dump(mode="json", exclude={"snapshot_hash"}), self.snapshot_hash)
        return self

    @classmethod
    def create(cls, **values: object) -> ContextSnapshot:
        values.setdefault("schema_version", TURN_SCHEMA)
        values["snapshot_hash"] = _hash_for_values(cls, values, "snapshot_hash")
        return cls(**values)


def _hash_for_values(
    model_type: type[BaseModel], values: dict[str, object], field_name: str
) -> str:
    probe_values = dict(values)
    probe_values[field_name] = "0" * 64
    probe = model_type.model_construct(**probe_values)
    return sha256_hex(probe.model_dump(mode="json", exclude={field_name}))


class ConversationState(P7Model):
    conversation_id: ConversationId
    schema_version: Literal[P7_SCHEMA_VERSION] = P7_SCHEMA_VERSION
    engine_version: Literal[P7_CONVERSATION_ENGINE] = P7_CONVERSATION_ENGINE
    revision: int = Field(ge=1)
    status: ConversationStatus = ConversationStatus.OPEN
    model_calls: int = Field(default=0, ge=0)
    model_call_budget: int = Field(default=120, ge=0)
    current_turn_id: str | None = None
    active_task_id: str | None = None
    next_turn_sequence: int = Field(default=1, ge=1)
    created_at_utc: datetime
    updated_at_utc: datetime
    state_hash: str

    _hash = field_validator("state_hash")(_hash)

    @field_validator("current_turn_id", "active_task_id")
    @classmethod
    def _optional_id_text(cls, value: str | None, info: object) -> str | None:
        return (
            None if value is None else _text(value, getattr(info, "field_name", "id"), maximum=128)
        )

    @field_validator("created_at_utc", "updated_at_utc")
    @classmethod
    def _state_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _state_hash_matches(self) -> ConversationState:
        verify_sha256(self.model_dump(mode="json", exclude={"state_hash"}), self.state_hash)
        if self.status is ConversationStatus.CLOSED and self.current_turn_id is not None:
            raise ValueError("closed conversation cannot have an active turn")
        return self

    @classmethod
    def create(
        cls,
        *,
        conversation_id: ConversationId | None = None,
        now: datetime,
        model_call_budget: int = 120,
    ) -> ConversationState:
        values = {
            "conversation_id": conversation_id or new_id(ConversationId),
            "schema_version": P7_SCHEMA_VERSION,
            "engine_version": P7_CONVERSATION_ENGINE,
            "revision": 1,
            "status": ConversationStatus.OPEN,
            "model_calls": 0,
            "model_call_budget": model_call_budget,
            "current_turn_id": None,
            "active_task_id": None,
            "next_turn_sequence": 1,
            "created_at_utc": ensure_utc(now),
            "updated_at_utc": ensure_utc(now),
        }
        return cls(**values, state_hash=_hash_for_values(cls, values, "state_hash"))


class TurnRecord(P7Model):
    turn_id: str
    conversation_id: ConversationId
    sequence_no: int = Field(ge=1)
    status: TurnStatus
    user_text: str
    context_snapshot: ContextSnapshot
    interpretation: TurnInterpretation | None = None
    response_text: str | None = None
    response_payload: FrozenJsonObject = {}
    response_source: ResponseSource = ResponseSource.PROGRAM
    task_ids: tuple[str, ...] = ()
    error_code: str | None = None
    created_at_utc: datetime
    updated_at_utc: datetime
    record_hash: str

    _hash = field_validator("record_hash")(_hash)

    @field_validator("turn_id")
    @classmethod
    def _turn_id(cls, value: str) -> str:
        return _text(value, "turn_id", maximum=128)

    @field_validator("user_text")
    @classmethod
    def _user_text(cls, value: str) -> str:
        return _text(value, "user_text", maximum=65_536)

    @field_validator("response_text", "error_code")
    @classmethod
    def _optional_record_text(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _text(value, getattr(info, "field_name", "text"), maximum=16_384)
        )

    @field_validator("response_payload", mode="before")
    @classmethod
    def _payload(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)

    @field_validator("created_at_utc", "updated_at_utc")
    @classmethod
    def _record_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _record_hash_matches(self) -> TurnRecord:
        verify_sha256(self.model_dump(mode="json", exclude={"record_hash"}), self.record_hash)
        return self

    @classmethod
    def create(cls, **values: object) -> TurnRecord:
        values.setdefault("response_payload", {})
        values["record_hash"] = _hash_for_values(cls, values, "record_hash")
        return cls(**values)


class ResponseRecord(P7Model):
    response_id: str
    turn_id: str
    conversation_id: ConversationId
    subrequest_index: int = Field(ge=0)
    intent: IntentKind
    source: ResponseSource
    text: str
    payload: FrozenJsonObject = {}
    delivery_id: str | None = None
    created_at_utc: datetime
    response_hash: str

    _hash = field_validator("response_hash")(_hash)

    @field_validator("response_id", "turn_id")
    @classmethod
    def _response_ids(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "id"), maximum=128)

    @field_validator("text")
    @classmethod
    def _response_text(cls, value: str) -> str:
        return _text(value, "response text", maximum=65_536)

    @field_validator("delivery_id")
    @classmethod
    def _delivery(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "delivery_id", maximum=128)

    @field_validator("payload", mode="before")
    @classmethod
    def _response_payload(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)

    @field_validator("created_at_utc")
    @classmethod
    def _response_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _response_hash_matches(self) -> ResponseRecord:
        verify_sha256(self.model_dump(mode="json", exclude={"response_hash"}), self.response_hash)
        return self

    @classmethod
    def create(cls, **values: object) -> ResponseRecord:
        values.setdefault("payload", {})
        values["response_hash"] = _hash_for_values(cls, values, "response_hash")
        return cls(**values)


__all__ = [
    "CalculationAction",
    "CalculationIntent",
    "ChemistryQAIntent",
    "ContextQueryIntent",
    "ContextSnapshot",
    "ContextTask",
    "ContextTurn",
    "ConversationState",
    "ConversationStatus",
    "GeneralQAIntent",
    "IntentKind",
    "MoleculeInputType",
    "P7Model",
    "ParameterSource",
    "ParameterValue",
    "ResponseRecord",
    "ResponseSource",
    "Subrequest",
    "TurnInterpretation",
    "TurnRecord",
    "TurnStatus",
]
