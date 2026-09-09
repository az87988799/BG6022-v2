"""Sparse, versioned P7 intake contracts.

The v4 boundary carries only what the user said in the current turn.  Default
values, historical values, provenance, task IDs, revisions, and approvals are
owned by the application layer and are intentionally absent from this model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ..orchestration.p7_versions import PROMPT_VERSION_V4, TURN_SCHEMA_V4
from .json_types import FrozenJsonObject, FrozenJsonValue, freeze_json_object, freeze_json_value
from .p7_conversation import IntentKind, P7Model, ResponseSource

INTAKE_SCHEMA_VERSION_V4 = TURN_SCHEMA_V4
INTAKE_PROMPT_VERSION_V4 = PROMPT_VERSION_V4


def _text(value: str, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank and contain no NUL")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value


class ParameterEvidenceV4(P7Model):
    """The only evidence a model may attach to a current-turn change."""

    quote: str

    @field_validator("quote")
    @classmethod
    def _quote(cls, value: str) -> str:
        return _text(value, "evidence.quote", maximum=4096)


class FieldChangeV4(P7Model):
    """One sparse user change; defaults and inherited values are not changes."""

    field: Literal[
        "molecule",
        "molecule_kind",
        "molecule_value",
        "method",
        "environment",
        "charge",
        "multiplicity",
        "operations",
        "hard_constraint",
        "prohibited_request",
    ]
    op: Literal["set", "reset_default"] = "set"
    value: FrozenJsonValue | None = None
    evidence: ParameterEvidenceV4

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> FrozenJsonValue | None:
        return None if value is None else freeze_json_value(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _change_shape(self) -> FieldChangeV4:
        if self.op == "set" and self.value is None:
            raise ValueError("a set change requires a non-null value")
        if self.op == "reset_default" and self.value is not None:
            raise ValueError("reset_default cannot carry a value")
        if self.field in {"charge", "multiplicity"} and self.op == "set":
            if type(self.value) is not int:
                raise ValueError(f"{self.field} must be an integer, not a boolean or string")
            if self.field == "multiplicity" and self.value < 1:
                raise ValueError("multiplicity must be positive")
        if self.field in {"method", "environment", "molecule_kind", "molecule_value"}:
            if self.op == "set" and not isinstance(self.value, str):
                raise ValueError(f"{self.field} must be a string")
        if self.field == "molecule" and self.op == "set":
            if not isinstance(self.value, Mapping):
                raise ValueError("molecule change must be an object")
            if set(self.value) - {"kind", "value", "raw"}:
                raise ValueError("molecule change contains unknown fields")
            if not isinstance(self.value.get("kind"), str) or not isinstance(
                self.value.get("value"), str
            ):
                raise ValueError("molecule change requires string kind and value")
            if self.value.get("kind") not in {"name", "cas", "cid", "smiles"}:
                raise ValueError("molecule change kind is not supported")
        if self.field == "operations" and self.op == "set":
            if not isinstance(self.value, (list, tuple)) or not all(
                isinstance(item, str) for item in self.value
            ):
                raise ValueError("operations must be an array of strings")
        if self.field in {"hard_constraint", "prohibited_request"} and self.op == "set":
            if not isinstance(self.value, str):
                raise ValueError(f"{self.field} must be a string")
        return self


class OutputQuantityPatchV4(P7Model):
    kind: str
    required: bool = True
    source_selector: str | None = None
    unit: str | None = None
    label: str | None = None

    @field_validator("kind", "source_selector", "unit", "label")
    @classmethod
    def _quantity_text(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _text(value, getattr(info, "field_name", "quantity"), maximum=256)
        )


class OutputPatchV4(P7Model):
    """Sparse display-only output modification."""

    quantities: tuple[OutputQuantityPatchV4, ...] | None = Field(
        default=None, min_length=1, max_length=16
    )
    language: Literal["zh", "en"] | None = None
    style: Literal["concise", "detailed"] | None = None
    layout: Literal["prose", "table", "json"] | None = None
    units: FrozenJsonObject | None = None
    precision: int | None = Field(default=None, ge=0, le=12)
    artifacts: tuple[str, ...] | None = None
    include_evidence: bool | None = None

    @field_validator("units", mode="before")
    @classmethod
    def _units(cls, value: object) -> FrozenJsonObject | None:
        return None if value is None else freeze_json_object(value)

    @field_validator("artifacts")
    @classmethod
    def _artifacts(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        cleaned = tuple(_text(item, "artifact", maximum=128) for item in value)
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("output artifacts must be unique")
        return cleaned

    @model_validator(mode="after")
    def _quantities_are_sparse(self) -> OutputPatchV4:
        if "quantities" in self.model_fields_set and self.quantities is None:
            raise ValueError("output_patch.quantities cannot be null")
        return self


class CalculationIntentV4(P7Model):
    """Calculation intent that reports only current-turn changes."""

    intent: Literal[IntentKind.CHEMICAL_CALCULATION] = IntentKind.CHEMICAL_CALCULATION
    action: Literal[
        "plan_new", "revise_draft", "request_execution", "cancel_task"
    ] = "plan_new"
    task_alias: str | None = None
    changes: tuple[FieldChangeV4, ...] = Field(default=(), max_length=12)
    plan_proposal: FrozenJsonObject | None = None
    output_patch: OutputPatchV4 | None = None
    requested_execution: bool = False

    @field_validator("task_alias")
    @classmethod
    def _alias(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "task_alias", maximum=256)

    @model_validator(mode="after")
    def _unique_fields(self) -> CalculationIntentV4:
        fields = tuple(item.field for item in self.changes)
        if len(fields) != len(set(fields)):
            raise ValueError("a v4 turn may contain at most one final change per field")
        return self

    @field_validator("plan_proposal", mode="before")
    @classmethod
    def _proposal(cls, value: object) -> FrozenJsonObject | None:
        return None if value is None else freeze_json_object(value)


class ChemistryQAIntentV4(P7Model):
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
    def _optional_text(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _text(value, getattr(info, "field_name", "text"), maximum=16_384)
        )

    @field_validator("cited_fact_aliases")
    @classmethod
    def _aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_text(item, "cited_fact_alias", maximum=128) for item in value)


class GeneralQAIntentV4(P7Model):
    intent: Literal[IntentKind.GENERAL_QA] = IntentKind.GENERAL_QA
    question: str
    answer_draft: str | None = None

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        return _text(value, "question", maximum=8192)

    @field_validator("answer_draft")
    @classmethod
    def _answer(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "answer_draft", maximum=16_384)


class ContextQueryIntentV4(P7Model):
    intent: Literal[IntentKind.CONTEXT_QUERY] = IntentKind.CONTEXT_QUERY
    query: str
    task_alias: str | None = None
    output_patch: OutputPatchV4 | None = None
    requested_display_change: bool = False

    @field_validator("query")
    @classmethod
    def _query(cls, value: str) -> str:
        return _text(value, "query", maximum=8192)

    @field_validator("task_alias")
    @classmethod
    def _alias(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "task_alias", maximum=256)


SubrequestV4 = Annotated[
    CalculationIntentV4
    | ChemistryQAIntentV4
    | GeneralQAIntentV4
    | ContextQueryIntentV4,
    Field(discriminator="intent"),
]


class TurnInterpretationV4(P7Model):
    """Strict v4 interpretation envelope."""

    schema_version: Literal[TURN_SCHEMA_V4] = TURN_SCHEMA_V4
    prompt_version: Literal[PROMPT_VERSION_V4] = PROMPT_VERSION_V4
    subrequests: tuple[SubrequestV4, ...] = Field(min_length=1, max_length=4)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: ResponseSource = ResponseSource.BASELINE

    @model_validator(mode="after")
    def _request_limits(self) -> TurnInterpretationV4:
        new_calculations = sum(
            item.intent is IntentKind.CHEMICAL_CALCULATION
            and item.action in {"plan_new", "revise_draft"}
            for item in self.subrequests
            if isinstance(item, CalculationIntentV4)
        )
        if new_calculations > 1:
            raise ValueError("a turn may contain at most one new calculation request")
        for item in self.subrequests:
            if isinstance(item, (ChemistryQAIntentV4, GeneralQAIntentV4)) and not (
                item.answer_draft and item.answer_draft.strip()
            ):
                raise ValueError("v4 QA responses require a non-empty answer_draft")
        return self


__all__ = [
    "CalculationIntentV4",
    "ChemistryQAIntentV4",
    "ContextQueryIntentV4",
    "FieldChangeV4",
    "INTAKE_PROMPT_VERSION_V4",
    "INTAKE_SCHEMA_VERSION_V4",
    "GeneralQAIntentV4",
    "OutputPatchV4",
    "OutputQuantityPatchV4",
    "ParameterEvidenceV4",
    "SubrequestV4",
    "TurnInterpretationV4",
]
