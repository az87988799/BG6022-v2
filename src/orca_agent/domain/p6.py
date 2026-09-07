"""Strict immutable contracts for the schema-5 offline scientific workflow.

P6 is a derived workflow.  These models deliberately contain references to
the frozen P5 source rather than copying or mutating P5 records.  Scientific
values are always accompanied by their raw token, context and evidence IDs.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..orchestration.p6_versions import (
    P6_ENGINE_VERSION,
    P6_OBSERVATION_PARSER_VERSION,
    P6_RENDERER_VERSION,
    P6_SCHEMA_VERSION,
    P6_SCIENTIFIC_POLICY_VERSION,
)
from ..orchestration.state import KernelModel, RunStatus
from ..orchestration.temporal import ensure_utc
from .errors import HashMismatchError
from .hashing import sha256_hex, verify_sha256
from .ids import (
    ActionId,
    ArtifactId,
    AssessmentId,
    ClaimId,
    ConversationId,
    EffectId,
    EventId,
    EvidenceId,
    ExecutionId,
    JobId,
    ReportManifestId,
    RunId,
    WorkflowRecordId,
)
from .json_types import (
    FrozenJsonObject,
    FrozenJsonValue,
    freeze_json_object,
    freeze_json_value,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")


class P6Model(KernelModel):
    """Strict, frozen P6 model base."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


class P6Phase(StrEnum):
    ASSESSMENT_PENDING = "assessment_pending"
    REPORT_PENDING = "report_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class P6SourceOrigin(StrEnum):
    ORCA_LOCAL = "orca_local"
    FAKE_FIXTURE = "fake_fixture"


class P6ProcessingMode(StrEnum):
    OFFLINE_REPARSE = "offline_reparse"


class P6MinimumStatus(StrEnum):
    SUPPORTED_WITHIN_POLICY = "supported_within_policy"
    INCONCLUSIVE = "inconclusive"
    NOT_SUPPORTED = "not_supported"


class P6ComparabilityStatus(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class P6ClaimStatus(StrEnum):
    SUPPORTED = "supported"
    QUALIFIED = "qualified"
    REJECTED = "rejected"


class P6ClaimType(StrEnum):
    ELECTRONIC_ENERGY = "electronic_energy"
    VIBRATIONAL_FREQUENCY = "vibrational_frequency"
    LOCAL_MINIMUM_SUPPORT = "local_minimum_support"
    ELECTRONIC_ENERGY_DIFFERENCE = "electronic_energy_difference"


class P6ModeKind(StrEnum):
    PROJECTED_RIGID = "projected_rigid"
    VIBRATIONAL_CANDIDATE = "vibrational_candidate"
    UNCLASSIFIED = "unclassified"


class P6EvidenceType(StrEnum):
    ELECTRONIC_ENERGY = "electronic_energy"
    VIBRATIONAL_FREQUENCY = "vibrational_frequency"
    METHOD_CONTEXT = "method_context"
    THERMOCHEMISTRY_CONTEXT = "thermochemistry_context"
    EXECUTION_FACT = "execution_fact"
    MINIMUM_CHECK = "minimum_check"


def _hash(value: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError("value must be lowercase SHA-256 hex")
    return value


def _optional_hash(value: str | None) -> str | None:
    return None if value is None else _hash(value)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank and contain no NUL")
    return value.strip()


def _finite(value: float, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _unique(values: tuple[object, ...], name: str) -> tuple[object, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")
    return values


def _hash_payload(model: P6Model, field_name: str) -> dict[str, object]:
    return model.model_dump(mode="json", exclude={field_name})


def _hash_values(model_type: type[P6Model], values: dict[str, object], field_name: str) -> str:
    """Hash a factory payload after Pydantic has supplied every default."""

    probe = model_type.model_construct(**values, **{field_name: "0" * 64})
    return sha256_hex(probe.model_dump(mode="json", exclude={field_name}))


def _canonicalize(value: object) -> object:
    """Turn model/tuple values used by factory methods into JSON-native data."""

    if isinstance(value, BaseModel):
        return _canonicalize(value.model_dump(mode="json"))
    if isinstance(value, tuple):
        return [_canonicalize(item) for item in value]
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    return value


class P6ArtifactRef(P6Model):
    artifact_id: ArtifactId
    content_hash: str
    size_bytes: int = Field(ge=0, le=128 * 1024 * 1024)
    media_type: str
    role: str
    relative_path: str | None = None
    owner_run_id: RunId
    owner_action_id: ActionId | None = None
    owner_execution_id: ExecutionId | None = None

    _hashes = field_validator("content_hash")(_hash)

    @field_validator("media_type", "role")
    @classmethod
    def _artifact_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "artifact field"))

    @field_validator("relative_path")
    @classmethod
    def _artifact_path(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "relative_path")


class P6Locator(P6Model):
    """A source locator whose offsets are decoded UTF-8 character offsets."""

    artifact_id: ArtifactId
    artifact_hash: str
    utf8_character_span: tuple[int, int] | None = None
    line: int | None = Field(default=None, ge=1)
    block: str | None = None
    index: int | None = Field(default=None, ge=0)

    _hashes = field_validator("artifact_hash")(_hash)

    @field_validator("block")
    @classmethod
    def _block_text(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "block")

    @field_validator("utf8_character_span")
    @classmethod
    def _span(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        if value is None:
            return None
        start, end = value
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise ValueError("UTF-8 character span must be a non-empty increasing interval")
        return value

    @model_validator(mode="after")
    def _has_location(self) -> P6Locator:
        if (
            self.utf8_character_span is None
            and self.line is None
            and self.block is None
            and self.index is None
        ):
            raise ValueError("locator must include a span, line, block, or index")
        return self


class MethodContext(P6Model):
    confirmed_molecule_id: WorkflowRecordId
    identity_hash: str
    canonical_isomeric_smiles: str
    molecular_formula: str
    atom_symbols: tuple[str, ...] = Field(min_length=1, max_length=256)
    isotope_policy: str = "default_natural_abundance"
    formal_charge: int = Field(ge=-32, le=32)
    multiplicity: int = Field(ge=1, le=64)
    method_profile_id: str
    method_profile_hash: str
    method_display_name: Literal["r²SCAN-3c"] = "r²SCAN-3c"
    basis_policy: Literal["composite_method_defined"] = "composite_method_defined"
    orca_version: str | None = None
    environment: Literal["gas"] = "gas"
    protocol_id: str
    protocol_hash: str | None = None
    settings_hash: str | None = None
    context_hash: str

    _hashes = field_validator(
        "identity_hash", "method_profile_hash", "protocol_hash", "settings_hash", "context_hash"
    )(_optional_hash)

    @field_validator(
        "canonical_isomeric_smiles",
        "molecular_formula",
        "isotope_policy",
        "method_profile_id",
        "protocol_id",
    )
    @classmethod
    def _context_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "context"))

    @field_validator("orca_version")
    @classmethod
    def _orca_version_text(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "orca_version")

    @field_validator("atom_symbols")
    @classmethod
    def _symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_text(item, "atom symbol") for item in value)
        return cleaned

    @model_validator(mode="after")
    def _context_invariants(self) -> MethodContext:
        try:
            verify_sha256(_hash_payload(self, "context_hash"), self.context_hash)
        except HashMismatchError as error:
            raise ValueError("method context_hash does not match content") from error
        return self

    @classmethod
    def create(cls, **values: object) -> MethodContext:
        values["context_hash"] = _hash_values(cls, values, "context_hash")
        return cls(**values)


