"""Strict, immutable contracts owned by the P3 fake Water vertical slice.

P3 contracts deliberately use their own schema/engine identifiers.  The P1
scientific records remain schema 1; the workflow, approval, execution, and
report records below are schema 2 and are never silently downgraded.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import HashMismatchError
from .hashing import sha256_hex, verify_sha256
from .ids import (
    ActionId,
    ApprovalGrantId,
    ArtifactId,
    AssessmentId,
    ClaimId,
    ConversationId,
    EffectId,
    EvidenceId,
    ExecutionId,
    InterruptId,
    JobId,
    PlanProposalId,
    ProblemSpecId,
    ReportManifestId,
    RunId,
)
from .json_types import JsonObject
from .models import ValidatedAction

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
P3_SCHEMA_VERSION = 2
P3_ENGINE_VERSION = "p3-water-v1"
P3_FIXTURE_ID = "water_sp_v1"
P3_FIXTURE_VERSION = "1"


class P3Model(BaseModel):
    """Strict and frozen boundary for every persisted P3 value."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class WorkflowPhase(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    DISPATCH_PENDING = "dispatch_pending"
    ASSESSMENT_PENDING = "assessment_pending"
    REPORT_PENDING = "report_pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class LedgerState(StrEnum):
    PLANNED = "planned"
    APPROVED = "approved"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class P3RunStatus(StrEnum):
    """Kernel status values owned by the schema-2 domain contract."""

    CREATED = "created"
    WAITING_FOR_INPUT = "waiting_for_input"
    READY = "ready"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (P3RunStatus.CANCELLED, P3RunStatus.FAILED)


def _hash_field(value: str) -> str:
    if _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError("hash must be lowercase SHA-256 hex")
    return value


