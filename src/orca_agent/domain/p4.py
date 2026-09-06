"""Strict schema-3 contracts for molecule identity and planning.

The contracts in this module are deliberately independent from the P3 Water
vertical slice.  They are persistence boundaries: IDs and versions are
required when a value is loaded, while ``create`` helpers are the only place
where new record IDs are generated.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..orchestration.p4_versions import P4_ENGINE_VERSION, P4_SCHEMA_VERSION
from ..orchestration.state import RunStatus
from ..orchestration.temporal import ensure_utc
from .errors import HashMismatchError
from .hashing import sha256_hex, verify_sha256
from .ids import (
    ArtifactId,
    CommandId,
    ConversationId,
    EffectId,
    EventId,
    InterruptId,
    RunId,
    WorkflowRecordId,
    new_id,
)
from .json_types import (
    FrozenJsonObject,
    JsonObject,
    freeze_json_object,
)
from .models import Environment, PlanProposal, ProblemSpec

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TEXT_PATTERN = re.compile(r"^[a-zA-Z0-9_.:/-]+$")


class P4Model(BaseModel):
    """Immutable, strict, extra-forbidden P4 value."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class MoleculeInputKind(StrEnum):
    NAME = "name"
    CAS = "cas"
    CID = "cid"
    SMILES = "smiles"


class IdentityProvider(StrEnum):
    FAKE = "fake"
    PUBCHEM = "pubchem"
    LOCAL = "local"


class P4Phase(StrEnum):
    RESOLVING_IDENTITY = "resolving_identity"
    AWAITING_IDENTITY = "awaiting_identity"
    PLAN_READY = "plan_ready"
    CANCELLED = "cancelled"
    FAILED = "failed"