class ThermochemistryContext(P6Model):
    temperature_K: float | None = None
    pressure_atm: float | None = None
    quasi_rrho: bool | None = None
    cutoff_frequency_cm1: float | None = None
    frequency_scale_factor: float | None = None
    qrrho_reference_frequency_cm1: float | None = None
    standard_state: str | None = None
    symmetry_number: int | None = Field(default=None, ge=1, le=10_000)
    field_locators: FrozenJsonObject = {}
    missing_reasons: tuple[str, ...] = ()
    context_hash: str

    _hashes = field_validator("context_hash")(_hash)

    @field_validator(
        "temperature_K",
        "pressure_atm",
        "cutoff_frequency_cm1",
        "frequency_scale_factor",
        "qrrho_reference_frequency_cm1",
    )
    @classmethod
    def _thermo_finite(cls, value: float | None, info: object) -> float | None:
        return (
            None if value is None else _finite(value, getattr(info, "field_name", "thermo value"))
        )

    @field_validator("standard_state")
    @classmethod
    def _standard_state_text(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "standard_state")

    @field_validator("field_locators", mode="before")
    @classmethod
    def _locators(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("missing_reasons")
    @classmethod
    def _missing(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_text(item, "missing reason") for item in value)

    @model_validator(mode="after")
    def _context_invariants(self) -> ThermochemistryContext:
        try:
            verify_sha256(_hash_payload(self, "context_hash"), self.context_hash)
        except HashMismatchError as error:
            raise ValueError("thermochemistry context_hash does not match content") from error
        return self

    @classmethod
    def create(cls, **values: object) -> ThermochemistryContext:
        values["context_hash"] = _hash_values(cls, values, "context_hash")
        return cls(**values)


class P6SourceResultRef(P6Model):
    result_id: WorkflowRecordId
    result_hash: str
    action_id: ActionId
    action_hash: str
    binding_id: WorkflowRecordId
    binding_hash: str
    execution_id: ExecutionId
    job_id: JobId
    primitive: Literal["sp", "opt", "freq"]
    data_origin: P6SourceOrigin
    input_manifest_hash: str
    output_manifest_hash: str
    orca_version: str | None = None
    energy: float | None = None
    energy_token: str | None = None
    energy_unit: Literal["Eh"] | None = None
    frequencies: tuple[float, ...] = ()
    frequency_tokens: tuple[str, ...] = ()
    optimized_geometry_artifact_id: ArtifactId | None = None
    optimized_geometry_hash: str | None = None
    hessian_artifact_id: ArtifactId | None = None
    hessian_hash: str | None = None
    stdout_artifact_id: ArtifactId
    stdout_hash: str
    stderr_artifact_id: ArtifactId
    stderr_hash: str
    input_artifact_id: ArtifactId
    input_hash: str
    geometry_hash: str
    upstream_result_id: WorkflowRecordId | None = None
    upstream_result_hash: str | None = None
    parse_status: str
    integrity_verified: bool = True

    _hashes = field_validator(
        "result_hash",
        "action_hash",
        "binding_hash",
        "input_manifest_hash",
        "output_manifest_hash",
        "stdout_hash",
        "stderr_hash",
        "input_hash",
        "geometry_hash",
        "optimized_geometry_hash",
        "hessian_hash",
        "upstream_result_hash",
    )(_optional_hash)

    @field_validator("energy")
    @classmethod
    def _energy(cls, value: float | None) -> float | None:
        return None if value is None else _finite(value, "energy")

    @field_validator("frequencies")
    @classmethod
    def _frequencies(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        return tuple(_finite(item, "frequency") for item in value)

    @field_validator("orca_version", "energy_token", "parse_status")
    @classmethod
    def _optional_text(cls, value: str | None, info: object) -> str | None:
        return None if value is None else _text(value, getattr(info, "field_name", "source text"))

    @model_validator(mode="after")
    def _source_result_invariants(self) -> P6SourceResultRef:
        if self.energy is None:
            if self.energy_token is not None or self.energy_unit is not None:
                raise ValueError("missing energy cannot carry an energy token or unit")
        elif self.energy_token is None or self.energy_unit != "Eh":
            raise ValueError("energy must carry an Eh raw token")
        if self.frequencies and len(self.frequencies) != len(self.frequency_tokens):
            raise ValueError("frequency values and raw tokens must have equal length")
        if self.optimized_geometry_artifact_id is None and self.optimized_geometry_hash is not None:
            raise ValueError("optimized geometry hash has no artifact")
        if self.hessian_artifact_id is None and self.hessian_hash is not None:
            raise ValueError("Hessian hash has no artifact")
        return self


class P6SourceSnapshot(P6Model):
    record_id: WorkflowRecordId
    snapshot_id: WorkflowRecordId
    schema_version: Literal[P6_SCHEMA_VERSION] = P6_SCHEMA_VERSION
    engine_version: Literal[P6_ENGINE_VERSION] = P6_ENGINE_VERSION
    source_p5_run_id: RunId
    source_schema_version: Literal[4] = 4
    source_engine_version: Literal["p5-local-orca-v1"] = "p5-local-orca-v1"
    source_revision: int = Field(ge=1)
    source_event_head_id: EventId
    source_event_head_hash: str
    source_origin: P6SourceOrigin
    processing_mode: Literal[P6ProcessingMode.OFFLINE_REPARSE] = P6ProcessingMode.OFFLINE_REPARSE
    confirmed_molecule_id: WorkflowRecordId
    identity_hash: str
    canonical_isomeric_smiles: str
    molecular_formula: str
    formal_charge: int = Field(ge=-32, le=32)
    multiplicity: int = Field(ge=1, le=64)
    method_profile_id: str
    method_profile_hash: str
    protocol_id: str
    environment: Literal["gas"] = "gas"
    results: tuple[P6SourceResultRef, ...] = Field(min_length=1, max_length=16)
    artifacts: tuple[P6ArtifactRef, ...] = Field(min_length=1, max_length=128)
    snapshot_hash: str

    _hashes = field_validator(
        "source_event_head_hash", "identity_hash", "method_profile_hash", "snapshot_hash"
    )(_hash)

    @field_validator(
        "canonical_isomeric_smiles", "molecular_formula", "method_profile_id", "protocol_id"
    )
    @classmethod
    def _snapshot_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "snapshot"))

    @model_validator(mode="after")
    def _snapshot_invariants(self) -> P6SourceSnapshot:
        if len({item.result_id for item in self.results}) != len(self.results):
            raise ValueError("source results must be unique")
        if len({item.artifact_id for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("source artifacts must be unique")
        try:
            verify_sha256(_hash_payload(self, "snapshot_hash"), self.snapshot_hash)
        except HashMismatchError as error:
            raise ValueError("snapshot_hash does not match content") from error
        return self

    @classmethod
    def create(cls, **values: object) -> P6SourceSnapshot:
        values.setdefault("schema_version", P6_SCHEMA_VERSION)
        values.setdefault("engine_version", P6_ENGINE_VERSION)
        values.setdefault("processing_mode", P6ProcessingMode.OFFLINE_REPARSE)
        values["snapshot_hash"] = _hash_values(cls, values, "snapshot_hash")
        return cls(**values)


class ScientificPolicy(P6Model):
    record_id: WorkflowRecordId
    policy_id: Literal["p6.nonlinear.r2scan3c.v1"] = "p6.nonlinear.r2scan3c.v1"
    policy_version: Literal[P6_SCIENTIFIC_POLICY_VERSION] = P6_SCIENTIFIC_POLICY_VERSION
    schema_version: Literal[P6_SCHEMA_VERSION] = P6_SCHEMA_VERSION
    engine_version: Literal[P6_ENGINE_VERSION] = P6_ENGINE_VERSION
    supported_orca_versions: tuple[str, ...] = ("6.1.1",)
    supported_method_profile_id: Literal["baseline.r2scan3c.v1"] = "baseline.r2scan3c.v1"
    supported_method_display_name: Literal["r²SCAN-3c"] = "r²SCAN-3c"
    supported_environment: Literal["gas"] = "gas"
    supported_charge: Literal[0] = 0
    supported_multiplicity: Literal[1] = 1
    nonlinear_min_atoms: Literal[3] = 3
    inertia_floor_amu_angstrom2: float = 1.0e-10
    nonlinear_inertia_ratio_floor: float = 1.0e-6
    projected_frequency_abs_max_cm1: float = 1.0e-8
    projected_mode_abs_max: float = 1.0e-12
    significant_imaginary_frequency_cm1: float = -20.0
    near_zero_frequency_cm1: float = 1.0
    low_frequency_limit_cm1: float = 50.0
    policy_hash: str

    _hashes = field_validator("policy_hash")(_hash)

    @field_validator(
        "inertia_floor_amu_angstrom2",
        "nonlinear_inertia_ratio_floor",
        "projected_frequency_abs_max_cm1",
        "projected_mode_abs_max",
        "significant_imaginary_frequency_cm1",
        "near_zero_frequency_cm1",
        "low_frequency_limit_cm1",
    )
    @classmethod
    def _policy_finite(cls, value: float, info: object) -> float:
        return _finite(value, getattr(info, "field_name", "policy value"))

    @field_validator("supported_orca_versions")
    @classmethod
    def _versions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_text(item, "ORCA version") for item in value)

    @model_validator(mode="after")
    def _policy_invariants(self) -> ScientificPolicy:
        if not self.significant_imaginary_frequency_cm1 < 0 < self.near_zero_frequency_cm1:
            raise ValueError("scientific frequency thresholds have invalid ordering")
        try:
            verify_sha256(
                self.model_dump(mode="json", exclude={"policy_hash", "record_id"}),
                self.policy_hash,
            )
        except HashMismatchError as error:
            raise ValueError("policy_hash does not match content") from error
        return self

    @classmethod
    def default(cls, *, record_id: WorkflowRecordId) -> ScientificPolicy:
        values = {
            "record_id": record_id,
            "policy_id": "p6.nonlinear.r2scan3c.v1",
            "policy_version": P6_SCIENTIFIC_POLICY_VERSION,
            "schema_version": P6_SCHEMA_VERSION,
            "engine_version": P6_ENGINE_VERSION,
            "supported_orca_versions": ("6.1.1",),
            "supported_method_profile_id": "baseline.r2scan3c.v1",
            "supported_method_display_name": "r²SCAN-3c",
            "supported_environment": "gas",
            "supported_charge": 0,
            "supported_multiplicity": 1,
            "nonlinear_min_atoms": 3,
            "inertia_floor_amu_angstrom2": 1.0e-10,
            "nonlinear_inertia_ratio_floor": 1.0e-6,
            "projected_frequency_abs_max_cm1": 1.0e-8,
            "projected_mode_abs_max": 1.0e-12,
            "significant_imaginary_frequency_cm1": -20.0,
            "near_zero_frequency_cm1": 1.0,
            "low_frequency_limit_cm1": 50.0,
        }
        values["policy_hash"] = sha256_hex(
            _canonicalize({key: value for key, value in values.items() if key != "record_id"})
        )
        return cls(**values)


class ModeClassification(P6Model):
    mode_index: int = Field(ge=0, le=100_000)
    frequency: float
    frequency_token: str
    unit: Literal["cm^-1"] = "cm^-1"
    kind: P6ModeKind
    mode_column_max_abs: float | None = None
    reason: str | None = None
    frequency_locator: P6Locator | None = None
    mode_locator: P6Locator | None = None

    @field_validator("frequency")
    @classmethod
    def _frequency_finite(cls, value: float) -> float:
        return _finite(value, "frequency")

    @field_validator("frequency_token")
    @classmethod
    def _frequency_token(cls, value: str) -> str:
        return _text(value, "frequency_token")

    @field_validator("mode_column_max_abs")
    @classmethod
    def _mode_max(cls, value: float | None) -> float | None:
        return None if value is None else _finite(value, "mode_column_max_abs")

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "mode reason")