def _non_blank(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value.strip()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


class P3WorkflowState(P3Model):
    """Authoritative schema-2 run state reconstructed from P3 kernel events."""

    run_id: RunId
    schema_version: int
    engine_version: str
    status: P3RunStatus
    pending_interrupt_id: InterruptId | None
    last_outcome_code: str | None
    cancel_reason_code: str | None
    phase: WorkflowPhase
    conversation_id: ConversationId
    problem_spec_id: ProblemSpecId
    proposal_id: PlanProposalId
    action_id: ActionId
    action_hash: str
    envelope_hash: str
    budget_hash: str
    approval_interrupt_id: InterruptId
    approval_grant_id: ApprovalGrantId | None
    dispatch_effect_id: EffectId | None
    assessment_effect_id: EffectId | None
    report_effect_id: EffectId | None
    execution_id: ExecutionId | None
    job_id: JobId | None
    assessment_id: AssessmentId | None
    claim_id: ClaimId | None
    report_manifest_id: ReportManifestId | None
    accepted_artifact_ids: tuple[ArtifactId, ...]
    last_error_code: str | None

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P3_SCHEMA_VERSION:
            raise ValueError("P3 workflow schema_version must be 2")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P3_ENGINE_VERSION:
            raise ValueError("P3 workflow engine_version is unsupported")
        return value

    @field_validator("accepted_artifact_ids")
    @classmethod
    def _unique_artifacts(cls, values: tuple[ArtifactId, ...]) -> tuple[ArtifactId, ...]:
        if len(set(values)) != len(values):
            raise ValueError("accepted_artifact_ids must be unique")
        return values

    _hashes = field_validator("action_hash", "envelope_hash", "budget_hash")(_hash_field)

    @field_validator("last_outcome_code", "cancel_reason_code", "last_error_code")
    @classmethod
    def _codes_are_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("workflow codes must not be blank")
        return value

    @model_validator(mode="after")
    def _phase_matches_kernel_status(self) -> P3WorkflowState:
        if self.status is P3RunStatus.WAITING_FOR_INPUT:
            if self.phase is not WorkflowPhase.AWAITING_APPROVAL:
                raise ValueError("waiting P3 state must await approval")
            if self.pending_interrupt_id is None:
                raise ValueError("waiting P3 state must have a pending interrupt")
        elif self.pending_interrupt_id is not None:
            raise ValueError("non-waiting P3 state cannot have a pending interrupt")

        if self.phase is WorkflowPhase.AWAITING_APPROVAL and self.status not in (
            P3RunStatus.CREATED,
            P3RunStatus.WAITING_FOR_INPUT,
        ):
            raise ValueError("approval phase has an invalid kernel status")
        if (
            self.phase
            in (
                WorkflowPhase.DISPATCH_PENDING,
                WorkflowPhase.ASSESSMENT_PENDING,
                WorkflowPhase.REPORT_PENDING,
                WorkflowPhase.COMPLETED,
            )
            and self.status is not P3RunStatus.READY
        ):
            raise ValueError("active or completed P3 phase must use ready status")
        if self.phase is WorkflowPhase.CANCELLED and self.status is not P3RunStatus.CANCELLED:
            raise ValueError("cancelled P3 phase must use cancelled status")
        if self.phase is WorkflowPhase.FAILED and self.status is not P3RunStatus.FAILED:
            raise ValueError("failed P3 phase must use failed status")
        if self.status is P3RunStatus.CANCELLED and self.phase is not WorkflowPhase.CANCELLED:
            raise ValueError("cancelled kernel status must use cancelled phase")
        if self.status is P3RunStatus.FAILED and self.phase is not WorkflowPhase.FAILED:
            raise ValueError("failed kernel status must use failed phase")
        empty_refs = (
            self.approval_grant_id,
            self.dispatch_effect_id,
            self.assessment_effect_id,
            self.report_effect_id,
            self.execution_id,
            self.job_id,
            self.assessment_id,
            self.claim_id,
            self.report_manifest_id,
        )
        if self.phase is WorkflowPhase.AWAITING_APPROVAL:
            if any(value is not None for value in empty_refs) or self.accepted_artifact_ids:
                raise ValueError("approval phase contains post-approval references")
        elif self.phase is WorkflowPhase.DISPATCH_PENDING:
            if (
                self.approval_grant_id is None
                or self.dispatch_effect_id is None
                or self.execution_id is None
                or any(
                    value is not None
                    for value in (
                        self.assessment_effect_id,
                        self.report_effect_id,
                        self.job_id,
                        self.assessment_id,
                        self.claim_id,
                        self.report_manifest_id,
                    )
                )
                or self.accepted_artifact_ids
            ):
                raise ValueError("dispatch phase references are inconsistent")
        elif self.phase is WorkflowPhase.ASSESSMENT_PENDING:
            if (
                self.approval_grant_id is None
                or self.dispatch_effect_id is None
                or self.assessment_effect_id is None
                or self.execution_id is None
                or self.job_id is None
                or self.assessment_id is not None
                or self.claim_id is not None
                or self.report_manifest_id is not None
                or len(self.accepted_artifact_ids) != 1
            ):
                raise ValueError("assessment phase references are inconsistent")
        elif self.phase is WorkflowPhase.REPORT_PENDING:
            if (
                self.approval_grant_id is None
                or self.dispatch_effect_id is None
                or self.assessment_effect_id is None
                or self.report_effect_id is None
                or self.execution_id is None
                or self.job_id is None
                or self.assessment_id is None
                or self.claim_id is None
                or self.report_manifest_id is not None
                or len(self.accepted_artifact_ids) != 2
            ):
                raise ValueError("report phase references are inconsistent")
        elif self.phase is WorkflowPhase.COMPLETED:
            if any(value is None for value in empty_refs) or len(self.accepted_artifact_ids) != 4:
                raise ValueError("completed phase references are inconsistent")
        return self


class ApprovalGrantV1(P3Model):
    """The exact approval binding accepted by the execution gateway."""

    approval_grant_id: ApprovalGrantId
    schema_version: int
    engine_version: str
    run_id: RunId
    conversation_id: ConversationId
    interrupt_id: InterruptId
    action_id: ActionId
    action_hash: str
    envelope_hash: str
    budget_hash: str
    source_revision: int = Field(ge=1)
    approved_at_utc: datetime
    expires_at_utc: datetime
    record_hash: str

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P3_SCHEMA_VERSION:
            raise ValueError("approval schema_version must be 2")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P3_ENGINE_VERSION:
            raise ValueError("approval engine_version is unsupported")
        return value

    _hashes = field_validator("action_hash", "envelope_hash", "budget_hash", "record_hash")(
        _hash_field
    )
    _times = field_validator("approved_at_utc", "expires_at_utc")(_utc)

    @model_validator(mode="after")
    def _expiry(self) -> ApprovalGrantV1:
        if self.expires_at_utc <= self.approved_at_utc:
            raise ValueError("approval expiry must be after approval time")
        try:
            verify_sha256(self._hash_payload(), self.record_hash)
        except HashMismatchError as error:
            raise ValueError("approval record_hash does not match content") from error
        return self

    def _hash_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude={"record_hash"})

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        interrupt_id: InterruptId,
        action: ValidatedAction,
        source_revision: int,
        approved_at_utc: datetime,
        expires_at_utc: datetime,
        approval_grant_id: ApprovalGrantId | None = None,
    ) -> ApprovalGrantV1:
        envelope_hash = sha256_hex(action.execution_envelope)
        budget_hash = sha256_hex(action.budget)
        values = {
            "approval_grant_id": approval_grant_id or _new_prefixed(ApprovalGrantId),
            "schema_version": P3_SCHEMA_VERSION,
            "engine_version": P3_ENGINE_VERSION,
            "run_id": run_id,
            "conversation_id": conversation_id,
            "interrupt_id": interrupt_id,
            "action_id": action.action_id,
            "action_hash": action.action_hash,
            "envelope_hash": envelope_hash,
            "budget_hash": budget_hash,
            "source_revision": source_revision,
            "approved_at_utc": approved_at_utc,
            "expires_at_utc": expires_at_utc,
        }
        return cls(
            **values,
            record_hash=hash_model_fields(cls, values, exclude="record_hash"),
        )


