"""Explicit, read-only P7 capability catalog and extension seam."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import field_validator, model_validator

from orca_agent.domain.hashing import sha256_hex, verify_sha256
from orca_agent.domain.ids import WorkflowRecordId, new_id
from orca_agent.domain.p7_task import OutputKind, P7Model
from orca_agent.orchestration.p7_versions import CAPABILITY_VIEW_SCHEMA
from orca_agent.planning.p5_protocols import P5_DEFAULT_PROTOCOL
from orca_agent.planning.registry import METHOD_R2SCAN3C, fixed_registry_snapshot

P7_BASELINE_CAPABILITY_ID = "chem.ground_state_baseline.v1"
P7_BASELINE_CAPABILITY_VERSION = "1"


def _text(value: str, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{name} is too long")
    return value


def _hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("hash must be lowercase SHA-256 hex")
    return value


class CapabilityDescriptor(P7Model):
    """A projection of existing registrations plus an implementation binding."""

    schema_version: str = CAPABILITY_VIEW_SCHEMA
    capability_id: str
    version: str
    content_hash: str
    purpose: str
    input_kinds: tuple[str, ...]
    output_kinds: tuple[OutputKind, ...]
    method_profile_id: str
    method_profile_hash: str
    environment: str
    protocol_id: str
    protocol_hash: str
    registry_snapshot_id: str
    registry_snapshot_hash: str
    implementation_id: str
    execution_kind: str
    dependencies: tuple[str, ...] = ()
    enabled: bool = True

    _hash = field_validator(
        "content_hash", "method_profile_hash", "protocol_hash", "registry_snapshot_hash"
    )(_hash)

    @field_validator(
        "capability_id",
        "version",
        "purpose",
        "method_profile_id",
        "environment",
        "protocol_id",
        "registry_snapshot_id",
        "implementation_id",
        "execution_kind",
    )
    @classmethod
    def _descriptor_text(cls, value: str, info: object) -> str:
        return _text(value, getattr(info, "field_name", "descriptor"))

    @field_validator("input_kinds", "dependencies")
    @classmethod
    def _descriptor_collections(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        cleaned = tuple(_text(item, getattr(info, "field_name", "value")) for item in value)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("descriptor collection has duplicates")
        return cleaned

    @model_validator(mode="after")
    def _descriptor_content(self) -> CapabilityDescriptor:
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        verify_sha256(payload, self.content_hash)
        return self

    @classmethod
    def create(cls, **values: object) -> CapabilityDescriptor:
        values["content_hash"] = sha256_hex(
            cls.model_construct(**{**values, "content_hash": "0" * 64}).model_dump(
                mode="json", exclude={"content_hash"}
            )
        )
        return cls(**values)


@runtime_checkable
class CapabilityModule(Protocol):
    """Small extension contract used by tests and future non-ORCA modules."""

    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    def validate_input(self, value: object) -> object: ...

    def execute(self, value: object) -> object: ...

    def project_result(self, value: object) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class RegisteredCapability:
    descriptor: CapabilityDescriptor
    module: CapabilityModule | None = None


class CapabilityCatalog:
    """Closed catalog assembled once at application bootstrap."""

    def __init__(self, entries: Sequence[RegisteredCapability] = ()) -> None:
        values: dict[tuple[str, str], RegisteredCapability] = {}
        for entry in entries:
            key = (entry.descriptor.capability_id, entry.descriptor.version)
            prior = values.get(key)
            if prior is not None and prior.descriptor.content_hash != entry.descriptor.content_hash:
                raise ValueError("capability ID/version is registered with different content")
            if prior is not None and prior.module is not entry.module:
                raise ValueError("capability ID/version is registered more than once")
            values[key] = entry
        self._entries = values

    def list(self, *, enabled_only: bool = True) -> tuple[CapabilityDescriptor, ...]:
        values = tuple(
            entry.descriptor
            for entry in self._entries.values()
            if not enabled_only or entry.descriptor.enabled
        )
        return tuple(sorted(values, key=lambda item: (item.capability_id, item.version)))

    def get(self, capability_id: str, version: str | None = None) -> RegisteredCapability | None:
        matches = [
            entry
            for (item_id, item_version), entry in self._entries.items()
            if item_id == capability_id and (version is None or item_version == version)
        ]
        if not matches:
            return None
        if version is None and len(matches) > 1:
            raise ValueError("capability version is ambiguous")
        return matches[0]

    def require(self, capability_id: str, version: str | None = None) -> RegisteredCapability:
        entry = self.get(capability_id, version)
        if entry is None or not entry.descriptor.enabled:
            raise ValueError("capability is unavailable")
        return entry

    def module_for(self, capability_id: str, version: str | None = None) -> CapabilityModule | None:
        return self.require(capability_id, version).module

    def register(self, module: CapabilityModule) -> CapabilityCatalog:
        """Return a new catalog; runtime hot reload is intentionally unavailable."""

        if not isinstance(module.descriptor, CapabilityDescriptor):
            raise TypeError("capability module has no valid descriptor")
        return CapabilityCatalog(
            (*self._entries.values(), RegisteredCapability(module.descriptor, module))
        )


def _baseline_descriptor() -> CapabilityDescriptor:
    registry = fixed_registry_snapshot(record_id=new_id(WorkflowRecordId))
    return CapabilityDescriptor.create(
        capability_id=P7_BASELINE_CAPABILITY_ID,
        version=P7_BASELINE_CAPABILITY_VERSION,
        purpose="one starting structure, gas-phase r²SCAN-3c Opt → Freq → independent SP",
        input_kinds=("name", "cas", "cid", "smiles"),
        output_kinds=tuple(OutputKind),
        method_profile_id=METHOD_R2SCAN3C.registry_id,
        method_profile_hash=METHOD_R2SCAN3C.entry_hash,
        environment="gas",
        protocol_id=P5_DEFAULT_PROTOCOL.protocol_id,
        protocol_hash=P5_DEFAULT_PROTOCOL.protocol_hash,
        registry_snapshot_id=str(registry.record_id),
        registry_snapshot_hash=registry.snapshot_hash,
        implementation_id="orca.p5.p6.baseline",
        execution_kind="registered_workflow",
        dependencies=("p4", "p5", "p6"),
    )


def build_capability_catalog(
    extra_modules: Iterable[CapabilityModule] = (),
) -> CapabilityCatalog:
    catalog = CapabilityCatalog((RegisteredCapability(_baseline_descriptor()),))
    for module in extra_modules:
        catalog = catalog.register(module)
    return catalog


__all__ = [
    "CapabilityCatalog",
    "CapabilityDescriptor",
    "CapabilityModule",
    "P7_BASELINE_CAPABILITY_ID",
    "P7_BASELINE_CAPABILITY_VERSION",
    "RegisteredCapability",
    "build_capability_catalog",
]