class ScientificCheck(P6Model):
    check_id: str
    status: Literal["passed", "failed", "inconclusive", "not_applicable"]
    summary: str
    evidence_ids: tuple[EvidenceId, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @field_validator("check_id", "summary")
    @classmethod
    def _check_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "check"))

    @field_validator("evidence_ids", "missing_requirements", "warnings")
    @classmethod
    def _check_lists(cls, value: tuple[object, ...], info: object) -> tuple[object, ...]:
        cleaned = tuple(
            item if not isinstance(item, str) else _text(item, getattr(info, "field_name", "item"))
            for item in value
        )
        return _unique(cleaned, getattr(info, "field_name", "items"))


class P6EvidenceRecord(P6Model):
    record_id: WorkflowRecordId
    evidence_id: EvidenceId
    schema_version: Literal[P6_SCHEMA_VERSION] = P6_SCHEMA_VERSION
    engine_version: Literal[P6_ENGINE_VERSION] = P6_ENGINE_VERSION
    source_p5_run_id: RunId
    source_result_id: WorkflowRecordId | None = None
    source_execution_id: ExecutionId | None = None
    quantity: str
    evidence_type: P6EvidenceType
    value: FrozenJsonValue = None
    raw_value_token: str | None = None
    unit: str | None = None
    locator: P6Locator | None = None
    source_artifact_id: ArtifactId
    source_artifact_hash: str
    source_origin: P6SourceOrigin
    processing_mode: Literal[P6ProcessingMode.OFFLINE_REPARSE] = P6ProcessingMode.OFFLINE_REPARSE
    context_hash: str | None = None
    context: MethodContext | ThermochemistryContext | None = None
    assessment_id: AssessmentId | None = None
    evidence_hash: str

    _hashes = field_validator("source_artifact_hash", "context_hash", "evidence_hash")(
        _optional_hash
    )

    @field_validator("quantity")
    @classmethod
    def _quantity(cls, value: str) -> str:
        return _text(value, "quantity")

    @field_validator("raw_value_token", "unit")
    @classmethod
    def _value_text(cls, value: str | None, info: object) -> str | None:
        return None if value is None else _text(value, getattr(info, "field_name", "value text"))

    @field_validator("value", mode="before")
    @classmethod
    def _value_json(cls, value: object) -> FrozenJsonValue:
        return freeze_json_value(value)

    @model_validator(mode="after")
    def _evidence_invariants(self) -> P6EvidenceRecord:
        if self.value is not None and self.raw_value_token is None:
            raise ValueError("evidence values require their raw number/token")
        if self.locator is None:
            raise ValueError("evidence must have a source locator")
        if self.context is not None and self.context_hash != getattr(
            self.context, "context_hash", None
        ):
            raise ValueError("evidence context hash does not match context")
        try:
            verify_sha256(_hash_payload(self, "evidence_hash"), self.evidence_hash)
        except HashMismatchError as error:
            raise ValueError("evidence_hash does not match content") from error
        return self

    @classmethod
    def create(cls, **values: object) -> P6EvidenceRecord:
        values.setdefault("schema_version", P6_SCHEMA_VERSION)
        values.setdefault("engine_version", P6_ENGINE_VERSION)
        values.setdefault("processing_mode", P6ProcessingMode.OFFLINE_REPARSE)
        values["evidence_hash"] = _hash_values(cls, values, "evidence_hash")
        return cls(**values)