class ExecutionIntent(P3Model):
    """Immutable execution binding plus a separately mutable ledger state."""

    execution_id: ExecutionId
    schema_version: int
    engine_version: str
    run_id: RunId
    action_id: ActionId
    approval_grant_id: ApprovalGrantId
    idempotency_key: str
    ledger_state: LedgerState
    record_hash: str

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P3_SCHEMA_VERSION:
            raise ValueError("execution schema_version must be 2")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P3_ENGINE_VERSION:
            raise ValueError("execution engine_version is unsupported")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _key(cls, value: str) -> str:
        return _non_blank(value, "idempotency_key")

    @field_validator("record_hash")
    @classmethod
    def _record_hash(cls, value: str) -> str:
        return _hash_field(value)

    @model_validator(mode="after")
    def _record_matches(self) -> ExecutionIntent:
        try:
            verify_sha256(self._hash_payload(), self.record_hash)
        except HashMismatchError as error:
            raise ValueError("execution record_hash does not match content") from error
        return self

    def _hash_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude={"record_hash"})

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        action_id: ActionId,
        approval_grant_id: ApprovalGrantId,
        idempotency_key: str,
        execution_id: ExecutionId | None = None,
    ) -> ExecutionIntent:
        values = {
            "execution_id": execution_id or _new_prefixed(ExecutionId),
            "schema_version": P3_SCHEMA_VERSION,
            "engine_version": P3_ENGINE_VERSION,
            "run_id": run_id,
            "action_id": action_id,
            "approval_grant_id": approval_grant_id,
            "idempotency_key": idempotency_key,
            "ledger_state": LedgerState.APPROVED,
        }
        return cls(
            **values,
            record_hash=hash_model_fields(cls, values, exclude="record_hash"),
        )


class FakeJobResult(P3Model):
    """Durable, fixture-only result returned by the fake backend."""

    job_id: JobId
    schema_version: int
    engine_version: str
    run_id: RunId
    action_id: ActionId
    execution_id: ExecutionId
    input_hash: str
    fixture_id: Literal["water_sp_v1"]
    fixture_version: Literal["1"]
    fixture_hash: str
    status: Literal["succeeded"]
    raw_result_artifact_id: ArtifactId
    result_hash: str

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P3_SCHEMA_VERSION:
            raise ValueError("job result schema_version must be 2")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P3_ENGINE_VERSION:
            raise ValueError("job result engine_version is unsupported")
        return value

    _hashes = field_validator("input_hash", "fixture_hash", "result_hash")(_hash_field)

    @model_validator(mode="after")
    def _fixture(self) -> FakeJobResult:
        if self.fixture_id != P3_FIXTURE_ID or self.fixture_version != P3_FIXTURE_VERSION:
            raise ValueError("unsupported fake fixture")
        try:
            verify_sha256(self._hash_payload(), self.result_hash)
        except HashMismatchError as error:
            raise ValueError("job result_hash does not match content") from error
        return self

    def _hash_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude={"result_hash"})


