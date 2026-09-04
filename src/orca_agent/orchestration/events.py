"""Versioned immutable events persisted by the durable kernel."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from orca_agent.application.results import ApplicationResult
from orca_agent.domain.errors import HashMismatchError
from orca_agent.domain.hashing import sha256_hex, verify_sha256
from orca_agent.domain.ids import CommandId, EventId, RunId, new_id
from orca_agent.domain.json_types import FrozenJsonObject, JsonObject, freeze_json_object
from orca_agent.domain.versions import CURRENT_SCHEMA_VERSION, validate_schema_version

from .commands import CommandType
from .state import KernelModel
from .temporal import ensure_utc
from .versions import ENGINE_VERSION

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class EventType(StrEnum):
    RUN_CREATED = "run_created"
    INTERRUPT_REQUESTED = "interrupt_requested"
    INTERRUPT_REPLACED = "interrupt_replaced"
    INTERRUPT_RESOLVED = "interrupt_resolved"
    INTERRUPT_EXPIRED = "interrupt_expired"
    RUN_CANCELLED = "run_cancelled"
    EFFECT_SUCCEEDED = "effect_succeeded"
    EFFECT_DEAD_LETTERED = "effect_dead_lettered"


class KernelEvent(KernelModel):
    """An append-only event with independently verifiable payload and result."""

    event_id: EventId
    command_id: CommandId
    command_type: CommandType
    run_id: RunId
    sequence_no: int = Field(ge=1)
    expected_revision: int = Field(ge=0)
    new_revision: int = Field(ge=1)
    event_type: EventType
    schema_version: int
    engine_version: str
    payload: FrozenJsonObject
    payload_hash: str
    result: FrozenJsonObject
    result_hash: str
    occurred_at_utc: datetime
    recorded_at_utc: datetime

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        return validate_schema_version(value)

    @field_validator("payload", "result", mode="before")
    @classmethod
    def _objects_are_json(cls, value: object) -> FrozenJsonObject:
        try:
            return freeze_json_object(value)
        except ValueError as error:
            raise ValueError("event payload and result must be JSON objects") from error

    @field_validator("payload_hash", "result_hash")
    @classmethod
    def _hashes_are_canonical_shape(cls, value: str) -> str:
        if _HASH_PATTERN.fullmatch(value) is None:
            raise ValueError("hash must be lowercase SHA-256 hex")
        return value

    @field_validator("occurred_at_utc", "recorded_at_utc")
    @classmethod
    def _times_are_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _event_invariants(self) -> KernelEvent:
        if self.new_revision != self.expected_revision + 1:
            raise ValueError("new_revision must equal expected_revision + 1")
        if self.sequence_no != self.new_revision:
            raise ValueError("sequence_no must equal new_revision")
        try:
            verify_sha256(self.payload, self.payload_hash)
            verify_sha256(self.result, self.result_hash)
        except HashMismatchError as error:
            raise ValueError("event content hash does not match") from error
        return self

    @classmethod
    def create(
        cls,
        *,
        command_id: CommandId,
        command_type: CommandType,
        run_id: RunId,
        sequence_no: int,
        expected_revision: int,
        event_type: EventType,
        payload: JsonObject,
        result: ApplicationResult | JsonObject,
        occurred_at_utc: datetime,
        recorded_at_utc: datetime | None = None,
        event_id: EventId | None = None,
        engine_version: str = ENGINE_VERSION,
        schema_version: int = CURRENT_SCHEMA_VERSION,
    ) -> KernelEvent:
        result_value = (
            result.model_dump(mode="json") if isinstance(result, ApplicationResult) else result
        )
        return cls(
            event_id=event_id or new_id(EventId),
            command_id=command_id,
            command_type=command_type,
            run_id=run_id,
            sequence_no=sequence_no,
            expected_revision=expected_revision,
            new_revision=sequence_no,
            event_type=event_type,
            schema_version=schema_version,
            engine_version=engine_version,
            payload=payload,
            payload_hash=sha256_hex(payload),
            result=result_value,
            result_hash=sha256_hex(result_value),
            occurred_at_utc=occurred_at_utc,
            recorded_at_utc=recorded_at_utc or occurred_at_utc,
        )

    def verify_payload_hash(self) -> None:
        verify_sha256(self.payload, self.payload_hash)

    def verify_result_hash(self) -> None:
        verify_sha256(self.result, self.result_hash)


EventEnvelope = KernelEvent

__all__ = ["EventEnvelope", "EventType", "KernelEvent"]