class ScientificAssessment(P6Model):
    record_id: WorkflowRecordId
    assessment_id: AssessmentId
    schema_version: Literal[P6_SCHEMA_VERSION] = P6_SCHEMA_VERSION
    engine_version: Literal[P6_ENGINE_VERSION] = P6_ENGINE_VERSION
    run_id: RunId
    source_p5_run_id: RunId
    source_snapshot_id: WorkflowRecordId
    source_snapshot_hash: str
    policy_id: str
    policy_hash: str
    parser_version: Literal[P6_OBSERVATION_PARSER_VERSION] = P6_OBSERVATION_PARSER_VERSION
    source_origin: P6SourceOrigin
    processing_mode: Literal[P6ProcessingMode.OFFLINE_REPARSE] = P6ProcessingMode.OFFLINE_REPARSE
    result_ids: tuple[WorkflowRecordId, ...] = Field(min_length=1, max_length=16)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=256)
    claim_ids: tuple[ClaimId, ...] = ()
    energy_available: bool
    minimum_status: P6MinimumStatus
    minimum_reason: str
    checks: tuple[ScientificCheck, ...] = Field(min_length=1, max_length=64)
    mode_classifications: tuple[ModeClassification, ...] = Field(max_length=100_000)
    missing_requirements: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    integrity_verified: bool = True
    assessment_hash: str

    _hashes = field_validator("source_snapshot_hash", "policy_hash", "assessment_hash")(_hash)

    @field_validator("policy_id", "minimum_reason")
    @classmethod
    def _assessment_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "assessment"))

    @field_validator("missing_requirements", "warnings", "limitations")
    @classmethod
    def _assessment_lists(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        cleaned = tuple(
            _text(item, getattr(info, "field_name", "assessment item")) for item in value
        )
        return _unique(cleaned, getattr(info, "field_name", "assessment items"))

    @model_validator(mode="after")
    def _assessment_invariants(self) -> ScientificAssessment:
        if len(set(self.result_ids)) != len(self.result_ids) or len(set(self.evidence_ids)) != len(
            self.evidence_ids
        ):
            raise ValueError("assessment references must be unique")
        try:
            verify_sha256(_hash_payload(self, "assessment_hash"), self.assessment_hash)
        except HashMismatchError as error:
            raise ValueError("assessment_hash does not match content") from error
        return self

    @classmethod
    def create(cls, **values: object) -> ScientificAssessment:
        values.setdefault("schema_version", P6_SCHEMA_VERSION)
        values.setdefault("engine_version", P6_ENGINE_VERSION)
        values.setdefault("parser_version", P6_OBSERVATION_PARSER_VERSION)
        values.setdefault("processing_mode", P6ProcessingMode.OFFLINE_REPARSE)
        values["assessment_hash"] = _hash_values(cls, values, "assessment_hash")
        return cls(**values)


class ComparabilityDimension(P6Model):
    name: str
    candidate_value: FrozenJsonValue = None
    reference_value: FrozenJsonValue = None
    status: P6ComparabilityStatus
    reason: str

    @field_validator("name", "reason")
    @classmethod
    def _dimension_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "dimension"))

    @field_validator("candidate_value", "reference_value", mode="before")
    @classmethod
    def _dimension_json(cls, value: object) -> FrozenJsonValue:
        return freeze_json_value(value)