class ParsedFakeObservation(P3Model):
    """Only the known Water fixture observation can enter scientific claims."""

    schema_version: int
    engine_version: str
    action_id: ActionId
    execution_id: ExecutionId
    fixture_id: Literal["water_sp_v1"]
    fixture_version: Literal["1"]
    fixture_hash: str
    energy: float
    unit: Literal["Hartree"]
    source: Literal["fake_fixture"]

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P3_SCHEMA_VERSION:
            raise ValueError("observation schema_version must be 2")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P3_ENGINE_VERSION:
            raise ValueError("observation engine_version is unsupported")
        return value

    @field_validator("fixture_hash")
    @classmethod
    def _fixture_hash(cls, value: str) -> str:
        return _hash_field(value)

    @field_validator("energy")
    @classmethod
    def _finite_energy(cls, value: float) -> float:
        if type(value) is not float or not isfinite(value):
            raise ValueError("energy must be a finite float")
        return value


class FixtureScientificAssessment(P3Model):
    """Assessment that explicitly limits interpretation to the fake fixture."""

    assessment_id: AssessmentId
    schema_version: int
    engine_version: str
    run_id: RunId
    action_id: ActionId
    execution_id: ExecutionId
    evidence_ids: tuple[EvidenceId, ...]
    claim_id: ClaimId
    fixture_verified_only: Literal[True]
    accepted: Literal[True]
    limitations: tuple[str, ...]
    assessment_hash: str

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P3_SCHEMA_VERSION:
            raise ValueError("assessment schema_version must be 2")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P3_ENGINE_VERSION:
            raise ValueError("assessment engine_version is unsupported")
        return value

    @field_validator("evidence_ids", "limitations")
    @classmethod
    def _non_empty_unique(cls, values: tuple[object, ...]) -> tuple[object, ...]:
        if not values or len(set(values)) != len(values):
            raise ValueError("assessment collection must be non-empty and unique")
        return values

    @field_validator("assessment_hash")
    @classmethod
    def _assessment_hash(cls, value: str) -> str:
        return _hash_field(value)

    @model_validator(mode="after")
    def _hash_matches(self) -> FixtureScientificAssessment:
        try:
            verify_sha256(self._hash_payload(), self.assessment_hash)
        except HashMismatchError as error:
            raise ValueError("assessment_hash does not match content") from error
        return self

    def _hash_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude={"assessment_hash"})


class ReportManifestV1(P3Model):
    """Traceable report output manifest with an explicit fake-result marker."""

    report_manifest_id: ReportManifestId
    schema_version: int
    engine_version: str
    run_id: RunId
    action_id: ActionId
    execution_id: ExecutionId
    claim_id: ClaimId
    evidence_ids: tuple[EvidenceId, ...]
    markdown_artifact_id: ArtifactId
    json_artifact_id: ArtifactId
    markdown_hash: str
    json_hash: str
    renderer_version: Literal["p3-renderer-v1"]
    fake_marker: Literal["fake_fixture_only"]
    manifest_hash: str

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P3_SCHEMA_VERSION:
            raise ValueError("report schema_version must be 2")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P3_ENGINE_VERSION:
            raise ValueError("report engine_version is unsupported")
        return value

    _hashes = field_validator("markdown_hash", "json_hash", "manifest_hash")(_hash_field)

    @model_validator(mode="after")
    def _hash_matches(self) -> ReportManifestV1:
        if not self.evidence_ids:
            raise ValueError("report evidence_ids must not be empty")
        if self.markdown_artifact_id == self.json_artifact_id:
            raise ValueError("report artifacts must be distinct")
        try:
            verify_sha256(self._hash_payload(), self.manifest_hash)
        except HashMismatchError as error:
            raise ValueError("manifest_hash does not match content") from error
        return self

    def _hash_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude={"manifest_hash"})


def _new_prefixed(identifier_type):
    """Local import-free factory wrapper kept out of persisted defaults."""

    import uuid

    return identifier_type(f"{identifier_type.prefix}_{uuid.uuid4().hex}")


def workflow_record_hash(value: P3Model) -> str:
    """Hash a typed P3 record using the shared Canonical JSON v1."""

    return sha256_hex(value)


def hash_model_fields(
    model_type: type[BaseModel], values: dict[str, object], *, exclude: str
) -> str:
    """Hash JSON-mode model fields while a caller is constructing a record."""

    candidate = model_type.model_construct(**values, **{exclude: "0" * 64})
    return sha256_hex(candidate.model_dump(mode="json", exclude={exclude}))


__all__ = [
    "ApprovalGrantV1",
    "ExecutionIntent",
    "FakeJobResult",
    "FixtureScientificAssessment",
    "hash_model_fields",
    "LedgerState",
    "P3Model",
    "P3RunStatus",
    "P3WorkflowState",
    "ParsedFakeObservation",
    "ReportManifestV1",
    "WorkflowPhase",
    "workflow_record_hash",
]
