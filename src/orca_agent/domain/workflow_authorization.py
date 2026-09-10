"""Immutable parent authorization for the P7-to-P5 execution boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..orchestration.temporal import ensure_utc
from .hashing import sha256_hex
from .json_types import FrozenJsonObject, freeze_json_object

WORKFLOW_AUTHORIZATION_SCHEMA_VERSION = "p7.workflow-execution-authorization.v1"


class AuthorizationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


def _text(value: str, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank and contain no NUL")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value


def _hash(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("authorization hash fields must be lowercase SHA-256 hex")
    return value


class WorkflowExecutionAuthorization(AuthorizationModel):
    """One parent grant from which all P5 node credentials are derived."""

    schema_version: Literal[WORKFLOW_AUTHORIZATION_SCHEMA_VERSION] = (
        WORKFLOW_AUTHORIZATION_SCHEMA_VERSION
    )
    authorization_id: str
    task_id: str
    prepared_calculation_id: str
    task_revision: int = Field(ge=1)
    preparation_generation: int = Field(ge=1)
    prepared_snapshot_hash: str
    request_hash: str
    validation_hash: str
    plan_hash: str
    geometry_hash: str
    xyz_bytes_sha256: str
    first_input_hash: str | None = None
    allowed_nodes: FrozenJsonObject = {}
    budget: FrozenJsonObject = {}
    geometry_policy: FrozenJsonObject = {}
    node_credentials: FrozenJsonObject = {}
    issued_at_utc: datetime
    expires_at_utc: datetime
    status: Literal["issued", "consumed", "revoked"] = "issued"
    authorization_hash: str

    _hashes = field_validator(
        "prepared_snapshot_hash",
        "request_hash",
        "validation_hash",
        "plan_hash",
        "geometry_hash",
        "xyz_bytes_sha256",
        "first_input_hash",
        "authorization_hash",
    )(_hash)

    @field_validator("authorization_id", "task_id", "prepared_calculation_id", mode="after")
    @classmethod
    def _ids(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "authorization id"))

    @field_validator(
        "allowed_nodes", "budget", "geometry_policy", "node_credentials", mode="before"
    )
    @classmethod
    def _objects(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("issued_at_utc", "expires_at_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _invariants(self) -> WorkflowExecutionAuthorization:
        if self.expires_at_utc <= self.issued_at_utc:
            raise ValueError("workflow authorization expiry must be after issue time")
        payload = self.model_dump(mode="json", exclude={"authorization_hash"})
        if self.authorization_hash != sha256_hex(payload):
            raise ValueError("workflow authorization hash does not match content")
        return self

    @classmethod
    def create(cls, **values: object) -> WorkflowExecutionAuthorization:
        values.setdefault("schema_version", WORKFLOW_AUTHORIZATION_SCHEMA_VERSION)
        probe = cls.model_construct(**{**values, "authorization_hash": "0" * 64})
        values["authorization_hash"] = sha256_hex(
            probe.model_dump(mode="json", exclude={"authorization_hash"})
        )
        return cls(**values)

    def parent_binding(self) -> dict[str, str]:
        return {
            "authorization_id": self.authorization_id,
            "authorization_hash": self.authorization_hash,
            "prepared_calculation_id": self.prepared_calculation_id,
            "prepared_snapshot_hash": self.prepared_snapshot_hash,
        }

    def credential_for(self, node_id: str) -> str:
        credentials = dict(self.node_credentials)
        value = credentials.get(node_id)
        if not isinstance(value, str):
            raise ValueError("workflow authorization has no credential for the requested node")
        return value


__all__ = [
    "WORKFLOW_AUTHORIZATION_SCHEMA_VERSION",
    "WorkflowExecutionAuthorization",
]