class ComparabilityAssessment(P6Model):
    record_id: WorkflowRecordId
    candidate_assessment_id: AssessmentId
    candidate_assessment_hash: str
    reference_assessment_id: AssessmentId
    reference_assessment_hash: str
    candidate_energy_evidence_id: EvidenceId
    reference_energy_evidence_id: EvidenceId
    status: P6ComparabilityStatus
    dimensions: tuple[ComparabilityDimension, ...] = Field(min_length=1, max_length=32)
    quantity: Literal["electronic_energy_difference"] = "electronic_energy_difference"
    delta_formula: Literal["E_candidate - E_reference"] = "E_candidate - E_reference"
    delta_energy: float | None = None
    delta_energy_token: str | None = None
    unit: Literal["Eh"] = "Eh"
    limitations: tuple[str, ...] = ()
    comparability_hash: str

    _hashes = field_validator(
        "candidate_assessment_hash", "reference_assessment_hash", "comparability_hash"
    )(_hash)

    @field_validator("delta_energy")
    @classmethod
    def _delta(cls, value: float | None) -> float | None:
        return None if value is None else _finite(value, "delta_energy")

    @field_validator("delta_energy_token")
    @classmethod
    def _delta_token(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "delta_energy_token")

    @model_validator(mode="after")
    def _comparison_invariants(self) -> ComparabilityAssessment:
        if self.status is P6ComparabilityStatus.COMPATIBLE:
            if self.delta_energy is None or self.delta_energy_token is None:
                raise ValueError("compatible comparison requires an energy difference")
        elif self.delta_energy is not None or self.delta_energy_token is not None:
            raise ValueError("incompatible comparison cannot carry an energy difference")
        try:
            verify_sha256(_hash_payload(self, "comparability_hash"), self.comparability_hash)
        except HashMismatchError as error:
            raise ValueError("comparability_hash does not match content") from error
        return self

    @classmethod
    def create(cls, **values: object) -> ComparabilityAssessment:
        values["comparability_hash"] = _hash_values(cls, values, "comparability_hash")
        return cls(**values)


