"""Minimal versioned contracts shared by future V2 layers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ContractInvariantError, HashMismatchError
from .hashing import sha256_hex, verify_sha256
from .ids import (
    ActionId,
    ArtifactId,
    ClaimId,
    EvidenceId,
    PlanProposalId,
    PrimitiveId,
    ProblemSpecId,
    new_id,
)
from .json_types import (
    FrozenJsonObject,
    FrozenJsonValue,
    JsonObject,
    JsonValue,
    freeze_json_object,
    freeze_json_value,
)
from .versions import CURRENT_SCHEMA_VERSION, validate_schema_version

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REGISTRY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_UNSAFE_KEY_PARTS = (
    "absolute_path",
    "api_key",
    "command",
    "executable",
    "orca_input",
    "password",
    "path",
    "raw_orca",
    "secret",
    "shell",
    "token",
)


class StrictDomainModel(BaseModel):
    """Shared strict configuration for every P1 contract."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class Environment(StrEnum):
    GAS = "gas"
    UNSPECIFIED = "unspecified"


class PrimitiveKind(StrEnum):
    SP = "sp"
    OPT = "opt"
    FREQ = "freq"


class BackendKind(StrEnum):
    FAKE = "fake"
    LOCAL = "local"


class EvidenceType(StrEnum):
    EXECUTION_SUMMARY = "execution_summary"
    PARSED_ENERGY = "parsed_energy"


class ClaimType(StrEnum):
    ENERGY = "energy"
    FREQUENCY = "frequency"
    STRUCTURE = "structure"


class ClaimStatus(StrEnum):
    VALIDATED = "validated"
    QUALIFIED = "qualified"
    REJECTED = "rejected"


class Budget(StrictDomainModel):
    """Typed resource ceilings; this does not allocate or execute resources."""

    wall_time_seconds: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    cores: int = Field(gt=0)


class ExecutionEnvelope(StrictDomainModel):
    """Logical execution routing with no local paths or shell commands."""

    backend_kind: BackendKind
    artifact_namespace_id: ArtifactId


class Provenance(StrictDomainModel):
    producer: str
    producer_version: str
    created_at: datetime

    @field_validator("producer", "producer_version")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provenance text must not be blank")
        return value.strip()

    @field_validator("created_at")
    @classmethod
    def _must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")
        return value


def _non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