class LookupStatus(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    FAILED = "failed"


class RetryAfterStatus(StrEnum):
    MISSING = "missing"
    VALID = "valid"
    INVALID = "invalid"


class IdentityDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


class StereoStatus(StrEnum):
    DEFINED = "defined"
    UNSPECIFIED = "unspecified"
    NOT_APPLICABLE = "not_applicable"


def _hash_field(value: str | None) -> str | None:
    if value is None:
        return None
    if _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError("hash must be lowercase SHA-256 hex")
    return value


def _non_blank(value: str, name: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank and contain no NUL")
    cleaned = value.strip()
    if maximum is not None and len(cleaned) > maximum:
        raise ValueError(f"{name} exceeds its maximum length")
    return cleaned


def _p4_version(value: int) -> int:
    if type(value) is not int or value != P4_SCHEMA_VERSION:
        raise ValueError("P4 record schema_version must be 3")
    return value


def _p4_engine(value: str) -> str:
    if value != P4_ENGINE_VERSION:
        raise ValueError("P4 record engine_version is unsupported")
    return value


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


def _record_hash(model: BaseModel, field: str) -> str:
    return sha256_hex(model.model_dump(mode="json", exclude={field}))


def _verify_record_hash(model: BaseModel, field: str, value: str) -> None:
    try:
        verify_sha256(model.model_dump(mode="json", exclude={field}), value)
    except HashMismatchError as error:
        raise ValueError(f"{field} does not match record content") from error


def _json_shape(value: object) -> object:
    """Turn tuple-backed strict values into canonical JSON arrays for hashing."""

    if isinstance(value, BaseModel):
        return _json_shape(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_shape(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_json_shape(child) for child in value]
    if isinstance(value, list):
        return [_json_shape(child) for child in value]
    return value


class MoleculeQuery(P4Model):
    """The exact input and routing choice used by one planning run."""

    query_id: WorkflowRecordId
    schema_version: int
    engine_version: str
    run_id: RunId
    conversation_id: ConversationId
    input_kind: MoleculeInputKind
    raw_input: str
    normalized_input: str
    charge: int
    multiplicity: int = Field(ge=1)
    provider: IdentityProvider
    protocol_id: str
    query_hash: str

    _schema = field_validator("schema_version")(_p4_version)
    _engine = field_validator("engine_version")(_p4_engine)
    _hash = field_validator("query_hash")(_hash_field)

    @field_validator("raw_input", "normalized_input")
    @classmethod
    def _input_text(cls, value: str, info: object) -> str:
        return _non_blank(value, getattr(info, "field_name", "input"), maximum=4096)

    @field_validator("protocol_id")
    @classmethod
    def _protocol(cls, value: str) -> str:
        return _non_blank(value, "protocol_id")

    @model_validator(mode="after")
    def _hash_matches(self) -> MoleculeQuery:
        _verify_record_hash(self, "query_hash", self.query_hash)
        return self

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        conversation_id: ConversationId,
        input_kind: MoleculeInputKind,
        raw_input: str,
        normalized_input: str,
        charge: int,
        multiplicity: int,
        provider: IdentityProvider,
        protocol_id: str,
        query_id: WorkflowRecordId | None = None,
    ) -> MoleculeQuery:
        identifier = query_id or new_id(WorkflowRecordId)
        values = {
            "query_id": str(identifier),
            "schema_version": P4_SCHEMA_VERSION,
            "engine_version": P4_ENGINE_VERSION,
            "run_id": str(run_id),
            "conversation_id": str(conversation_id),
            "input_kind": input_kind,
            "raw_input": raw_input,
            "normalized_input": normalized_input,
            "charge": charge,
            "multiplicity": multiplicity,
            "provider": provider,
            "protocol_id": protocol_id,
        }
        return cls(**values, query_hash=sha256_hex(_json_shape(values)))


class IdentityCandidate(P4Model):
    """One provider candidate after local RDKit normalization."""

    candidate_id: str
    schema_version: int
    engine_version: str
    source_smiles: str
    canonical_isomeric_smiles: str
    molecular_formula: str
    formal_charge: int
    fragment_count: int = Field(ge=1)
    radical_electron_count: int = Field(ge=0)
    stereo_status: StereoStatus
    isotope_labels: tuple[int, ...] = ()
    cid: int | None = Field(default=None, gt=0)
    inchikey: str | None = None
    source_artifact_id: ArtifactId | None = None
    source_body_sha256: str
    provider: IdentityProvider
    rdkit_version: str
    normalization_strategy: str
    checks: FrozenJsonObject
    candidate_hash: str

    _schema = field_validator("schema_version")(_p4_version)
    _engine = field_validator("engine_version")(_p4_engine)
    _hash = field_validator("candidate_hash", "source_body_sha256")(_hash_field)

    @field_validator(
        "candidate_id",
        "source_smiles",
        "canonical_isomeric_smiles",
        "molecular_formula",
        "rdkit_version",
        "normalization_strategy",
    )
    @classmethod
    def _candidate_text(cls, value: str, info: object) -> str:
        return _non_blank(value, getattr(info, "field_name", "candidate"))

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_shape(cls, value: str) -> str:
        if _TEXT_PATTERN.fullmatch(value) is None:
            raise ValueError("candidate_id contains unsupported characters")
        return value

    @field_validator("inchikey")
    @classmethod
    def _inchikey(cls, value: str | None) -> str | None:
        return None if value is None else _non_blank(value, "inchikey")

    @field_validator("checks", mode="before")
    @classmethod
    def _checks_object(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("isotope_labels")
    @classmethod
    def _isotopes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(type(item) is not int or item < 1 for item in value):
            raise ValueError("isotope labels must be positive integers")
        if tuple(sorted(value)) != value:
            raise ValueError("isotope labels must be sorted")
        return value

    @model_validator(mode="after")
    def _hash_matches(self) -> IdentityCandidate:
        _verify_record_hash(self, "candidate_hash", self.candidate_hash)
        return self

    @classmethod
    def create(
        cls,
        *,
        candidate_id: str,
        source_smiles: str,
        canonical_isomeric_smiles: str,
        molecular_formula: str,
        formal_charge: int,
        fragment_count: int,
        radical_electron_count: int,
        stereo_status: StereoStatus,
        isotope_labels: tuple[int, ...] = (),
        cid: int | None = None,
        inchikey: str | None = None,
        source_artifact_id: ArtifactId | None = None,
        source_body_sha256: str,
        provider: IdentityProvider,
        rdkit_version: str,
        normalization_strategy: str,
        checks: JsonObject,
    ) -> IdentityCandidate:
        values = {
            "candidate_id": candidate_id,
            "schema_version": P4_SCHEMA_VERSION,
            "engine_version": P4_ENGINE_VERSION,
            "source_smiles": source_smiles,
            "canonical_isomeric_smiles": canonical_isomeric_smiles,
            "molecular_formula": molecular_formula,
            "formal_charge": formal_charge,
            "fragment_count": fragment_count,
            "radical_electron_count": radical_electron_count,
            "stereo_status": stereo_status,
            "isotope_labels": isotope_labels,
            "cid": cid,
            "inchikey": inchikey,
            "source_artifact_id": None if source_artifact_id is None else str(source_artifact_id),
            "source_body_sha256": source_body_sha256,
            "provider": provider,
            "rdkit_version": rdkit_version,
            "normalization_strategy": normalization_strategy,
            "checks": checks,
        }
        return cls(**values, candidate_hash=sha256_hex(_json_shape(values)))


class CandidateBundle(P4Model):
    """Bound, finite candidate set presented to the explicit confirmation step."""

    record_id: WorkflowRecordId
    schema_version: int
    engine_version: str
    run_id: RunId
    query_id: WorkflowRecordId
    query_hash: str
    candidates: tuple[IdentityCandidate, ...] = Field(max_length=10)
    candidate_set_hash: str
    confirmable: bool
    reason_codes: tuple[str, ...] = ()
    winning_request: FrozenJsonObject
    bundle_hash: str

    _schema = field_validator("schema_version")(_p4_version)
    _engine = field_validator("engine_version")(_p4_engine)
    _hashes = field_validator("query_hash", "candidate_set_hash", "bundle_hash")(_hash_field)

    @field_validator("winning_request", mode="before")
    @classmethod
    def _winning_request_object(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_non_blank(item, "reason code") for item in value)

    @model_validator(mode="after")
    def _bundle_invariants(self) -> CandidateBundle:
        ids = tuple(item.candidate_id for item in self.candidates)
        if len(set(ids)) != len(ids):
            raise ValueError("candidate IDs must be unique")
        expected_set_hash = sha256_hex([item.model_dump(mode="json") for item in self.candidates])
        if expected_set_hash != self.candidate_set_hash:
            raise ValueError("candidate_set_hash does not match candidates")
        if self.confirmable and not self.candidates:
            raise ValueError("a confirmable bundle needs at least one candidate")
        _verify_record_hash(self, "bundle_hash", self.bundle_hash)
        return self

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        query_id: WorkflowRecordId,
        query_hash: str,
        candidates: tuple[IdentityCandidate, ...],
        confirmable: bool,
        reason_codes: tuple[str, ...] = (),
        winning_request: JsonObject,
        record_id: WorkflowRecordId | None = None,
    ) -> CandidateBundle:
        candidate_set_hash = sha256_hex([item.model_dump(mode="json") for item in candidates])
        values = {
            "record_id": str(record_id or new_id(WorkflowRecordId)),
            "schema_version": P4_SCHEMA_VERSION,
            "engine_version": P4_ENGINE_VERSION,
            "run_id": str(run_id),
            "query_id": str(query_id),
            "query_hash": query_hash,
            "candidates": candidates,
            "candidate_set_hash": candidate_set_hash,
            "confirmable": confirmable,
            "reason_codes": reason_codes,
            "winning_request": winning_request,
        }
        return cls(**values, bundle_hash=sha256_hex(_json_shape(values)))


class ResponseEnvelope(P4Model):
    """Owner-bound raw response evidence; the body is kept losslessly."""

    record_id: WorkflowRecordId
    schema_version: int
    engine_version: str
    run_id: RunId
    query_id: WorkflowRecordId
    query_hash: str
    effect_id: EffectId
    generation: int = Field(ge=1)
    request_sequence: int = Field(ge=1)
    provider: IdentityProvider
    adapter_version: str
    request_metadata: FrozenJsonObject
    received_at_utc: datetime
    body_base64: str
    body_sha256: str
    envelope_hash: str

    _schema = field_validator("schema_version")(_p4_version)
    _engine = field_validator("engine_version")(_p4_engine)
    _hashes = field_validator("query_hash", "body_sha256", "envelope_hash")(_hash_field)
    _received = field_validator("received_at_utc")(_utc)

    @field_validator("adapter_version")
    @classmethod
    def _adapter(cls, value: str) -> str:
        return _non_blank(value, "adapter_version")

    @field_validator("request_metadata", mode="before")
    @classmethod
    def _metadata(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("body_base64")
    @classmethod
    def _base64(cls, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("body_base64 must not be empty")
        try:
            base64.b64decode(value.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError, binascii.Error) as error:
            raise ValueError("body_base64 is invalid") from error
        return value

    @model_validator(mode="after")
    def _body_and_hashes(self) -> ResponseEnvelope:
        body = self.body_bytes()
        import hashlib

        if hashlib.sha256(body).hexdigest() != self.body_sha256:
            raise ValueError("body_sha256 does not match body_base64")
        _verify_record_hash(self, "envelope_hash", self.envelope_hash)
        return self

    def body_bytes(self) -> bytes:
        try:
            return base64.b64decode(self.body_base64.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError, binascii.Error) as error:
            raise ValueError("body_base64 is invalid") from error

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        query_id: WorkflowRecordId,
        query_hash: str,
        effect_id: EffectId,
        generation: int,
        request_sequence: int,
        provider: IdentityProvider,
        adapter_version: str,
        request_metadata: JsonObject,
        received_at_utc: datetime,
        body: bytes,
        record_id: WorkflowRecordId | None = None,
    ) -> ResponseEnvelope:
        import hashlib

        received = ensure_utc(received_at_utc)
        body_sha256 = hashlib.sha256(body).hexdigest()
        values = {
            "record_id": str(record_id or new_id(WorkflowRecordId)),
            "schema_version": P4_SCHEMA_VERSION,
            "engine_version": P4_ENGINE_VERSION,
            "run_id": str(run_id),
            "query_id": str(query_id),
            "query_hash": query_hash,
            "effect_id": str(effect_id),
            "generation": generation,
            "request_sequence": request_sequence,
            "provider": provider,
            "adapter_version": adapter_version,
            "request_metadata": request_metadata,
            "received_at_utc": received.isoformat().replace("+00:00", "Z"),
            "body_base64": base64.b64encode(body).decode("ascii"),
            "body_sha256": body_sha256,
        }
        return cls(
            **{**values, "received_at_utc": received},
            envelope_hash=sha256_hex(values),
        )


class LookupAttempt(P4Model):
    """One immutable provider attempt keyed by effect generation and sequence."""

    record_id: WorkflowRecordId
    schema_version: int
    engine_version: str
    run_id: RunId
    query_id: WorkflowRecordId
    query_hash: str
    effect_id: EffectId
    generation: int = Field(ge=1)
    request_sequence: int = Field(ge=1)
    provider: IdentityProvider
    adapter_version: str
    request_summary: FrozenJsonObject
    received_at_utc: datetime
    status: LookupStatus
    error_code: str | None = None
    response_artifact_id: ArtifactId | None = None
    response_body_sha256: str | None = None
    response_envelope_hash: str | None = None
    retry_after_raw: str | None = None
    retry_after_status: RetryAfterStatus = RetryAfterStatus.MISSING
    retry_not_before_utc: datetime | None = None
    attempt_hash: str

    _schema = field_validator("schema_version")(_p4_version)
    _engine = field_validator("engine_version")(_p4_engine)
    _hashes = field_validator(
        "query_hash", "response_body_sha256", "response_envelope_hash", "attempt_hash"
    )(_hash_field)
    _received = field_validator("received_at_utc")(_utc)
    _retry_time = field_validator("retry_not_before_utc")(_utc)

    @field_validator("adapter_version")
    @classmethod
    def _adapter_version(cls, value: str) -> str:
        return _non_blank(value, "adapter_version")

    @field_validator("request_summary", mode="before")
    @classmethod
    def _request_summary(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("error_code")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _non_blank(value, "attempt text", maximum=256)

    @model_validator(mode="after")
    def _attempt_invariants(self) -> LookupAttempt:
        if self.status is LookupStatus.RETRYABLE_FAILURE and self.error_code is None:
            raise ValueError("retryable attempt needs an error code")
        if self.retry_after_status is RetryAfterStatus.VALID and self.retry_not_before_utc is None:
            raise ValueError("valid Retry-After needs a deadline")
        if (
            self.retry_after_status is not RetryAfterStatus.VALID
            and self.retry_not_before_utc is not None
        ):
            raise ValueError("only valid Retry-After may carry a deadline")
        if (self.response_artifact_id is None) != (
            self.response_body_sha256 is None or self.response_envelope_hash is None
        ):
            raise ValueError("response artifact and response hashes must be complete")
        _verify_record_hash(self, "attempt_hash", self.attempt_hash)
        return self

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        query_id: WorkflowRecordId,
        query_hash: str,
        effect_id: EffectId,
        generation: int,
        request_sequence: int,
        provider: IdentityProvider,
        adapter_version: str,
        request_summary: JsonObject,
        received_at_utc: datetime,
        status: LookupStatus,
        error_code: str | None = None,
        response_artifact_id: ArtifactId | None = None,
        response_body_sha256: str | None = None,
        response_envelope_hash: str | None = None,
        retry_after_raw: str | None = None,
        retry_after_status: RetryAfterStatus = RetryAfterStatus.MISSING,
        retry_not_before_utc: datetime | None = None,
        record_id: WorkflowRecordId | None = None,
    ) -> LookupAttempt:
        received = ensure_utc(received_at_utc)
        retry_deadline = None if retry_not_before_utc is None else ensure_utc(retry_not_before_utc)
        values = {
            "record_id": str(record_id or new_id(WorkflowRecordId)),
            "schema_version": P4_SCHEMA_VERSION,
            "engine_version": P4_ENGINE_VERSION,
            "run_id": str(run_id),
            "query_id": str(query_id),
            "query_hash": query_hash,
            "effect_id": str(effect_id),
            "generation": generation,
            "request_sequence": request_sequence,
            "provider": provider,
            "adapter_version": adapter_version,
            "request_summary": request_summary,
            "received_at_utc": received.isoformat().replace("+00:00", "Z"),
            "status": status,
            "error_code": error_code,
            "response_artifact_id": (
                None if response_artifact_id is None else str(response_artifact_id)
            ),
            "response_body_sha256": response_body_sha256,
            "response_envelope_hash": response_envelope_hash,
            "retry_after_raw": retry_after_raw,
            "retry_after_status": retry_after_status,
            "retry_not_before_utc": (
                None
                if retry_not_before_utc is None
                else retry_deadline.isoformat().replace("+00:00", "Z")
            ),
        }
        return cls(
            **{
                **values,
                "received_at_utc": received,
                "retry_not_before_utc": retry_deadline,
            },
            attempt_hash=sha256_hex(_json_shape(values)),
        )


class ConfirmedMolecule(P4Model):
    """The exact candidate selected by an explicit user confirmation."""

    record_id: WorkflowRecordId
    schema_version: int
    engine_version: str
    run_id: RunId
    query_id: WorkflowRecordId
    query_hash: str
    candidate_set_hash: str
    candidate_id: str
    candidate_hash: str
    canonical_isomeric_smiles: str
    molecular_formula: str
    formal_charge: int
    multiplicity: int = Field(ge=1)
    environment: Environment
    structure_hash: str
    provider: IdentityProvider
    confirmation_command_id: CommandId
    confirmation_event_id: EventId
    identity_record_hash: str

    _schema = field_validator("schema_version")(_p4_version)
    _engine = field_validator("engine_version")(_p4_engine)
    _hashes = field_validator(
        "query_hash",
        "candidate_set_hash",
        "candidate_hash",
        "structure_hash",
        "identity_record_hash",
    )(_hash_field)

    @field_validator("candidate_id", "canonical_isomeric_smiles", "molecular_formula")
    @classmethod
    def _identity_text(cls, value: str, info: object) -> str:
        return _non_blank(value, getattr(info, "field_name", "identity"))

    @model_validator(mode="after")
    def _identity_hash(self) -> ConfirmedMolecule:
        _verify_record_hash(self, "identity_record_hash", self.identity_record_hash)
        return self

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        query_id: WorkflowRecordId,
        query_hash: str,
        candidate_set_hash: str,
        candidate_id: str,
        candidate_hash: str,
        canonical_isomeric_smiles: str,
        molecular_formula: str,
        formal_charge: int,
        multiplicity: int,
        environment: Environment,
        structure_hash: str,
        provider: IdentityProvider,
        confirmation_command_id: CommandId,
        confirmation_event_id: EventId,
        record_id: WorkflowRecordId | None = None,
    ) -> ConfirmedMolecule:
        values = {
            "record_id": str(record_id or new_id(WorkflowRecordId)),
            "schema_version": P4_SCHEMA_VERSION,
            "engine_version": P4_ENGINE_VERSION,
            "run_id": str(run_id),
            "query_id": str(query_id),
            "query_hash": query_hash,
            "candidate_set_hash": candidate_set_hash,
            "candidate_id": candidate_id,
            "candidate_hash": candidate_hash,
            "canonical_isomeric_smiles": canonical_isomeric_smiles,
            "molecular_formula": molecular_formula,
            "formal_charge": formal_charge,
            "multiplicity": multiplicity,
            "environment": environment,
            "structure_hash": structure_hash,
            "provider": provider,
            "confirmation_command_id": str(confirmation_command_id),
            "confirmation_event_id": str(confirmation_event_id),
        }
        return cls(**values, identity_record_hash=sha256_hex(_json_shape(values)))


class P4WorkflowState(P4Model):
    """Authoritative schema-3 state reconstructed only from P4 events."""

    run_id: RunId
    schema_version: int
    engine_version: str
    status: RunStatus
    phase: P4Phase
    conversation_id: ConversationId
    query_id: WorkflowRecordId
    query_hash: str
    registry_snapshot_id: WorkflowRecordId
    registry_snapshot_hash: str
    identity_effect_id: EffectId | None
    pending_interrupt_id: InterruptId | None
    candidate_bundle_id: WorkflowRecordId | None
    candidate_bundle_hash: str | None
    candidate_set_hash: str | None
    confirmed_molecule_id: WorkflowRecordId | None
    confirmed_molecule_hash: str | None
    prepared_plan_id: WorkflowRecordId | None
    prepared_plan_hash: str | None
    last_outcome_code: str | None
    last_error_code: str | None

    _schema = field_validator("schema_version")(_p4_version)
    _engine = field_validator("engine_version")(_p4_engine)
    _hashes = field_validator(
        "query_hash",
        "registry_snapshot_hash",
        "candidate_bundle_hash",
        "candidate_set_hash",
        "confirmed_molecule_hash",
        "prepared_plan_hash",
    )(_hash_field)

    @field_validator("last_outcome_code", "last_error_code")
    @classmethod
    def _codes(cls, value: str | None) -> str | None:
        return None if value is None else _non_blank(value, "workflow code")

    @model_validator(mode="after")
    def _phase_invariants(self) -> P4WorkflowState:
        if self.phase is P4Phase.RESOLVING_IDENTITY:
            if self.status is not RunStatus.CREATED or self.identity_effect_id is None:
                raise ValueError("resolving P4 state is inconsistent")
            if any(
                value is not None
                for value in (
                    self.pending_interrupt_id,
                    self.candidate_bundle_id,
                    self.candidate_bundle_hash,
                    self.candidate_set_hash,
                    self.confirmed_molecule_id,
                    self.confirmed_molecule_hash,
                    self.prepared_plan_id,
                    self.prepared_plan_hash,
                )
            ):
                raise ValueError("resolving P4 state contains post-resolution references")
        elif self.phase is P4Phase.AWAITING_IDENTITY:
            if (
                self.status is not RunStatus.WAITING_FOR_INPUT
                or self.identity_effect_id is None
                or self.pending_interrupt_id is None
                or self.candidate_bundle_id is None
                or self.candidate_bundle_hash is None
                or self.candidate_set_hash is None
                or self.confirmed_molecule_id is not None
                or self.prepared_plan_id is not None
            ):
                raise ValueError("awaiting-identity P4 state is inconsistent")
        elif self.phase is P4Phase.PLAN_READY:
            if (
                self.status is not RunStatus.READY
                or self.pending_interrupt_id is not None
                or self.candidate_bundle_id is None
                or self.candidate_bundle_hash is None
                or self.candidate_set_hash is None
                or self.confirmed_molecule_id is None
                or self.confirmed_molecule_hash is None
                or self.prepared_plan_id is None
                or self.prepared_plan_hash is None
            ):
                raise ValueError("plan-ready P4 state is inconsistent")
        elif self.phase is P4Phase.CANCELLED:
            if self.status is not RunStatus.CANCELLED or self.pending_interrupt_id is not None:
                raise ValueError("cancelled P4 state is inconsistent")
        elif self.phase is P4Phase.FAILED:
            if self.status is not RunStatus.FAILED or self.pending_interrupt_id is not None:
                raise ValueError("failed P4 state is inconsistent")
        return self


class PreparedPlan(P4Model):
    """Planning-only wrapper around P1 contracts and frozen P4 registry data."""

    record_id: WorkflowRecordId
    schema_version: int
    engine_version: str
    run_id: RunId
    confirmed_molecule_id: WorkflowRecordId
    confirmed_molecule_hash: str
    registry_snapshot_id: WorkflowRecordId
    registry_snapshot_hash: str
    protocol_id: str
    protocol_hash: str
    method_profile_id: str
    method_profile_hash: str
    problem_spec: ProblemSpec
    proposal: PlanProposal
    geometry_bindings: FrozenJsonObject
    capability_id: str
    planning_valid: Literal[True]
    execution_ready: Literal[False]
    execution_approved: Literal[False]
    real_scientific_result: Literal[False]
    missing_execution_prerequisites: tuple[str, ...]
    plan_hash: str

    _schema = field_validator("schema_version")(_p4_version)
    _engine = field_validator("engine_version")(_p4_engine)
    _hashes = field_validator(
        "confirmed_molecule_hash",
        "registry_snapshot_hash",
        "protocol_hash",
        "method_profile_hash",
        "plan_hash",
    )(_hash_field)

    @field_validator("geometry_bindings", mode="before")
    @classmethod
    def _geometry(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("missing_execution_prerequisites")
    @classmethod
    def _prerequisites(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_non_blank(item, "execution prerequisite") for item in value)

    @model_validator(mode="after")
    def _plan_hash_matches(self) -> PreparedPlan:
        _verify_record_hash(self, "plan_hash", self.plan_hash)
        if self.problem_spec.environment is not Environment.GAS:
            raise ValueError("P4 prepared plan must use the gas environment")
        return self

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        confirmed_molecule_id: WorkflowRecordId,
        confirmed_molecule_hash: str,
        registry_snapshot_id: WorkflowRecordId,
        registry_snapshot_hash: str,
        protocol_id: str,
        protocol_hash: str,
        method_profile_id: str,
        method_profile_hash: str,
        problem_spec: ProblemSpec,
        proposal: PlanProposal,
        geometry_bindings: JsonObject,
        capability_id: str,
        record_id: WorkflowRecordId | None = None,
    ) -> PreparedPlan:
        values = {
            "record_id": str(record_id or new_id(WorkflowRecordId)),
            "schema_version": P4_SCHEMA_VERSION,
            "engine_version": P4_ENGINE_VERSION,
            "run_id": str(run_id),
            "confirmed_molecule_id": str(confirmed_molecule_id),
            "confirmed_molecule_hash": confirmed_molecule_hash,
            "registry_snapshot_id": str(registry_snapshot_id),
            "registry_snapshot_hash": registry_snapshot_hash,
            "protocol_id": protocol_id,
            "protocol_hash": protocol_hash,
            "method_profile_id": method_profile_id,
            "method_profile_hash": method_profile_hash,
            "problem_spec": problem_spec,
            "proposal": proposal,
            "geometry_bindings": geometry_bindings,
            "capability_id": capability_id,
            "planning_valid": True,
            "execution_ready": False,
            "execution_approved": False,
            "real_scientific_result": False,
            "missing_execution_prerequisites": (
                "initial_3d_geometry",
                "implemented_orca_backend",
                "validated_action_and_execution_approval",
            ),
        }
        return cls(**values, plan_hash=sha256_hex(_json_shape(values)))


class P4Result(P4Model):
    """Public P4 result; event receipts carry the same fields in ``details``."""

    accepted: bool
    code: str
    run_id: RunId
    revision: int
    status: RunStatus
    phase: P4Phase
    conversation_id: ConversationId
    details: FrozenJsonObject

    @field_validator("details", mode="before")
    @classmethod
    def _details(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)


__all__ = [
    "CandidateBundle",
    "ConfirmedMolecule",
    "IdentityCandidate",
    "IdentityDecision",
    "IdentityProvider",
    "LookupAttempt",
    "LookupStatus",
    "MoleculeInputKind",
    "MoleculeQuery",
    "P4Model",
    "P4Phase",
    "P4Result",
    "P4WorkflowState",
    "PreparedPlan",
    "ResponseEnvelope",
    "RetryAfterStatus",
    "StereoStatus",
]