class P6ClaimRecord(P6Model):
    record_id: WorkflowRecordId
    claim_id: ClaimId
    schema_version: Literal[P6_SCHEMA_VERSION] = P6_SCHEMA_VERSION
    engine_version: Literal[P6_ENGINE_VERSION] = P6_ENGINE_VERSION
    claim_type: P6ClaimType
    subject_result_ids: tuple[WorkflowRecordId, ...] = Field(min_length=1, max_length=16)
    quantity: str
    value: FrozenJsonValue = None
    raw_value_token: str | None = None
    unit: str | None = None
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=256)
    evidence_hashes: tuple[str, ...] = Field(min_length=1, max_length=256)
    assessment_id: AssessmentId
    assessment_hash: str
    policy_id: str
    policy_hash: str
    status: P6ClaimStatus
    formula: str | None = None
    limitations: tuple[str, ...] = ()
    claim_hash: str

    _hashes = field_validator("assessment_hash", "policy_hash", "claim_hash")(_hash)

    @field_validator("evidence_hashes")
    @classmethod
    def _evidence_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_hash(item) for item in value)

    @field_validator("quantity", "policy_id")
    @classmethod
    def _claim_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "claim"))

    @field_validator("value", mode="before")
    @classmethod
    def _claim_value(cls, value: object) -> FrozenJsonValue:
        return freeze_json_value(value)

    @field_validator("raw_value_token", "unit", "formula")
    @classmethod
    def _claim_optional_text(cls, value: str | None, info: object) -> str | None:
        return None if value is None else _text(value, getattr(info, "field_name", "claim text"))

    @field_validator("limitations")
    @classmethod
    def _claim_limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(tuple(_text(item, "limitation") for item in value), "limitations")

    @model_validator(mode="after")
    def _claim_invariants(self) -> P6ClaimRecord:
        if len(self.evidence_ids) != len(self.evidence_hashes):
            raise ValueError("claim evidence IDs and hashes must be paired")
        if self.value is not None and self.raw_value_token is None:
            raise ValueError("claim values require raw tokens")
        if self.status is P6ClaimStatus.QUALIFIED and not self.limitations:
            raise ValueError("qualified claim requires limitations")
        if (
            self.claim_type is P6ClaimType.ELECTRONIC_ENERGY_DIFFERENCE
            and self.formula != "E_candidate - E_reference"
        ):
            raise ValueError("energy difference claim must use the fixed formula")
        try:
            verify_sha256(_hash_payload(self, "claim_hash"), self.claim_hash)
        except HashMismatchError as error:
            raise ValueError("claim_hash does not match content") from error
        return self

    @classmethod
    def create(cls, **values: object) -> P6ClaimRecord:
        values.setdefault("schema_version", P6_SCHEMA_VERSION)
        values.setdefault("engine_version", P6_ENGINE_VERSION)
        values["claim_hash"] = _hash_values(cls, values, "claim_hash")
        return cls(**values)