def _safe_json_object(value: JsonObject, field_name: str) -> JsonObject:
    """Reject execution-shaped keys from proposal/config JSON."""

    try:
        frozen = freeze_json_object(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must contain only JSON values") from error

    def visit(current: JsonValue, location: str) -> None:
        if hasattr(current, "items"):
            for key, child in current.items():
                normalized = key.casefold().replace("-", "_")
                if any(part in normalized for part in _UNSAFE_KEY_PARTS):
                    raise ValueError(f"{field_name} contains a forbidden key")
                visit(child, f"{location}.{key}")
        elif isinstance(current, (list, tuple)):
            for index, child in enumerate(current):
                visit(child, f"{location}[{index}]")

    visit(frozen, field_name)
    return frozen


def _registry_id(value: str, field_name: str) -> str:
    value = _non_blank(value, field_name)
    if _REGISTRY_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a registry ID")
    return value


def _hash_field(value: str) -> str:
    if _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError("hash must be lowercase SHA-256 hex")
    return value


ValueT = TypeVar("ValueT")


def _unique(values: tuple[ValueT, ...], field_name: str) -> tuple[ValueT, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


class ProblemSpec(StrictDomainModel):
    record_id: ProblemSpecId
    schema_version: int
    goal: str
    molecule_ref: str
    charge: int
    multiplicity: int = Field(ge=1)
    environment: Environment = Environment.UNSPECIFIED
    target_properties: tuple[str, ...]
    constraints: FrozenJsonObject

    @classmethod
    def create(
        cls,
        *,
        goal: str,
        molecule_ref: str,
        charge: int,
        multiplicity: int,
        target_properties: tuple[str, ...],
        environment: Environment = Environment.UNSPECIFIED,
        constraints: JsonObject | None = None,
        record_id: ProblemSpecId | None = None,
    ) -> ProblemSpec:
        return cls(
            record_id=record_id or new_id(ProblemSpecId),
            schema_version=CURRENT_SCHEMA_VERSION,
            goal=goal,
            molecule_ref=molecule_ref,
            charge=charge,
            multiplicity=multiplicity,
            environment=environment,
            target_properties=target_properties,
            constraints={} if constraints is None else constraints,
        )

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        return validate_schema_version(value)

    @field_validator("goal", "molecule_ref")
    @classmethod
    def _references_must_be_non_blank(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "value")
        return _non_blank(value, field_name)

    @field_validator("target_properties")
    @classmethod
    def _target_properties(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for value in values:
            value = _non_blank(value, "target_properties item")
            if value not in cleaned:
                cleaned.append(value)
        if not cleaned:
            raise ValueError("target_properties must not be empty")
        return tuple(cleaned)

    @field_validator("constraints", mode="before")
    @classmethod
    def _constraints_are_safe(cls, value: JsonObject) -> JsonObject:
        return _safe_json_object(value, "constraints")


class PrimitiveSpec(StrictDomainModel):
    primitive_id: PrimitiveId
    schema_version: int
    kind: PrimitiveKind
    molecule_ref: str
    method_profile_id: str
    depends_on: tuple[PrimitiveId, ...] = ()
    parameters: FrozenJsonObject

    @classmethod
    def create(
        cls,
        *,
        kind: PrimitiveKind,
        molecule_ref: str,
        method_profile_id: str,
        depends_on: tuple[PrimitiveId, ...] = (),
        parameters: JsonObject | None = None,
        primitive_id: PrimitiveId | None = None,
    ) -> PrimitiveSpec:
        return cls(
            primitive_id=primitive_id or new_id(PrimitiveId),
            schema_version=CURRENT_SCHEMA_VERSION,
            kind=kind,
            molecule_ref=molecule_ref,
            method_profile_id=method_profile_id,
            depends_on=depends_on,
            parameters={} if parameters is None else parameters,
        )

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        return validate_schema_version(value)

    @field_validator("molecule_ref")
    @classmethod
    def _molecule_ref(cls, value: str) -> str:
        return _non_blank(value, "molecule_ref")

    @field_validator("method_profile_id")
    @classmethod
    def _method_profile_id(cls, value: str) -> str:
        return _registry_id(value, "method_profile_id")

    @field_validator("depends_on")
    @classmethod
    def _depends_on(cls, values: tuple[PrimitiveId, ...]) -> tuple[PrimitiveId, ...]:
        return _unique(values, "depends_on")

    @field_validator("parameters", mode="before")
    @classmethod
    def _parameters_are_safe(cls, value: JsonObject) -> JsonObject:
        return _safe_json_object(value, "parameters")


class PlanProposal(StrictDomainModel):
    proposal_id: PlanProposalId
    schema_version: int
    problem_spec_id: ProblemSpecId
    problem_spec_hash: str
    steps: tuple[PrimitiveSpec, ...]
    rationale: str
    planner_id: str

    @classmethod
    def create(
        cls,
        *,
        problem_spec_id: ProblemSpecId,
        problem_spec_hash: str,
        steps: tuple[PrimitiveSpec, ...],
        rationale: str,
        planner_id: str,
        proposal_id: PlanProposalId | None = None,
    ) -> PlanProposal:
        return cls(
            proposal_id=proposal_id or new_id(PlanProposalId),
            schema_version=CURRENT_SCHEMA_VERSION,
            problem_spec_id=problem_spec_id,
            problem_spec_hash=problem_spec_hash,
            steps=steps,
            rationale=rationale,
            planner_id=planner_id,
        )

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        return validate_schema_version(value)

    @field_validator("problem_spec_hash")
    @classmethod
    def _problem_spec_hash(cls, value: str) -> str:
        return _hash_field(value)

    @field_validator("steps")
    @classmethod
    def _steps(cls, values: tuple[PrimitiveSpec, ...]) -> tuple[PrimitiveSpec, ...]:
        if not values:
            raise ValueError("steps must not be empty")
        _unique(tuple(step.primitive_id for step in values), "step IDs")
        return values

    @field_validator("rationale", "planner_id")
    @classmethod
    def _proposal_text(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "value")
        return _non_blank(value, field_name)


class ValidatedAction(StrictDomainModel):
    action_id: ActionId
    schema_version: int
    proposal_hash: str
    primitive: PrimitiveSpec
    execution_envelope: ExecutionEnvelope
    budget: Budget
    action_hash: str

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        return validate_schema_version(value)

    @field_validator("proposal_hash", "action_hash")
    @classmethod
    def _hashes(cls, value: str) -> str:
        return _hash_field(value)

    @model_validator(mode="after")
    def _action_hash_matches(self) -> ValidatedAction:
        try:
            self.verify_action_hash()
        except HashMismatchError as error:
            raise ValueError("action_hash does not match canonical action content") from error
        return self

    def _hash_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude={"action_hash"})

    @classmethod
    def create(
        cls,
        *,
        proposal_hash: str,
        primitive: PrimitiveSpec,
        execution_envelope: ExecutionEnvelope,
        budget: Budget,
        action_id: ActionId | None = None,
    ) -> ValidatedAction:
        action_id = action_id or new_id(ActionId)
        hash_payload: JsonObject = {
            "action_id": str(action_id),
            "schema_version": CURRENT_SCHEMA_VERSION,
            "proposal_hash": proposal_hash,
            "primitive": primitive.model_dump(mode="json"),
            "execution_envelope": execution_envelope.model_dump(mode="json"),
            "budget": budget.model_dump(mode="json"),
        }
        return cls(
            action_id=action_id,
            schema_version=CURRENT_SCHEMA_VERSION,
            proposal_hash=proposal_hash,
            primitive=primitive,
            execution_envelope=execution_envelope,
            budget=budget,
            action_hash=sha256_hex(hash_payload),
        )

    def verify_action_hash(self) -> None:
        verify_sha256(self._hash_payload(), self.action_hash)


class EvidenceRecord(StrictDomainModel):
    evidence_id: EvidenceId
    schema_version: int
    action_id: ActionId
    evidence_type: EvidenceType
    payload: FrozenJsonObject
    artifact_refs: tuple[ArtifactId, ...] = ()
    provenance: Provenance
    payload_hash: str

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        return validate_schema_version(value)

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_is_json(cls, value: JsonObject) -> JsonObject:
        return _safe_json_object(value, "payload")

    @field_validator("artifact_refs")
    @classmethod
    def _artifact_refs(cls, values: tuple[ArtifactId, ...]) -> tuple[ArtifactId, ...]:
        return _unique(values, "artifact_refs")

    @field_validator("payload_hash")
    @classmethod
    def _payload_hash(cls, value: str) -> str:
        return _hash_field(value)

    @model_validator(mode="after")
    def _payload_hash_matches(self) -> EvidenceRecord:
        try:
            self.verify_payload_hash()
        except HashMismatchError as error:
            raise ValueError("payload_hash does not match payload content") from error
        return self

    def _hash_payload(self) -> JsonObject:
        return self.payload

    @classmethod
    def create(
        cls,
        *,
        action_id: ActionId,
        evidence_type: EvidenceType,
        payload: JsonObject,
        provenance: Provenance,
        artifact_refs: tuple[ArtifactId, ...] = (),
        evidence_id: EvidenceId | None = None,
    ) -> EvidenceRecord:
        return cls(
            evidence_id=evidence_id or new_id(EvidenceId),
            schema_version=CURRENT_SCHEMA_VERSION,
            action_id=action_id,
            evidence_type=evidence_type,
            payload=payload,
            artifact_refs=artifact_refs,
            provenance=provenance,
            payload_hash=sha256_hex(payload),
        )

    def verify_payload_hash(self) -> None:
        verify_sha256(self._hash_payload(), self.payload_hash)


class ValidatedClaim(StrictDomainModel):
    claim_id: ClaimId
    schema_version: int
    claim_type: ClaimType
    value: FrozenJsonValue
    unit: str | None = None
    evidence_ids: tuple[EvidenceId, ...]
    status: ClaimStatus
    limitations: tuple[str, ...] = ()
    claim_hash: str

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        return validate_schema_version(value)

    @field_validator("evidence_ids")
    @classmethod
    def _evidence_ids(cls, values: tuple[EvidenceId, ...]) -> tuple[EvidenceId, ...]:
        if not values:
            raise ValueError("evidence_ids must not be empty")
        return _unique(values, "evidence_ids")

    @field_validator("value", mode="before")
    @classmethod
    def _value_is_json(cls, value: JsonValue) -> JsonValue:
        try:
            return freeze_json_value(value)
        except ValueError as error:
            raise ValueError("value must contain only JSON values") from error

    @field_validator("limitations")
    @classmethod
    def _limitations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_non_blank(value, "limitation") for value in values)
        return cleaned

    @model_validator(mode="after")
    def _qualified_claim_has_limitations(self) -> ValidatedClaim:
        if self.status in (ClaimStatus.QUALIFIED, ClaimStatus.REJECTED) and not self.limitations:
            raise ContractInvariantError(
                "qualified or rejected claims require limitations",
            )
        return self

    @model_validator(mode="after")
    def _claim_hash_matches(self) -> ValidatedClaim:
        try:
            self.verify_claim_hash()
        except HashMismatchError as error:
            raise ValueError("claim_hash does not match claim content") from error
        return self

    def _hash_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude={"claim_hash"})

    @classmethod
    def create(
        cls,
        *,
        claim_type: ClaimType,
        value: JsonValue,
        evidence_ids: tuple[EvidenceId, ...],
        status: ClaimStatus,
        unit: str | None = None,
        limitations: tuple[str, ...] = (),
        claim_id: ClaimId | None = None,
    ) -> ValidatedClaim:
        claim_id = claim_id or new_id(ClaimId)
        hash_payload: JsonObject = {
            "claim_id": str(claim_id),
            "schema_version": CURRENT_SCHEMA_VERSION,
            "claim_type": claim_type.value,
            "value": value,
            "unit": unit,
            "evidence_ids": [str(item) for item in evidence_ids],
            "status": status.value,
            "limitations": list(limitations),
        }
        return cls(
            claim_id=claim_id,
            schema_version=CURRENT_SCHEMA_VERSION,
            claim_type=claim_type,
            value=value,
            unit=unit,
            evidence_ids=evidence_ids,
            status=status,
            limitations=limitations,
            claim_hash=sha256_hex(hash_payload),
        )

    def verify_claim_hash(self) -> None:
        verify_sha256(self._hash_payload(), self.claim_hash)
