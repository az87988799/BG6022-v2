"""Strict schema-4 contracts for local ORCA execution.

P5 records are intentionally separate from the P3 fake fixture contracts.  A
fake result can exercise the execution lifecycle, but its origin remains
``fake_fixture`` and can never be presented as a local ORCA result.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..orchestration.p5_versions import (
    P5_COMPILER_VERSION,
    P5_ENGINE_VERSION,
    P5_GEOMETRY_VERSION,
    P5_SCHEMA_VERSION,
)
from ..orchestration.state import RunStatus
from ..orchestration.temporal import ensure_utc
from .hashing import sha256_hex
from .ids import (
    ActionId,
    ApprovalGrantId,
    ArtifactId,
    CommandId,
    ConversationId,
    ExecutionId,
    JobId,
    RunId,
    WorkflowRecordId,
)
from .json_types import FrozenJsonObject, freeze_json_object

_HASH = re.compile(r"^[0-9a-f]{64}$")

P5_DEFAULT_NPROCS = 4
P5_DEFAULT_TOTAL_MEMORY_MB = 2048
P5_DEFAULT_MAXCORE_MB = (P5_DEFAULT_TOTAL_MEMORY_MB * 75) // (100 * P5_DEFAULT_NPROCS)


class P5Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class P5Phase(StrEnum):
    PREPARING = "preparing"
    AWAITING_EXECUTION_APPROVAL = "awaiting_execution_approval"
    DISPATCH_PENDING = "dispatch_pending"
    RUNNING = "running"
    COLLECTING = "collecting"
    CANCELLING = "cancelling"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class P5NodeKind(StrEnum):
    SP = "sp"
    OPT = "opt"
    FREQ = "freq"


class P5GeometrySource(StrEnum):
    INITIAL = "initial_geometry"
    OPTIMIZED = "optimized_geometry"


class P5ActionStatus(StrEnum):
    PLANNED = "planned"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class P5JobStatus(StrEnum):
    RESERVED = "reserved"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    NEEDS_RECONCILIATION = "needs_reconciliation"


class P5DataOrigin(StrEnum):
    FAKE_FIXTURE = "fake_fixture"
    ORCA_LOCAL = "orca_local"


class P5ParseStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    REJECTED = "rejected"


def _hash(value: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError("value must be lowercase SHA-256 hex")
    return value


def _optional_hash(value: str | None) -> str | None:
    return None if value is None else _hash(value)


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field_name} must be non-blank and contain no NUL")
    return value.strip()


def _finite(value: float, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return float(value)


class P5Budget(P5Model):
    """Closed resource envelope used by both compiler and runner."""

    nprocs: int = Field(default=P5_DEFAULT_NPROCS, ge=1, le=64)
    total_memory_mb: int = Field(default=P5_DEFAULT_TOTAL_MEMORY_MB, ge=256, le=1_048_576)
    maxcore_mb: int = Field(default=P5_DEFAULT_MAXCORE_MB, ge=1, le=1_048_576)
    wall_time_seconds: int = Field(default=300, ge=1, le=86_400)
    run_wall_time_seconds: int = Field(default=3600, ge=1, le=604_800)
    stdout_stderr_limit_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)
    workdir_limit_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)

    @model_validator(mode="after")
    def _budget_invariants(self) -> P5Budget:
        expected = (self.total_memory_mb * 75) // (100 * self.nprocs)
        if self.maxcore_mb != expected:
            raise ValueError("maxcore_mb must equal floor(0.75 * total_memory_mb / nprocs)")
        if self.wall_time_seconds > self.run_wall_time_seconds:
            raise ValueError("job wall time cannot exceed run wall time")
        return self

    @classmethod
    def defaults_for(cls, kind: P5NodeKind) -> P5Budget:
        seconds = {P5NodeKind.SP: 300, P5NodeKind.OPT: 900, P5NodeKind.FREQ: 1800}[kind]
        return cls(wall_time_seconds=seconds)

    def budget_hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


class GeometryRecord(P5Model):
    record_id: WorkflowRecordId
    schema_version: Literal[P5_SCHEMA_VERSION] = P5_SCHEMA_VERSION
    engine_version: Literal[P5_ENGINE_VERSION] = P5_ENGINE_VERSION
    run_id: RunId
    confirmed_molecule_id: WorkflowRecordId
    identity_hash: str
    canonical_isomeric_smiles: str
    molecular_formula: str
    formal_charge: int
    multiplicity: int = Field(ge=1)
    atom_symbols: tuple[str, ...] = Field(min_length=1)
    atom_map: tuple[int, ...] = Field(min_length=1)
    coordinates: tuple[tuple[float, float, float], ...] = Field(min_length=1)
    xyz_precision: int = Field(default=8, ge=3, le=12)
    rdkit_version: str
    algorithm_version: Literal[P5_GEOMETRY_VERSION] = P5_GEOMETRY_VERSION
    seed: int = Field(ge=0)
    geometry_hash: str
    xyz_bytes_sha256: str
    record_hash: str

    _hashes = field_validator("identity_hash", "geometry_hash", "xyz_bytes_sha256", "record_hash")(
        _hash
    )

    @field_validator("canonical_isomeric_smiles", "molecular_formula", "rdkit_version")
    @classmethod
    def _texts(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "text"))

    @field_validator("coordinates")
    @classmethod
    def _coordinates(
        cls, value: tuple[tuple[float, float, float], ...]
    ) -> tuple[tuple[float, float, float], ...]:
        for point in value:
            if len(point) != 3:
                raise ValueError("each coordinate must have three values")
            for coordinate in point:
                _finite(coordinate, "coordinate")
        return tuple(tuple(float(item) for item in point) for point in value)

    @model_validator(mode="after")
    def _geometry_invariants(self) -> GeometryRecord:
        if len(self.atom_symbols) != len(self.atom_map) or len(self.atom_map) != len(
            self.coordinates
        ):
            raise ValueError("atom symbols, atom map, and coordinates must have equal length")
        if (
            len(set(self.atom_map)) != len(self.atom_map)
            or tuple(sorted(self.atom_map)) != self.atom_map
        ):
            raise ValueError("atom map must be unique and sorted")
        if any(not _text(symbol, "atom symbol") for symbol in self.atom_symbols):
            raise ValueError("atom symbols must be non-blank")
        return self

    def xyz_bytes(self) -> bytes:
        lines = [str(len(self.atom_symbols)), "BG6022 P5 initial geometry"]
        lines.extend(
            f"{symbol} {x:.{self.xyz_precision}f} "
            f"{y:.{self.xyz_precision}f} {z:.{self.xyz_precision}f}"
            for symbol, (x, y, z) in zip(self.atom_symbols, self.coordinates, strict=True)
        )
        return ("\n".join(lines) + "\n").encode("utf-8")


class P5ExecutionNode(P5Model):
    node_id: str
    kind: P5NodeKind
    geometry_source: P5GeometrySource
    depends_on: tuple[str, ...] = ()
    source_node_id: str | None = None
    method_profile_id: str
    method_profile_hash: str
    budget: P5Budget

    _hashes = field_validator("method_profile_hash")(_hash)

    @field_validator("node_id", "method_profile_id")
    @classmethod
    def _node_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "node"))


class P5ExecutionPlan(P5Model):
    record_id: WorkflowRecordId
    schema_version: Literal[P5_SCHEMA_VERSION] = P5_SCHEMA_VERSION
    engine_version: Literal[P5_ENGINE_VERSION] = P5_ENGINE_VERSION
    source_run_id: RunId
    source_plan_id: WorkflowRecordId
    source_plan_hash: str
    confirmed_molecule_id: WorkflowRecordId
    confirmed_molecule_hash: str
    protocol_id: str
    protocol_hash: str
    registry_snapshot_id: WorkflowRecordId
    registry_snapshot_hash: str
    method_profile_id: str
    method_profile_hash: str
    nodes: tuple[P5ExecutionNode, ...] = Field(min_length=1, max_length=3)
    run_budget_seconds: int = Field(default=3600, ge=1)
    plan_hash: str

    _hashes = field_validator(
        "source_plan_hash",
        "confirmed_molecule_hash",
        "protocol_hash",
        "registry_snapshot_hash",
        "method_profile_hash",
        "plan_hash",
    )(_hash)

    @model_validator(mode="after")
    def _plan_invariants(self) -> P5ExecutionPlan:
        ids = tuple(node.node_id for node in self.nodes)
        if len(set(ids)) != len(ids):
            raise ValueError("P5 execution node IDs must be unique")
        known = set(ids)
        if any(dep not in known for node in self.nodes for dep in node.depends_on):
            raise ValueError("P5 node dependency is unknown")
        if self.nodes[0].depends_on:
            raise ValueError("the first P5 node cannot have a dependency")
        return self


class P5ExecutionBinding(P5Model):
    record_id: WorkflowRecordId
    schema_version: Literal[P5_SCHEMA_VERSION] = P5_SCHEMA_VERSION
    engine_version: Literal[P5_ENGINE_VERSION] = P5_ENGINE_VERSION
    run_id: RunId
    conversation_id: ConversationId
    source_run_id: RunId
    confirmed_molecule_id: WorkflowRecordId
    confirmed_molecule_hash: str
    prepared_plan_id: WorkflowRecordId
    prepared_plan_hash: str
    execution_plan_id: WorkflowRecordId
    execution_plan_hash: str
    node_id: str
    action_id: ActionId
    primitive_id: str
    upstream_result_id: WorkflowRecordId | None = None
    upstream_result_hash: str | None = None
    method_profile_id: str
    method_profile_hash: str
    geometry_artifact_id: ArtifactId
    geometry_hash: str
    xyz_bytes_sha256: str
    compiler_version: Literal[P5_COMPILER_VERSION] = P5_COMPILER_VERSION
    feature_profile_hash: str
    input_manifest_hash: str
    input_sha256: str
    backend_kind: str
    runtime_config_hash: str
    orca_version: str | None = None
    executable_sha256: str | None = None
    budget: P5Budget
    run_budget_seconds: int = Field(ge=1)
    recovery_strategy: Literal["reconcile_no_auto_retry"] = "reconcile_no_auto_retry"
    binding_hash: str

    _hashes = field_validator(
        "confirmed_molecule_hash",
        "prepared_plan_hash",
        "execution_plan_hash",
        "method_profile_hash",
        "geometry_hash",
        "xyz_bytes_sha256",
        "feature_profile_hash",
        "input_manifest_hash",
        "input_sha256",
        "runtime_config_hash",
        "binding_hash",
    )(_hash)
    _optional_hashes = field_validator("upstream_result_hash", "executable_sha256")(_optional_hash)

    @field_validator("node_id", "primitive_id", "method_profile_id", "backend_kind")
    @classmethod
    def _binding_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "binding"))


class P5ApprovalGrant(P5Model):
    grant_id: ApprovalGrantId
    schema_version: Literal[P5_SCHEMA_VERSION] = P5_SCHEMA_VERSION
    engine_version: Literal[P5_ENGINE_VERSION] = P5_ENGINE_VERSION
    run_id: RunId
    conversation_id: ConversationId
    interrupt_id: str | None = None
    revision: int = Field(ge=1)
    action_id: ActionId
    action_hash: str
    binding_hash: str
    envelope_hash: str
    budget_hash: str
    issued_at_utc: datetime
    expires_at_utc: datetime
    approval_command_id: CommandId
    grant_hash: str

    _hashes = field_validator(
        "action_hash", "binding_hash", "envelope_hash", "budget_hash", "grant_hash"
    )(_hash)

    @field_validator("issued_at_utc", "expires_at_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _grant_invariants(self) -> P5ApprovalGrant:
        if self.expires_at_utc <= self.issued_at_utc:
            raise ValueError("approval grant expiry must be after issue time")
        return self


class P5ActionRecord(P5Model):
    record_id: WorkflowRecordId
    schema_version: Literal[P5_SCHEMA_VERSION] = P5_SCHEMA_VERSION
    engine_version: Literal[P5_ENGINE_VERSION] = P5_ENGINE_VERSION
    run_id: RunId
    conversation_id: ConversationId
    action_id: ActionId
    node_id: str
    primitive_id: str
    action_hash: str
    envelope_hash: str
    budget_hash: str
    binding_id: WorkflowRecordId
    binding_hash: str
    input_artifact_id: ArtifactId
    geometry_artifact_id: ArtifactId
    status: P5ActionStatus = P5ActionStatus.PLANNED
    grant_id: ApprovalGrantId | None = None
    execution_id: ExecutionId | None = None
    created_at_utc: datetime
    action_record_hash: str

    _hashes = field_validator(
        "action_hash", "envelope_hash", "budget_hash", "binding_hash", "action_record_hash"
    )(_hash)

    @field_validator("created_at_utc")
    @classmethod
    def _created_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class P5ResultRecord(P5Model):
    record_id: WorkflowRecordId
    schema_version: Literal[P5_SCHEMA_VERSION] = P5_SCHEMA_VERSION
    engine_version: Literal[P5_ENGINE_VERSION] = P5_ENGINE_VERSION
    run_id: RunId
    action_id: ActionId
    execution_id: ExecutionId
    job_id: JobId
    data_origin: P5DataOrigin
    primitive: P5NodeKind
    input_manifest_hash: str
    output_manifest_hash: str
    orca_version: str | None = None
    executable_sha256: str | None = None
    exit_code: int | None = None
    normal_termination: bool
    scf_converged: bool | None = None
    optimization_converged: bool | None = None
    parse_status: P5ParseStatus
    energy: float | None = None
    energy_unit: str | None = None
    energy_token: str | None = None
    frequencies: tuple[float, ...] = ()
    frequency_unit: str | None = None
    optimized_geometry_artifact_id: ArtifactId | None = None
    hessian_artifact_id: ArtifactId | None = None
    stdout_artifact_id: ArtifactId
    stderr_artifact_id: ArtifactId
    diagnostics: tuple[str, ...] = ()
    source_locations: FrozenJsonObject = {}
    scientific_assessment: Literal["not_evaluated"] = "not_evaluated"
    claim_status: Literal["not_generated"] = "not_generated"
    result_hash: str

    _hashes = field_validator(
        "input_manifest_hash",
        "output_manifest_hash",
        "result_hash",
    )(_hash)
    _optional_hashes = field_validator("executable_sha256")(_optional_hash)

    @field_validator("source_locations", mode="before")
    @classmethod
    def _locations(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("energy", *())
    @classmethod
    def _energy(cls, value: float | None) -> float | None:
        return None if value is None else _finite(value, "energy")


class P5ExecutionContext(P5Model):
    record_id: WorkflowRecordId
    schema_version: Literal[P5_SCHEMA_VERSION] = P5_SCHEMA_VERSION
    engine_version: Literal[P5_ENGINE_VERSION] = P5_ENGINE_VERSION
    run_id: RunId
    conversation_id: ConversationId
    source_run_id: RunId
    source_registry_snapshot_id: WorkflowRecordId
    source_registry_snapshot_hash: str
    confirmed_molecule_id: WorkflowRecordId
    confirmed_molecule_hash: str
    prepared_plan_id: WorkflowRecordId
    prepared_plan_hash: str
    execution_plan_id: WorkflowRecordId
    execution_plan_hash: str
    context_hash: str

    _hashes = field_validator(
        "source_registry_snapshot_hash",
        "confirmed_molecule_hash",
        "prepared_plan_hash",
        "execution_plan_hash",
        "context_hash",
    )(_hash)


class P5WorkflowState(P5Model):
    """Authoritative schema-4 state reconstructed from P5 events."""

    run_id: RunId
    schema_version: Literal[P5_SCHEMA_VERSION] = P5_SCHEMA_VERSION
    engine_version: Literal[P5_ENGINE_VERSION] = P5_ENGINE_VERSION
    status: RunStatus
    phase: P5Phase
    conversation_id: ConversationId
    source_run_id: RunId
    execution_plan_id: WorkflowRecordId
    execution_plan_hash: str
    current_node_id: str | None = None
    current_action_id: ActionId | None = None
    current_action_hash: str | None = None
    current_binding_hash: str | None = None
    current_grant_id: ApprovalGrantId | None = None
    current_execution_id: ExecutionId | None = None
    last_result_id: WorkflowRecordId | None = None
    cancel_requested: bool = False
    last_outcome_code: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None

    _hashes = field_validator("execution_plan_hash", "current_action_hash", "current_binding_hash")(
        lambda value: None if value is None else _hash(value)
    )

    @model_validator(mode="after")
    def _state_invariants(self) -> P5WorkflowState:
        terminal = {P5Phase.COMPLETED, P5Phase.FAILED, P5Phase.CANCELLED}
        if self.phase is P5Phase.CANCELLED and self.status is not RunStatus.CANCELLED:
            raise ValueError("cancelled P5 phase requires cancelled run status")
        if self.phase is P5Phase.FAILED and self.status is not RunStatus.FAILED:
            raise ValueError("failed P5 phase requires failed run status")
        if (
            self.phase in terminal
            and self.current_execution_id is not None
            and self.phase is P5Phase.COMPLETED
        ):
            raise ValueError("completed P5 state cannot retain an active execution")
        if self.phase is P5Phase.AWAITING_EXECUTION_APPROVAL and self.current_action_id is None:
            raise ValueError("approval phase requires an action")
        if (
            self.phase in {P5Phase.RUNNING, P5Phase.COLLECTING, P5Phase.CANCELLING}
            and self.current_execution_id is None
        ):
            raise ValueError("active P5 phase requires an execution")
        return self


class P5JobRecord(P5Model):
    """Immutable job identity plus mutable execution facts."""

    job_id: JobId
    run_id: RunId
    action_id: ActionId
    execution_id: ExecutionId
    idempotency_key: str
    binding_id: WorkflowRecordId
    binding_hash: str
    input_manifest_hash: str
    geometry_hash: str
    backend_kind: str
    launch_token: str
    launch_generation: int = Field(ge=1)
    launch_reserved_at_utc: datetime
    launch_consumed_at_utc: datetime | None = None
    status: P5JobStatus
    supervisor_pid: int | None = None
    supervisor_created_at: float | None = None
    orca_pid: int | None = None
    orca_created_at: float | None = None
    host_identity: str
    executable_sha256: str | None = None
    job_directory_id: str
    deadline_utc: datetime
    cancel_requested: bool = False
    stop_reason: str | None = None
    last_observation_sequence: int = 0
    last_observation_hash: str | None = None
    exit_code: int | None = None
    terminal_receipt_id: WorkflowRecordId | None = None
    terminal_receipt_hash: str | None = None
    terminal_at_utc: datetime | None = None

    _hashes = field_validator(
        "binding_hash",
        "input_manifest_hash",
        "geometry_hash",
        "executable_sha256",
        "last_observation_hash",
        "terminal_receipt_hash",
    )(lambda value: None if value is None else _hash(value))

    @field_validator(
        "launch_reserved_at_utc", "launch_consumed_at_utc", "deadline_utc", "terminal_at_utc"
    )
    @classmethod
    def _job_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


def stable_geometry_hash(
    *,
    atom_symbols: tuple[str, ...],
    atom_map: tuple[int, ...],
    coordinates: tuple[tuple[float, float, float], ...],
) -> str:
    """Hash coordinates and atom mapping separately from identity and file bytes."""

    return sha256_hex(
        {
            "atom_symbols": list(atom_symbols),
            "atom_map": list(atom_map),
            "coordinates": [[float(item) for item in point] for point in coordinates],
        }
    )


def bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "GeometryRecord",
    "P5ActionRecord",
    "P5ActionStatus",
    "P5ApprovalGrant",
    "P5Budget",
    "P5_DEFAULT_MAXCORE_MB",
    "P5_DEFAULT_NPROCS",
    "P5_DEFAULT_TOTAL_MEMORY_MB",
    "P5DataOrigin",
    "P5ExecutionBinding",
    "P5ExecutionContext",
    "P5ExecutionNode",
    "P5ExecutionPlan",
    "P5GeometrySource",
    "P5JobRecord",
    "P5JobStatus",
    "P5Model",
    "P5NodeKind",
    "P5ParseStatus",
    "P5Phase",
    "P5ResultRecord",
    "P5WorkflowState",
    "bytes_sha256",
    "stable_geometry_hash",
]