class P6ReportManifest(P6Model):
    report_manifest_id: ReportManifestId
    record_id: WorkflowRecordId
    schema_version: Literal[P6_SCHEMA_VERSION] = P6_SCHEMA_VERSION
    engine_version: Literal[P6_ENGINE_VERSION] = P6_ENGINE_VERSION
    run_id: RunId
    source_p5_run_id: RunId
    source_snapshot_id: WorkflowRecordId
    source_snapshot_hash: str
    policy_id: str
    policy_hash: str
    parser_version: Literal[P6_OBSERVATION_PARSER_VERSION] = P6_OBSERVATION_PARSER_VERSION
    renderer_version: Literal[P6_RENDERER_VERSION] = P6_RENDERER_VERSION
    assessment_id: AssessmentId
    assessment_hash: str
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=256)
    claim_ids: tuple[ClaimId, ...] = ()
    markdown_artifact_id: ArtifactId
    json_artifact_id: ArtifactId
    manifest_artifact_id: ArtifactId | None = None
    markdown_hash: str
    json_hash: str
    markdown_size_bytes: int = Field(ge=1, le=32 * 1024 * 1024)
    json_size_bytes: int = Field(ge=1, le=32 * 1024 * 1024)
    verification_scope: Literal["local_ledger", "archived_packet"] = "local_ledger"
    dependencies: tuple[P6ArtifactRef, ...] = Field(min_length=1, max_length=256)
    coverage: FrozenJsonObject
    created_at_utc: datetime
    manifest_hash: str

    _hashes = field_validator(
        "source_snapshot_hash",
        "policy_hash",
        "assessment_hash",
        "markdown_hash",
        "json_hash",
        "manifest_hash",
    )(_hash)

    @field_validator("policy_id")
    @classmethod
    def _manifest_policy(cls, value: str) -> str:
        return _text(value, "policy_id")

    @field_validator("coverage", mode="before")
    @classmethod
    def _coverage(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("created_at_utc")
    @classmethod
    def _manifest_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _manifest_invariants(self) -> P6ReportManifest:
        if self.markdown_artifact_id == self.json_artifact_id:
            raise ValueError("report artifacts must be distinct")
        if len(set(self.evidence_ids)) != len(self.evidence_ids) or len(set(self.claim_ids)) != len(
            self.claim_ids
        ):
            raise ValueError("manifest references must be unique")
        try:
            verify_sha256(_hash_payload(self, "manifest_hash"), self.manifest_hash)
        except HashMismatchError as error:
            raise ValueError("manifest_hash does not match content") from error
        return self

    @classmethod
    def create(cls, **values: object) -> P6ReportManifest:
        values.setdefault("schema_version", P6_SCHEMA_VERSION)
        values.setdefault("engine_version", P6_ENGINE_VERSION)
        values.setdefault("parser_version", P6_OBSERVATION_PARSER_VERSION)
        values.setdefault("renderer_version", P6_RENDERER_VERSION)
        values["manifest_hash"] = _hash_values(cls, values, "manifest_hash")
        return cls(**values)


class P6WorkflowState(P6Model):
    """Authoritative schema-5 projection reconstructed by P6 event replay."""

    run_id: RunId
    schema_version: Literal[P6_SCHEMA_VERSION] = P6_SCHEMA_VERSION
    engine_version: Literal[P6_ENGINE_VERSION] = P6_ENGINE_VERSION
    status: RunStatus
    phase: P6Phase
    conversation_id: ConversationId
    source_p5_run_id: RunId
    source_snapshot_id: WorkflowRecordId
    source_snapshot_hash: str
    source_revision: int = Field(ge=1)
    source_event_head_id: EventId
    policy_id: str
    policy_hash: str
    profile_id: Literal["p6.nonlinear.r2scan3c.v1"] = "p6.nonlinear.r2scan3c.v1"
    assessment_effect_id: EffectId | None = None
    report_effect_id: EffectId | None = None
    assessment_id: AssessmentId | None = None
    assessment_hash: str | None = None
    evidence_ids: tuple[EvidenceId, ...] = ()
    claim_ids: tuple[ClaimId, ...] = ()
    report_manifest_id: ReportManifestId | None = None
    report_artifact_ids: tuple[ArtifactId, ...] = ()
    comparability_id: WorkflowRecordId | None = None
    reference_assessment_id: AssessmentId | None = None
    last_outcome_code: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None

    _hashes = field_validator("source_snapshot_hash", "policy_hash", "assessment_hash")(
        _optional_hash
    )

    @field_validator("policy_id", "last_outcome_code", "last_error_code", "last_error_message")
    @classmethod
    def _state_text(cls, value: str | None, info: object) -> str | None:
        return None if value is None else _text(value, getattr(info, "field_name", "state"))

    @field_validator("report_artifact_ids", "evidence_ids", "claim_ids")
    @classmethod
    def _state_refs(cls, value: tuple[object, ...], info: object) -> tuple[object, ...]:
        return _unique(value, getattr(info, "field_name", "state references"))

    @model_validator(mode="after")
    def _state_invariants(self) -> P6WorkflowState:
        active = {P6Phase.ASSESSMENT_PENDING, P6Phase.REPORT_PENDING, P6Phase.COMPLETED}
        if self.phase in active and self.status is not RunStatus.READY:
            raise ValueError("active P6 phases require ready status")
        if self.phase is P6Phase.FAILED and self.status is not RunStatus.FAILED:
            raise ValueError("failed P6 phase requires failed status")
        if self.phase is P6Phase.CANCELLED and self.status is not RunStatus.CANCELLED:
            raise ValueError("cancelled P6 phase requires cancelled status")
        if self.phase is P6Phase.ASSESSMENT_PENDING:
            if (
                self.assessment_effect_id is None
                or self.report_effect_id is not None
                or self.assessment_id is not None
            ):
                raise ValueError("assessment_pending P6 state is inconsistent")
        elif self.phase is P6Phase.REPORT_PENDING:
            if (
                self.assessment_effect_id is None
                or self.report_effect_id is None
                or self.assessment_id is None
            ):
                raise ValueError("report_pending P6 state is inconsistent")
        elif self.phase is P6Phase.COMPLETED:
            if (
                self.assessment_id is None
                or self.report_manifest_id is None
                or not self.report_artifact_ids
            ):
                raise ValueError("completed P6 state is missing report references")
        return self


__all__ = [
    "ComparabilityAssessment",
    "ComparabilityDimension",
    "MethodContext",
    "ModeClassification",
    "P6ArtifactRef",
    "P6ClaimRecord",
    "P6ClaimStatus",
    "P6ClaimType",
    "P6ComparabilityStatus",
    "P6EvidenceRecord",
    "P6EvidenceType",
    "P6Locator",
    "P6MinimumStatus",
    "P6ModeKind",
    "P6Model",
    "P6Phase",
    "P6ProcessingMode",
    "P6ReportManifest",
    "P6SourceOrigin",
    "P6SourceResultRef",
    "P6SourceSnapshot",
    "P6WorkflowState",
    "ScientificAssessment",
    "ScientificCheck",
    "ScientificPolicy",
    "ThermochemistryContext",
]
