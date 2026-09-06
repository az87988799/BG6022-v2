"""Closed, hashable registry contracts used by P4 plan preparation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..orchestration.p4_versions import P4_ENGINE_VERSION, P4_SCHEMA_VERSION
from .hashing import sha256_hex, verify_sha256
from .ids import WorkflowRecordId, new_id
from .json_types import FrozenJsonObject, JsonObject, freeze_json_object
from .p4 import P4Model

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


class RegistryKind(StrEnum):
    METHOD = "method"
    PRIMITIVE = "primitive"
    PROTOCOL = "protocol"
    CAPABILITY = "capability"


def _id(value: str, name: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a registry ID")
    return value


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank")
    return value.strip()


def _hash(value: str) -> str:
    if _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError("hash must be lowercase SHA-256 hex")
    return value


def _json_shape(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_shape(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_shape(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_json_shape(child) for child in value]
    if isinstance(value, list):
        return [_json_shape(child) for child in value]
    return value


class MethodRegistryEntry(RegistryModel):
    registry_id: str
    version: str
    orca_version: str
    category: str
    method_name: str
    declared_primitives: tuple[str, ...]
    maturity: str
    content: FrozenJsonObject
    entry_hash: str

    _ids = field_validator("registry_id")(lambda value: _id(value, "registry_id"))
    _texts = field_validator("version", "orca_version", "category", "method_name", "maturity")(
        lambda value, info: _text(value, getattr(info, "field_name", "method"))
    )
    _entry_hash = field_validator("entry_hash")(_hash)

    @field_validator("declared_primitives")
    @classmethod
    def _primitives(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_id(item, "declared primitive") for item in value)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("declared primitives must be unique")
        return cleaned

    @field_validator("content", mode="before")
    @classmethod
    def _content(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @model_validator(mode="after")
    def _hash_matches(self) -> MethodRegistryEntry:
        verify_hash(self, "entry_hash", self.entry_hash)
        return self

    @classmethod
    def create(
        cls,
        *,
        registry_id: str,
        version: str,
        orca_version: str,
        category: str,
        method_name: str,
        declared_primitives: tuple[str, ...],
        maturity: str,
        content: JsonObject,
    ) -> MethodRegistryEntry:
        values = {
            "registry_id": registry_id,
            "version": version,
            "orca_version": orca_version,
            "category": category,
            "method_name": method_name,
            "declared_primitives": declared_primitives,
            "maturity": maturity,
            "content": content,
        }
        return cls(**values, entry_hash=sha256_hex(_json_shape(values)))


class PrimitiveRegistryEntry(RegistryModel):
    registry_id: str
    version: str
    kind: str
    input_geometry: str
    output_geometry: str
    allowed_parameters: tuple[str, ...]
    content: FrozenJsonObject
    entry_hash: str

    _id_validator = field_validator("registry_id")(lambda value: _id(value, "registry_id"))
    _text_validator = field_validator("version", "kind", "input_geometry", "output_geometry")(
        lambda value, info: _text(value, getattr(info, "field_name", "primitive"))
    )
    _entry_hash = field_validator("entry_hash")(_hash)

    @field_validator("allowed_parameters")
    @classmethod
    def _parameters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_id(item, "allowed parameter") for item in value)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("allowed parameters must be unique")
        return cleaned

    @field_validator("content", mode="before")
    @classmethod
    def _content(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @model_validator(mode="after")
    def _hash_matches(self) -> PrimitiveRegistryEntry:
        verify_hash(self, "entry_hash", self.entry_hash)
        return self

    @classmethod
    def create(
        cls,
        *,
        registry_id: str,
        version: str,
        kind: str,
        input_geometry: str,
        output_geometry: str,
        allowed_parameters: tuple[str, ...],
        content: JsonObject,
    ) -> PrimitiveRegistryEntry:
        values = {
            "registry_id": registry_id,
            "version": version,
            "kind": kind,
            "input_geometry": input_geometry,
            "output_geometry": output_geometry,
            "allowed_parameters": allowed_parameters,
            "content": content,
        }
        return cls(**values, entry_hash=sha256_hex(_json_shape(values)))


class ProtocolRegistryEntry(RegistryModel):
    registry_id: str
    version: str
    environment: str
    supported_elements: tuple[str, ...]
    max_non_h_atoms: int = Field(gt=0)
    method_profile_id: str
    primitive_template: tuple[str, ...]
    content: FrozenJsonObject
    entry_hash: str

    _id_validator = field_validator("registry_id", "method_profile_id")(
        lambda value, info: _id(value, getattr(info, "field_name", "registry ID"))
    )
    _version = field_validator("version")(lambda value: _text(value, "version"))
    _environment = field_validator("environment")(lambda value: _text(value, "environment"))
    _hash_validator = field_validator("entry_hash")(_hash)

    @field_validator("supported_elements", "primitive_template")
    @classmethod
    def _unique_text(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_text(item, "protocol item") for item in value)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("protocol items must be unique")
        return cleaned

    @field_validator("content", mode="before")
    @classmethod
    def _content(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @model_validator(mode="after")
    def _hash_matches(self) -> ProtocolRegistryEntry:
        verify_hash(self, "entry_hash", self.entry_hash)
        return self

    @classmethod
    def create(
        cls,
        *,
        registry_id: str,
        version: str,
        environment: str,
        supported_elements: tuple[str, ...],
        max_non_h_atoms: int,
        method_profile_id: str,
        primitive_template: tuple[str, ...],
        content: JsonObject,
    ) -> ProtocolRegistryEntry:
        values = {
            "registry_id": registry_id,
            "version": version,
            "environment": environment,
            "supported_elements": supported_elements,
            "max_non_h_atoms": max_non_h_atoms,
            "method_profile_id": method_profile_id,
            "primitive_template": primitive_template,
            "content": content,
        }
        return cls(**values, entry_hash=sha256_hex(_json_shape(values)))


class CapabilityRegistryEntry(RegistryModel):
    registry_id: str
    version: str
    mode: str
    can_plan: bool
    can_execute: bool
    content: FrozenJsonObject
    entry_hash: str

    _id_validator = field_validator("registry_id")(lambda value: _id(value, "registry_id"))
    _text_validator = field_validator("version", "mode")(
        lambda value, info: _text(value, getattr(info, "field_name", "capability"))
    )
    _hash_validator = field_validator("entry_hash")(_hash)

    @field_validator("content", mode="before")
    @classmethod
    def _content(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @model_validator(mode="after")
    def _hash_matches(self) -> CapabilityRegistryEntry:
        verify_hash(self, "entry_hash", self.entry_hash)
        return self

    @classmethod
    def create(
        cls,
        *,
        registry_id: str,
        version: str,
        mode: str,
        can_plan: bool,
        can_execute: bool,
        content: JsonObject,
    ) -> CapabilityRegistryEntry:
        values = {
            "registry_id": registry_id,
            "version": version,
            "mode": mode,
            "can_plan": can_plan,
            "can_execute": can_execute,
            "content": content,
        }
        return cls(**values, entry_hash=sha256_hex(_json_shape(values)))


class RegistrySnapshot(P4Model):
    """The registry contents frozen for one planning run."""

    record_id: WorkflowRecordId
    schema_version: int
    engine_version: str
    registry_version: str
    methods: tuple[MethodRegistryEntry, ...]
    primitives: tuple[PrimitiveRegistryEntry, ...]
    protocols: tuple[ProtocolRegistryEntry, ...]
    capabilities: tuple[CapabilityRegistryEntry, ...]
    manifest: tuple[FrozenJsonObject, ...]
    manifest_hash: str
    snapshot_hash: str

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if type(value) is not int or value != P4_SCHEMA_VERSION:
            raise ValueError("registry snapshot schema_version must be 3")
        return value

    @field_validator("engine_version")
    @classmethod
    def _engine(cls, value: str) -> str:
        if value != P4_ENGINE_VERSION:
            raise ValueError("registry snapshot engine_version is unsupported")
        return value

    _hashes = field_validator("manifest_hash", "snapshot_hash")(_hash)

    @field_validator("registry_version")
    @classmethod
    def _registry_version(cls, value: str) -> str:
        return _text(value, "registry_version")

    @field_validator("manifest", mode="before")
    @classmethod
    def _manifest(cls, value: object) -> tuple[FrozenJsonObject, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("registry manifest must be an array")
        return tuple(freeze_json_object(item) for item in value)

    @model_validator(mode="after")
    def _snapshot_hashes(self) -> RegistrySnapshot:
        entries = []
        keys = set()
        for kind, values in (
            (RegistryKind.METHOD.value, self.methods),
            (RegistryKind.PRIMITIVE.value, self.primitives),
            (RegistryKind.PROTOCOL.value, self.protocols),
            (RegistryKind.CAPABILITY.value, self.capabilities),
        ):
            for entry in values:
                key = (kind, entry.registry_id, entry.version)
                if key in keys:
                    raise ValueError("registry entry identity is duplicated")
                keys.add(key)
                entries.append(
                    dict(
                        kind=kind,
                        registry_id=entry.registry_id,
                        version=entry.version,
                        entry_hash=entry.entry_hash,
                    )
                )
        entries.sort(key=lambda item: (item["kind"], item["registry_id"], item["version"]))
        if tuple(entries) != self.manifest:
            raise ValueError("registry manifest does not match actual entries")
        expected_manifest = tuple(
            sorted(
                self.manifest,
                key=lambda item: (
                    str(item.get("kind", "")),
                    str(item.get("registry_id", "")),
                    str(item.get("version", "")),
                ),
            )
        )
        if expected_manifest != self.manifest:
            raise ValueError("registry manifest must use stable ordering")
        if sha256_hex([dict(item) for item in self.manifest]) != self.manifest_hash:
            raise ValueError("registry manifest hash does not match")
        values = self.model_dump(mode="json", exclude={"snapshot_hash"})
        if sha256_hex(values) != self.snapshot_hash:
            raise ValueError("registry snapshot hash does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        registry_version: str,
        methods: tuple[MethodRegistryEntry, ...],
        primitives: tuple[PrimitiveRegistryEntry, ...],
        protocols: tuple[ProtocolRegistryEntry, ...],
        capabilities: tuple[CapabilityRegistryEntry, ...],
        record_id: WorkflowRecordId | None = None,
    ) -> RegistrySnapshot:
        manifest_items: list[JsonObject] = []
        for kind, entries in (
            (RegistryKind.METHOD.value, methods),
            (RegistryKind.PRIMITIVE.value, primitives),
            (RegistryKind.PROTOCOL.value, protocols),
            (RegistryKind.CAPABILITY.value, capabilities),
        ):
            for entry in entries:
                manifest_items.append(
                    {
                        "kind": kind,
                        "registry_id": entry.registry_id,
                        "version": entry.version,
                        "entry_hash": entry.entry_hash,
                    }
                )
        manifest_items.sort(key=lambda item: (item["kind"], item["registry_id"], item["version"]))
        manifest_hash = sha256_hex(manifest_items)
        values = {
            "record_id": str(record_id or new_id(WorkflowRecordId)),
            "schema_version": P4_SCHEMA_VERSION,
            "engine_version": P4_ENGINE_VERSION,
            "registry_version": registry_version,
            "methods": methods,
            "primitives": primitives,
            "protocols": protocols,
            "capabilities": capabilities,
            "manifest": manifest_items,
            "manifest_hash": manifest_hash,
        }
        return cls(**values, snapshot_hash=sha256_hex(_json_shape(values)))


def verify_hash(model: BaseModel, field: str, value: str) -> None:
    try:
        verify_sha256(model.model_dump(mode="json", exclude={field}), value)
    except Exception as error:
        raise ValueError(f"{field} does not match registry content") from error


__all__ = [
    "CapabilityRegistryEntry",
    "MethodRegistryEntry",
    "PrimitiveRegistryEntry",
    "ProtocolRegistryEntry",
    "RegistryKind",
    "RegistryModel",
    "RegistrySnapshot",
]
