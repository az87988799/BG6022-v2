"""Calculation, output, task, and delivery contracts owned by P7."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..orchestration.p7_versions import (
    CAPABILITY_VIEW_SCHEMA,
    OUTPUT_SCHEMA,
    P7_SCHEMA_VERSION,
    P7_TASK_ENGINE,
)
from ..orchestration.temporal import ensure_utc
from .hashing import sha256_hex, verify_sha256
from .json_types import (
    FrozenJsonObject,
    FrozenJsonValue,
    freeze_json_object,
    freeze_json_value,
)
from .p7_conversation import MoleculeInputType, P7Model, ParameterValue


class TaskPhase(StrEnum):
    DRAFT = "draft"
    NEEDS_CLARIFICATION = "needs_clarification"
    PLAN_READY = "plan_ready"
    IDENTITY_PENDING = "identity_pending"
    EXECUTION_PENDING = "execution_pending"
    EXECUTING = "executing"
    ASSESSING = "assessing"
    RESULT_READY = "result_ready"
    ENDED_WITHOUT_RESULT = "ended_without_result"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class StopReason(StrEnum):
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    SUPERSEDED = "superseded"


class OutputKind(StrEnum):
    OPT_FINAL_ELECTRONIC_ENERGY = "opt_final_electronic_energy"
    INDEPENDENT_SP_ELECTRONIC_ENERGY = "independent_sp_electronic_energy"
    VIBRATIONAL_FREQUENCIES = "vibrational_frequencies"
    LOCAL_MINIMUM_SUPPORT = "local_minimum_support"
    OPTIMIZED_GEOMETRY = "optimized_geometry"
    EXECUTION_STATUS = "execution_status"
    SCIENTIFIC_STATUS = "scientific_status"
    REPORT = "report"
    EXISTING_ENERGY_DIFFERENCE = "existing_energy_difference"


class FulfillmentStatus(StrEnum):
    PROVIDED = "provided"
    PENDING = "pending"
    NOT_AVAILABLE = "not_available"
    UNSUPPORTED = "unsupported"
    INTEGRITY_ERROR = "integrity_error"


class OverallDeliveryStatus(StrEnum):
    FULFILLED = "fulfilled"
    PARTIAL = "partial"
    PENDING = "pending"
    UNMET = "unmet"


class ValidationStatus(StrEnum):
    VALID = "valid"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"


def _text(value: str, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank and contain no NUL")
    value = value.strip()
    if len(value) > maximum:
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


def _make_hash(model_type: type[BaseModel], values: dict[str, object], field_name: str) -> str:
    candidate = dict(values)
    candidate[field_name] = "0" * 64
    probe = model_type.model_construct(**candidate)
    return sha256_hex(probe.model_dump(mode="json", exclude={field_name}))


class OutputQuantity(P7Model):
    kind: OutputKind
    required: bool = True
    source_selector: str | None = None
    unit: str | None = None
    label: str | None = None

    @field_validator("source_selector", "unit", "label")
    @classmethod
    def _optional_quantity_text(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _text(value, getattr(info, "field_name", "quantity"), maximum=256)
        )


class OutputSpec(P7Model):
    """Strict rendering request independent from the calculation plan."""

    schema_version: Literal[OUTPUT_SCHEMA] = OUTPUT_SCHEMA
    quantities: tuple[OutputQuantity, ...] = Field(min_length=1, max_length=16)
    language: Literal["zh", "en"] = "zh"
    style: Literal["concise", "detailed"] = "concise"
    layout: Literal["prose", "table", "json"] = "prose"
    units: FrozenJsonObject = {}
    precision: int | None = Field(default=None, ge=0, le=12)
    artifacts: tuple[str, ...] = ()
    include_evidence: bool = False
    output_spec_hash: str

    _hash = field_validator("output_spec_hash")(_hash)

    @field_validator("units", mode="before")
    @classmethod
    def _units(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)

    @field_validator("artifacts")
    @classmethod
    def _artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_text(item, "artifact kind", maximum=128) for item in value)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("artifact kinds must be unique")
        return cleaned

    @model_validator(mode="after")
    def _output_hash_matches(self) -> OutputSpec:
        verify_sha256(
            self.model_dump(mode="json", exclude={"output_spec_hash"}), self.output_spec_hash
        )
        kinds = [item.kind for item in self.quantities]
        if len(set(kinds)) != len(kinds):
            raise ValueError("output quantities must not repeat a kind")
        return self

    @classmethod
    def create(
        cls,
        *,
        quantities: tuple[OutputQuantity, ...] | list[OutputQuantity],
        language: Literal["zh", "en"] = "zh",
        style: Literal["concise", "detailed"] = "concise",
        layout: Literal["prose", "table", "json"] = "prose",
        units: dict[str, object] | None = None,
        precision: int | None = None,
        artifacts: tuple[str, ...] | list[str] = (),
        include_evidence: bool = False,
    ) -> OutputSpec:
        values = {
            "schema_version": OUTPUT_SCHEMA,
            "quantities": tuple(quantities),
            "language": language,
            "style": style,
            "layout": layout,
            "units": {} if units is None else units,
            "precision": precision,
            "artifacts": tuple(artifacts),
            "include_evidence": include_evidence,
        }
        return cls(**values, output_spec_hash=_make_hash(cls, values, "output_spec_hash"))

    @classmethod
    def default(cls) -> OutputSpec:
        return cls.create(
            quantities=(
                OutputQuantity(kind=OutputKind.INDEPENDENT_SP_ELECTRONIC_ENERGY),
                OutputQuantity(kind=OutputKind.OPTIMIZED_GEOMETRY, required=False),
            )
        )


class CalculationRequest(P7Model):
    """Normalized user request with every important field carrying provenance."""

    schema_version: Literal[P7_SCHEMA_VERSION] = P7_SCHEMA_VERSION
    request_id: str
    source_turn_id: str
    molecule_kind: MoleculeInputType | None = None
    molecule_value: str | None = None
    charge: ParameterValue | None = None
    multiplicity: ParameterValue | None = None
    operations: tuple[str, ...] = ()
    method: ParameterValue | None = None
    environment: ParameterValue | None = None
    hard_constraints: tuple[str, ...] = ()
    prohibited_requests: tuple[str, ...] = ()
    output_spec: OutputSpec
    preview_only: bool = True
    request_hash: str

    _hash = field_validator("request_hash")(_hash)

    @field_validator("request_id", "source_turn_id")
    @classmethod
    def _ids(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "id"), maximum=128)

    @field_validator("molecule_value")
    @classmethod
    def _molecule_value(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "molecule_value", maximum=4096)

    @field_validator("operations")
    @classmethod
    def _operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_text(item, "operation", maximum=64).casefold() for item in value)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("operations must not repeat")
        return cleaned

    @field_validator("hard_constraints", "prohibited_requests")
    @classmethod
    def _constraints(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        cleaned = tuple(
            _text(item, getattr(info, "field_name", "constraint"), maximum=512) for item in value
        )
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("constraints must not repeat")
        return cleaned

    @model_validator(mode="after")
    def _request_hash_matches(self) -> CalculationRequest:
        verify_sha256(self.model_dump(mode="json", exclude={"request_hash"}), self.request_hash)
        if self.molecule_kind is None and self.molecule_value is not None:
            raise ValueError("molecule value requires an input kind")
        if self.molecule_kind is not None and self.molecule_value is None:
            raise ValueError("molecule kind requires an input value")
        return self

    @classmethod
    def create(cls, **values: object) -> CalculationRequest:
        values.setdefault("schema_version", P7_SCHEMA_VERSION)
        values["request_hash"] = _make_hash(cls, values, "request_hash")
        return cls(**values)


class PlanNode(P7Model):
    node_id: str
    kind: Literal["opt", "freq", "sp"]
    depends_on: tuple[str, ...] = ()
    geometry_source: Literal["initial_geometry", "optimized_geometry"]
    method_profile_id: str
    budget: FrozenJsonObject

    @field_validator("node_id", "method_profile_id")
    @classmethod
    def _node_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "node"), maximum=256)

    @field_validator("budget", mode="before")
    @classmethod
    def _budget(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)


class CalculationPlan(P7Model):
    """Program-generated plan bound to a catalog and deterministic protocol."""

    schema_version: Literal[P7_SCHEMA_VERSION] = P7_SCHEMA_VERSION
    engine_version: Literal[P7_TASK_ENGINE] = P7_TASK_ENGINE
    capability_view_schema: Literal[CAPABILITY_VIEW_SCHEMA] = CAPABILITY_VIEW_SCHEMA
    capability_id: str
    capability_version: str
    capability_hash: str
    request_hash: str
    validation_hash: str
    output_spec_hash: str
    method_profile_id: str
    method_profile_hash: str
    environment: Literal["gas"]
    protocol_id: str
    protocol_hash: str
    registry_snapshot_id: str
    registry_snapshot_hash: str
    nodes: tuple[PlanNode, ...] = Field(min_length=1, max_length=3)
    expected_outputs: tuple[OutputKind, ...] = Field(min_length=1, max_length=16)
    applicability: FrozenJsonObject
    resources: FrozenJsonObject
    scope_note: str
    plan_hash: str

    _hashes = field_validator(
        "capability_hash",
        "request_hash",
        "validation_hash",
        "output_spec_hash",
        "method_profile_hash",
        "protocol_hash",
        "registry_snapshot_hash",
        "plan_hash",
    )(_hash)

    @field_validator(
        "capability_id",
        "capability_version",
        "method_profile_id",
        "protocol_id",
        "registry_snapshot_id",
    )
    @classmethod
    def _plan_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "plan field"), maximum=256)

    @field_validator("applicability", "resources", mode="before")
    @classmethod
    def _plan_objects(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("scope_note")
    @classmethod
    def _scope(cls, value: str) -> str:
        return _text(value, "scope_note", maximum=2048)

    @model_validator(mode="after")
    def _plan_invariants(self) -> CalculationPlan:
        if len({node.node_id for node in self.nodes}) != len(self.nodes):
            raise ValueError("plan node IDs must be unique")
        known = {node.node_id for node in self.nodes}
        if any(dep not in known for node in self.nodes for dep in node.depends_on):
            raise ValueError("plan dependency is unknown")
        verify_sha256(self.model_dump(mode="json", exclude={"plan_hash"}), self.plan_hash)
        return self

    @classmethod
    def create(cls, **values: object) -> CalculationPlan:
        values.setdefault("schema_version", P7_SCHEMA_VERSION)
        values.setdefault("engine_version", P7_TASK_ENGINE)
        values.setdefault("capability_view_schema", CAPABILITY_VIEW_SCHEMA)
        values["plan_hash"] = _make_hash(cls, values, "plan_hash")
        return cls(**values)


class PlanValidation(P7Model):
    status: ValidationStatus
    valid: bool
    issues: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    unsupported_requests: tuple[str, ...] = ()
    clarification_question: str | None = None
    request_hash: str
    validation_hash: str

    _hashes = field_validator("request_hash", "validation_hash")(_hash)

    @field_validator("issues", "missing_fields", "unsupported_requests")
    @classmethod
    def _validation_lists(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        cleaned = tuple(
            _text(item, getattr(info, "field_name", "validation item"), maximum=1024)
            for item in value
        )
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("validation lists must not repeat")
        return cleaned

    @field_validator("clarification_question")
    @classmethod
    def _clarification(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "clarification_question", maximum=2048)

    @model_validator(mode="after")
    def _validation_invariants(self) -> PlanValidation:
        if self.valid != (self.status is ValidationStatus.VALID):
            raise ValueError("validation valid flag does not match status")
        if self.status is ValidationStatus.NEEDS_CLARIFICATION and not self.clarification_question:
            raise ValueError("clarification status requires a question")
        verify_sha256(
            self.model_dump(mode="json", exclude={"validation_hash"}), self.validation_hash
        )
        return self

    @classmethod
    def create(cls, **values: object) -> PlanValidation:
        values["validation_hash"] = _make_hash(cls, values, "validation_hash")
        return cls(**values)


class Fulfillment(P7Model):
    kind: OutputKind
    required: bool
    status: FulfillmentStatus
    source_selector: str | None = None
    value: FrozenJsonValue = None
    raw_value_token: str | None = None
    unit: str | None = None
    artifact: FrozenJsonObject | None = None
    reason: str | None = None
    evidence: tuple[str, ...] = ()

    @field_validator("source_selector", "raw_value_token", "unit", "reason")
    @classmethod
    def _fulfillment_text(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _text(value, getattr(info, "field_name", "fulfillment"), maximum=2048)
        )

    @field_validator("value", mode="before")
    @classmethod
    def _fulfillment_value(cls, value: object) -> FrozenJsonValue:
        return freeze_json_value(value)  # type: ignore[return-value]

    @field_validator("artifact", mode="before")
    @classmethod
    def _artifact(cls, value: object) -> FrozenJsonObject | None:
        return None if value is None else freeze_json_object(value)

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_text(item, "evidence", maximum=256) for item in value)


class DeliveryRecord(P7Model):
    delivery_id: str
    task_id: str
    delivery_version: int = Field(ge=1)
    output_spec: OutputSpec
    source: FrozenJsonObject
    fulfillment: tuple[Fulfillment, ...] = Field(min_length=1, max_length=16)
    overall_status: OverallDeliveryStatus
    created_at_utc: datetime
    source_hash: str
    delivery_hash: str

    _hashes = field_validator("source_hash", "delivery_hash")(_hash)

    @field_validator("delivery_id", "task_id")
    @classmethod
    def _delivery_ids(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "delivery id"), maximum=128)

    @field_validator("source", mode="before")
    @classmethod
    def _source(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("created_at_utc")
    @classmethod
    def _delivery_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _delivery_invariants(self) -> DeliveryRecord:
        verify_sha256(self.model_dump(mode="json", exclude={"delivery_hash"}), self.delivery_hash)
        if len({item.kind for item in self.fulfillment}) != len(self.fulfillment):
            raise ValueError("delivery fulfillment kinds must be unique")
        return self

    @classmethod
    def create(cls, **values: object) -> DeliveryRecord:
        values["source_hash"] = _make_hash(
            _DeliverySourceHash,
            {"source": values.get("source", {})},
            "source_hash",
        )
        values["delivery_hash"] = _make_hash(cls, values, "delivery_hash")
        return cls(**values)


class RenderedResultView(P7Model):
    """A display projection derived from, but distinct from, a delivery.

    A user may change layout, precision, language, or selected quantities after
    execution.  Those changes are presentation state only and must never
    mutate the immutable scientific DeliveryRecord.
    """

    schema_version: Literal["p7.rendered-result-view.v1"] = "p7.rendered-result-view.v1"
    view_id: str
    task_id: str
    source_delivery_id: str
    source_delivery_hash: str
    output_spec: OutputSpec
    fulfillment: tuple[Fulfillment, ...] = Field(min_length=1, max_length=16)
    overall_status: OverallDeliveryStatus
    rendered_text: str
    created_at_utc: datetime
    view_hash: str

    _hashes = field_validator("source_delivery_hash", "view_hash")(_hash)

    @field_validator("view_id", "task_id", "source_delivery_id")
    @classmethod
    def _view_ids(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "view id"), maximum=256)

    @field_validator("rendered_text")
    @classmethod
    def _rendered_text(cls, value: str) -> str:
        return _text(value, "rendered_text", maximum=65536)

    @field_validator("created_at_utc")
    @classmethod
    def _view_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _view_invariants(self) -> RenderedResultView:
        verify_sha256(self.model_dump(mode="json", exclude={"view_hash"}), self.view_hash)
        if len({item.kind for item in self.fulfillment}) != len(self.fulfillment):
            raise ValueError("rendered fulfillment kinds must be unique")
        return self

    @classmethod
    def create(cls, **values: object) -> RenderedResultView:
        values["view_hash"] = _make_hash(cls, values, "view_hash")
        return cls(**values)


class _DeliverySourceHash(P7Model):
    source: FrozenJsonObject
    source_hash: str = "0" * 64

    @field_validator("source", mode="before")
    @classmethod
    def _source_hash_object(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)


class TaskRecord(P7Model):
    task_id: str
    conversation_id: str
    alias: str
    revision: int = Field(ge=1)
    state: TaskPhase
    stop_reason: StopReason | None = None
    request: CalculationRequest | None = None
    plan: CalculationPlan | None = None
    validation: PlanValidation | None = None
    accepted_plan_hash: str | None = None
    accepted_output_spec_hash: str | None = None
    delivery_output_spec: OutputSpec | None = None
    p4_run_id: str | None = None
    p5_run_id: str | None = None
    p6_run_id: str | None = None
    clarification_count: int = Field(default=0, ge=0)
    no_progress_count: int = Field(default=0, ge=0)
    current_delivery_id: str | None = None
    # v5 preparation is additive to the v4 task projection.  Zero means the
    # task has not entered the snapshot-based preparation route.
    preparation_generation: int = Field(default=0, ge=0)
    prepared_calculation_id: str | None = None
    final_authorization_id: str | None = None
    created_at_utc: datetime
    updated_at_utc: datetime

    @field_validator("accepted_plan_hash", "accepted_output_spec_hash")
    @classmethod
    def _optional_hashes(cls, value: str | None) -> str | None:
        return None if value is None else _hash(value)

    @field_validator("task_id", "conversation_id", "alias")
    @classmethod
    def _task_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "task"), maximum=256)

    @field_validator(
        "p4_run_id",
        "p5_run_id",
        "p6_run_id",
        "current_delivery_id",
        "prepared_calculation_id",
        "final_authorization_id",
    )
    @classmethod
    def _optional_task_id(cls, value: str | None, info: object) -> str | None:
        return (
            None if value is None else _text(value, getattr(info, "field_name", "id"), maximum=256)
        )

    @field_validator("created_at_utc", "updated_at_utc")
    @classmethod
    def _task_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _task_invariants(self) -> TaskRecord:
        if self.state is TaskPhase.RESULT_READY and self.current_delivery_id is None:
            raise ValueError("result_ready task requires a delivery")
        if self.state is TaskPhase.PLAN_READY and (self.plan is None or self.validation is None):
            raise ValueError("plan_ready task requires a validated plan")
        if self.accepted_plan_hash is not None and self.plan is None:
            raise ValueError("accepted plan hash requires a plan")
        if self.prepared_calculation_id is not None and self.preparation_generation < 1:
            raise ValueError("a prepared calculation requires a positive generation")
        if self.final_authorization_id is not None and self.prepared_calculation_id is None:
            raise ValueError("final authorization requires a prepared calculation")
        return self


class PendingActionRecord(P7Model):
    """A server-issued, object-bound token shown to the user."""

    token: str
    conversation_id: str
    task_id: str | None = None
    action_type: str
    target_id: str
    expected_revision: int = Field(ge=1)
    content_hash: str
    payload: FrozenJsonObject
    status: Literal["pending", "consumed", "rejected", "stale"] = "pending"
    decision: str | None = None
    created_at_utc: datetime
    consumed_at_utc: datetime | None = None

    _hash = field_validator("content_hash")(_hash)

    @field_validator("token", "conversation_id", "action_type", "target_id")
    @classmethod
    def _pending_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "pending field"), maximum=512)

    @field_validator("task_id", "decision")
    @classmethod
    def _optional_pending_text(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _text(value, getattr(info, "field_name", "pending field"), maximum=512)
        )

    @field_validator("payload", mode="before")
    @classmethod
    def _pending_payload(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("created_at_utc", "consumed_at_utc")
    @classmethod
    def _pending_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


class HandoffRecord(P7Model):
    handoff_id: str
    task_id: str
    target: Literal["p4", "p5", "p6"]
    command_id: str
    child_id: str
    expected_revision: int = Field(ge=1)
    payload: FrozenJsonObject
    payload_hash: str
    status: Literal["prepared", "submitted", "linked", "reconciliation_required"]
    target_run_id: str | None = None
    created_at_utc: datetime
    updated_at_utc: datetime

    _hash = field_validator("payload_hash")(_hash)

    @field_validator("handoff_id", "task_id", "command_id", "child_id")
    @classmethod
    def _handoff_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "handoff field"), maximum=512)

    @field_validator("target_run_id")
    @classmethod
    def _target_run(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "target_run_id", maximum=512)

    @field_validator("payload", mode="before")
    @classmethod
    def _handoff_payload(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("created_at_utc", "updated_at_utc")
    @classmethod
    def _handoff_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @classmethod
    def create(
        cls,
        *,
        handoff_id: str,
        task_id: str,
        target: Literal["p4", "p5", "p6"],
        command_id: str,
        child_id: str,
        expected_revision: int,
        payload: dict[str, object],
        status: Literal["prepared", "submitted", "linked", "reconciliation_required"] = "prepared",
        target_run_id: str | None = None,
        created_at_utc: datetime,
        updated_at_utc: datetime | None = None,
    ) -> HandoffRecord:
        return cls(
            handoff_id=handoff_id,
            task_id=task_id,
            target=target,
            command_id=command_id,
            child_id=child_id,
            expected_revision=expected_revision,
            payload=payload,
            payload_hash=sha256_hex(payload),
            status=status,
            target_run_id=target_run_id,
            created_at_utc=created_at_utc,
            updated_at_utc=updated_at_utc or created_at_utc,
        )


__all__ = [
    "CalculationPlan",
    "CalculationRequest",
    "DeliveryRecord",
    "Fulfillment",
    "FulfillmentStatus",
    "OutputKind",
    "OutputQuantity",
    "OutputSpec",
    "OverallDeliveryStatus",
    "RenderedResultView",
    "PendingActionRecord",
    "ParameterValue",
    "HandoffRecord",
    "PlanNode",
    "PlanValidation",
    "StopReason",
    "TaskPhase",
    "TaskRecord",
    "ValidationStatus",
]
