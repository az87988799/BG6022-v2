"""Durable contracts for the P7 identity/geometry preparation stage."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..orchestration.temporal import ensure_utc
from .hashing import sha256_hex
from .json_types import FrozenJsonObject, freeze_json_object

PREPARATION_SCHEMA_VERSION = "p7.preparation.v1"


class PreparationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


class PreparationStatus(StrEnum):
    PREPARING = "preparing"
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


def _text(value: str, name: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank and contain no NUL")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value


def _hash(value: str | None, name: str = "hash") -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


class PreparedCalculation(PreparationModel):
    """Immutable snapshot used by the final confirmation boundary."""

    schema_version: Literal[PREPARATION_SCHEMA_VERSION] = PREPARATION_SCHEMA_VERSION
    prepared_id: str
    task_id: str
    conversation_id: str
    preparation_generation: int = Field(ge=1)
    task_revision: int = Field(ge=1)
    status: PreparationStatus
    request_hash: str
    validation_hash: str | None = None
    plan_hash: str | None = None
    geometry_source: Literal["rdkit_initial", "history_opt"] | None = None
    identity_snapshot: FrozenJsonObject = {}
    parameter_snapshot: FrozenJsonObject = {}
    geometry_draft: FrozenJsonObject | None = None
    geometry_hash: str | None = None
    xyz_bytes_sha256: str | None = None
    plan_snapshot: FrozenJsonObject | None = None
    output_spec_snapshot: FrozenJsonObject | None = None
    dependencies: FrozenJsonObject = {}
    first_input_preview: FrozenJsonObject | None = None
    first_input_hash: str | None = None
    readiness: FrozenJsonObject = {}
    confirmation_hash: str | None = None
    confirmation_expires_at_utc: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at_utc: datetime
    updated_at_utc: datetime
    snapshot_hash: str

    _hashes = field_validator(
        "request_hash",
        "validation_hash",
        "plan_hash",
        "geometry_hash",
        "xyz_bytes_sha256",
        "first_input_hash",
        "confirmation_hash",
        "snapshot_hash",
    )(_hash)

    @field_validator("prepared_id", "task_id", "conversation_id")
    @classmethod
    def _ids(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "id"), 256)

    @field_validator(
        "identity_snapshot",
        "parameter_snapshot",
        "geometry_draft",
        "plan_snapshot",
        "output_spec_snapshot",
        "dependencies",
        "first_input_preview",
        "readiness",
        mode="before",
    )
    @classmethod
    def _objects(cls, value: object) -> FrozenJsonObject | None:
        return None if value is None else freeze_json_object(value)

    @field_validator("error_code", "error_message")
    @classmethod
    def _errors(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, "preparation error", 4096)

    @field_validator("created_at_utc", "updated_at_utc", "confirmation_expires_at_utc")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _snapshot_invariants(self) -> PreparedCalculation:
        if self.status is PreparationStatus.READY:
            required_snapshot_fields = (
                self.identity_snapshot,
                self.parameter_snapshot,
                self.geometry_source,
                self.geometry_draft,
                self.plan_snapshot,
                self.output_spec_snapshot,
                self.first_input_preview,
                self.first_input_hash,
                self.validation_hash,
                self.confirmation_expires_at_utc,
            )
            if any(value in (None, {}) for value in required_snapshot_fields):
                raise ValueError(
                    "ready preparation requires a complete frozen calculation snapshot"
                )
            if not self.plan_hash or not self.geometry_hash or not self.xyz_bytes_sha256:
                raise ValueError("ready preparation requires a frozen plan and geometry")
            if not self.confirmation_hash:
                raise ValueError("ready preparation requires a confirmation content hash")
            confirmation_payload = self.model_dump(
                mode="json", exclude={"snapshot_hash", "confirmation_hash"}
            )
            if self.confirmation_hash != sha256_hex(confirmation_payload):
                raise ValueError("preparation confirmation content hash does not match")
            if self.error_code is not None:
                raise ValueError("ready preparation cannot carry an error")
        if (
            self.status in {PreparationStatus.FAILED, PreparationStatus.CANCELLED}
            and not self.error_code
        ):
            raise ValueError("terminal failed/cancelled preparation requires an error code")
        payload = self.model_dump(mode="json", exclude={"snapshot_hash"})
        if self.snapshot_hash != sha256_hex(payload):
            raise ValueError("prepared calculation snapshot hash does not match content")
        return self

    @classmethod
    def create(cls, **values: object) -> PreparedCalculation:
        values.setdefault("schema_version", PREPARATION_SCHEMA_VERSION)
        if (
            values.get("status") == PreparationStatus.READY
            or values.get("status") == PreparationStatus.READY.value
        ) and values.get("confirmation_hash") is None:
            confirmation_probe = cls.model_construct(
                **{
                    **values,
                    "confirmation_hash": None,
                    "snapshot_hash": "0" * 64,
                }
            )
            values["confirmation_hash"] = sha256_hex(
                confirmation_probe.model_dump(
                    mode="json", exclude={"snapshot_hash", "confirmation_hash"}
                )
            )
        probe = cls.model_construct(**{**values, "snapshot_hash": "0" * 64})
        values["snapshot_hash"] = sha256_hex(
            probe.model_dump(mode="json", exclude={"snapshot_hash"})
        )
        return cls(**values)

    def confirmation_binding(self) -> dict[str, object]:
        """Return only immutable values covered by a final confirmation."""

        return {
            "prepared_id": self.prepared_id,
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "preparation_generation": self.preparation_generation,
            "snapshot_hash": self.snapshot_hash,
            "plan_hash": self.plan_hash,
            "geometry_hash": self.geometry_hash,
            "xyz_bytes_sha256": self.xyz_bytes_sha256,
            "first_input_hash": self.first_input_hash,
            "confirmation_hash": self.confirmation_hash,
        }


class PreparationHandoff(PreparationModel):
    """Versioned preparation handoff; unlike the old p7 handoff it supports generations."""

    schema_version: Literal[PREPARATION_SCHEMA_VERSION] = PREPARATION_SCHEMA_VERSION
    handoff_id: str
    task_id: str
    preparation_generation: int = Field(ge=1)
    target: Literal["identity", "geometry", "preview"]
    command_id: str
    child_id: str
    expected_revision: int = Field(ge=1)
    payload: FrozenJsonObject
    status: Literal["prepared", "submitted", "linked", "stale", "failed"] = "prepared"
    payload_hash: str
    created_at_utc: datetime
    updated_at_utc: datetime

    _hashes = field_validator("payload_hash")(_hash)

    @field_validator("handoff_id", "task_id", "command_id", "child_id")
    @classmethod
    def _handoff_ids(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "handoff id"), 512)

    @field_validator("payload", mode="before")
    @classmethod
    def _payload(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("created_at_utc", "updated_at_utc")
    @classmethod
    def _handoff_times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _payload_hash(self) -> PreparationHandoff:
        if self.payload_hash != sha256_hex(self.payload):
            raise ValueError("preparation handoff payload hash does not match")
        return self

    @classmethod
    def create(cls, **values: object) -> PreparationHandoff:
        values["payload_hash"] = sha256_hex(values.get("payload", {}))
        return cls(**values)


__all__ = [
    "PREPARATION_SCHEMA_VERSION",
    "PreparationHandoff",
    "PreparationModel",
    "PreparationStatus",
    "PreparedCalculation",
]
