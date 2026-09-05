"""Versioned typed commands accepted by the P2 kernel."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar, Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    CommandId,
    EffectId,
    InterruptId,
    RunId,
    new_id,
)
from orca_agent.domain.json_types import (
    FrozenJsonObject,
    JsonObject,
    JsonValue,
    freeze_json_object,
)
from orca_agent.domain.versions import CURRENT_SCHEMA_VERSION, validate_schema_version

from .codes import CancelReasonCode, HandlerErrorCode, handler_error_message
from .effect_receipts import EffectSuccessReceiptV1, parse_effect_success_receipt, receipt_json
from .effects import EffectSpec
from .state import KernelModel
from .temporal import ensure_utc


class CommandType(StrEnum):
    CREATE_RUN = "create_run"
    REQUEST_INTERRUPT = "request_interrupt"
    REPLACE_INTERRUPT = "replace_interrupt"
    RESOLVE_INTERRUPT = "resolve_interrupt"
    EXPIRE_INTERRUPT = "expire_interrupt"
    CANCEL_RUN = "cancel_run"
    RECORD_EFFECT_SUCCEEDED = "record_effect_succeeded"
    RECORD_EFFECT_FAILED = "record_effect_failed"


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
    "traceback",
)


def _safe_object(value: object, field_name: str) -> FrozenJsonObject:
    try:
        frozen = freeze_json_object(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a JSON object") from error

    def visit(current: JsonValue) -> None:
        if isinstance(current, Mapping):
            for key, child in current.items():
                normalized = key.casefold().replace("-", "_")
                if any(part in normalized for part in _UNSAFE_KEY_PARTS):
                    raise ValueError(f"{field_name} contains a forbidden key")
                visit(child)
        elif isinstance(current, (list, tuple)):
            for child in current:
                visit(child)

    visit(frozen)
    return frozen


def _non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


class CommandBase(KernelModel):
    """Common command envelope; concrete commands define the typed payload."""

    command_id: CommandId
    command_type: CommandType
    schema_version: int
    run_id: RunId
    expected_revision: int | None
    requested_at_utc: datetime

    _kind: ClassVar[CommandType]

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        return validate_schema_version(value)

    @field_validator("requested_at_utc")
    @classmethod
    def _requested_time_is_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _revision_shape(self) -> CommandBase:
        if self.command_type is CommandType.CREATE_RUN:
            if self.expected_revision is not None:
                raise ValueError("CreateRun expected_revision must be None")
        elif self.expected_revision is None or self.expected_revision < 1:
            raise ValueError("non-create commands require expected_revision >= 1")
        return self

    def command_hash(self) -> str:
        """Hash the complete command envelope, including its typed payload."""

        return sha256_hex(self.model_dump(mode="json"))

    def event_payload(self) -> JsonObject:
        """Return only the event-specific payload, excluding the envelope."""

        raise NotImplementedError


class CreateRun(CommandBase):
    command_type: Literal[CommandType.CREATE_RUN] = CommandType.CREATE_RUN
    expected_revision: None = None
    effects: tuple[EffectSpec, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId | None = None,
        command_id: CommandId | None = None,
        requested_at_utc: datetime | None = None,
        effects: tuple[EffectSpec, ...] = (),
    ) -> CreateRun:
        return cls(
            command_id=command_id or new_id(CommandId),
            schema_version=CURRENT_SCHEMA_VERSION,
            run_id=run_id or new_id(RunId),
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            effects=effects,
        )

    def event_payload(self) -> JsonObject:
        return {
            "run_id": str(self.run_id),
            "effects": [effect.model_dump(mode="json") for effect in self.effects],
        }


class RequestInterrupt(CommandBase):
    command_type: Literal[CommandType.REQUEST_INTERRUPT] = CommandType.REQUEST_INTERRUPT
    expected_revision: int = Field(ge=1)
    interrupt_id: InterruptId
    kind: str
    payload: FrozenJsonObject
    expires_at_utc: datetime

    @field_validator("kind")
    @classmethod
    def _kind_is_non_blank(cls, value: str) -> str:
        return _non_blank(value, "kind")

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_is_safe(cls, value: object) -> FrozenJsonObject:
        return _safe_object(value, "payload")

    @field_validator("expires_at_utc")
    @classmethod
    def _expiry_is_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def event_payload(self) -> JsonObject:
        return {
            "interrupt_id": str(self.interrupt_id),
            "kind": self.kind,
            "payload": self.payload,
            "expires_at_utc": self.expires_at_utc.isoformat().replace("+00:00", "Z"),
        }

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        expected_revision: int,
        kind: str,
        payload: JsonObject,
        expires_at_utc: datetime,
        interrupt_id: InterruptId | None = None,
        command_id: CommandId | None = None,
        requested_at_utc: datetime | None = None,
    ) -> RequestInterrupt:
        return cls(
            command_id=command_id or new_id(CommandId),
            command_type=CommandType.REQUEST_INTERRUPT,
            schema_version=CURRENT_SCHEMA_VERSION,
            run_id=run_id,
            expected_revision=expected_revision,
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            interrupt_id=interrupt_id or new_id(InterruptId),
            kind=kind,
            payload=payload,
            expires_at_utc=expires_at_utc,
        )


class ReplaceInterrupt(CommandBase):
    command_type: Literal[CommandType.REPLACE_INTERRUPT] = CommandType.REPLACE_INTERRUPT
    expected_revision: int = Field(ge=1)
    old_interrupt_id: InterruptId
    new_interrupt_id: InterruptId
    kind: str
    payload: FrozenJsonObject
    expires_at_utc: datetime

    @field_validator("kind")
    @classmethod
    def _kind_is_non_blank(cls, value: str) -> str:
        return _non_blank(value, "kind")

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_is_safe(cls, value: object) -> FrozenJsonObject:
        return _safe_object(value, "payload")

    @field_validator("expires_at_utc")
    @classmethod
    def _expiry_is_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _replacement_id_differs(self) -> ReplaceInterrupt:
        if self.old_interrupt_id == self.new_interrupt_id:
            raise ValueError("replacement interrupt ID must differ from old ID")
        return self

    def event_payload(self) -> JsonObject:
        return {
            "old_interrupt_id": str(self.old_interrupt_id),
            "new_interrupt_id": str(self.new_interrupt_id),
            "kind": self.kind,
            "payload": self.payload,
            "expires_at_utc": self.expires_at_utc.isoformat().replace("+00:00", "Z"),
        }

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        expected_revision: int,
        old_interrupt_id: InterruptId,
        kind: str,
        payload: JsonObject,
        expires_at_utc: datetime,
        new_interrupt_id: InterruptId | None = None,
        command_id: CommandId | None = None,
        requested_at_utc: datetime | None = None,
    ) -> ReplaceInterrupt:
        replacement_id = new_interrupt_id or new_id(InterruptId)
        if replacement_id == old_interrupt_id:
            replacement_id = new_id(InterruptId)
        return cls(
            command_id=command_id or new_id(CommandId),
            command_type=CommandType.REPLACE_INTERRUPT,
            schema_version=CURRENT_SCHEMA_VERSION,
            run_id=run_id,
            expected_revision=expected_revision,
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            old_interrupt_id=old_interrupt_id,
            new_interrupt_id=replacement_id,
            kind=kind,
            payload=payload,
            expires_at_utc=expires_at_utc,
        )


class ResolveInterrupt(CommandBase):
    command_type: Literal[CommandType.RESOLVE_INTERRUPT] = CommandType.RESOLVE_INTERRUPT
    expected_revision: int = Field(ge=1)
    interrupt_id: InterruptId
    response: FrozenJsonObject

    @field_validator("response", mode="before")
    @classmethod
    def _response_is_safe(cls, value: object) -> FrozenJsonObject:
        return _safe_object(value, "response")

    def event_payload(self) -> JsonObject:
        return {"interrupt_id": str(self.interrupt_id), "response": self.response}

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        expected_revision: int,
        interrupt_id: InterruptId,
        response: JsonObject,
        command_id: CommandId | None = None,
        requested_at_utc: datetime | None = None,
    ) -> ResolveInterrupt:
        return cls(
            command_id=command_id or new_id(CommandId),
            command_type=CommandType.RESOLVE_INTERRUPT,
            schema_version=CURRENT_SCHEMA_VERSION,
            run_id=run_id,
            expected_revision=expected_revision,
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            interrupt_id=interrupt_id,
            response=response,
        )


class ExpireInterrupt(CommandBase):
    command_type: Literal[CommandType.EXPIRE_INTERRUPT] = CommandType.EXPIRE_INTERRUPT
    expected_revision: int = Field(ge=1)
    interrupt_id: InterruptId

    def event_payload(self) -> JsonObject:
        return {"interrupt_id": str(self.interrupt_id)}

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        expected_revision: int,
        interrupt_id: InterruptId,
        command_id: CommandId | None = None,
        requested_at_utc: datetime | None = None,
    ) -> ExpireInterrupt:
        return cls(
            command_id=command_id or new_id(CommandId),
            command_type=CommandType.EXPIRE_INTERRUPT,
            schema_version=CURRENT_SCHEMA_VERSION,
            run_id=run_id,
            expected_revision=expected_revision,
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            interrupt_id=interrupt_id,
        )


class CancelRun(CommandBase):
    command_type: Literal[CommandType.CANCEL_RUN] = CommandType.CANCEL_RUN
    expected_revision: int = Field(ge=1)
    reason_code: CancelReasonCode

    def event_payload(self) -> JsonObject:
        return {"reason_code": self.reason_code.value}

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        expected_revision: int,
        reason_code: str,
        command_id: CommandId | None = None,
        requested_at_utc: datetime | None = None,
    ) -> CancelRun:
        return cls(
            command_id=command_id or new_id(CommandId),
            command_type=CommandType.CANCEL_RUN,
            schema_version=CURRENT_SCHEMA_VERSION,
            run_id=run_id,
            expected_revision=expected_revision,
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            reason_code=CancelReasonCode(reason_code),
        )


class RecordEffectSucceeded(CommandBase):
    command_type: Literal[CommandType.RECORD_EFFECT_SUCCEEDED] = CommandType.RECORD_EFFECT_SUCCEEDED
    expected_revision: int = Field(ge=1)
    effect_id: EffectId
    result_summary: EffectSuccessReceiptV1

    @field_validator("result_summary", mode="before")
    @classmethod
    def _summary_is_typed(cls, value: object) -> EffectSuccessReceiptV1:
        try:
            return parse_effect_success_receipt(value)
        except ValueError as error:
            raise ValueError("result_summary must be an effect-success/v1 receipt") from error

    def event_payload(self) -> JsonObject:
        return {
            "effect_id": str(self.effect_id),
            "result_summary": receipt_json(self.result_summary),
        }

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        expected_revision: int,
        effect_id: EffectId,
        result_summary: JsonObject,
        command_id: CommandId | None = None,
        requested_at_utc: datetime | None = None,
    ) -> RecordEffectSucceeded:
        return cls(
            command_id=command_id or new_id(CommandId),
            command_type=CommandType.RECORD_EFFECT_SUCCEEDED,
            schema_version=CURRENT_SCHEMA_VERSION,
            run_id=run_id,
            expected_revision=expected_revision,
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            effect_id=effect_id,
            result_summary=result_summary,
        )


class RecordEffectFailed(CommandBase):
    command_type: Literal[CommandType.RECORD_EFFECT_FAILED] = CommandType.RECORD_EFFECT_FAILED
    expected_revision: int = Field(ge=1)
    effect_id: EffectId
    error_code: HandlerErrorCode
    error_message: str | None = None

    @model_validator(mode="after")
    def _error_message_is_fixed(self) -> RecordEffectFailed:
        expected = handler_error_message(self.error_code)
        if self.error_message is not None and self.error_message != expected:
            raise ValueError("error_message must use the fixed public message for error_code")
        return self

    @field_validator("error_message")
    @classmethod
    def _error_message_is_bounded(cls, value: str | None) -> str | None:
        if value is not None and (len(value) > 256 or "\x00" in value):
            raise ValueError("error_message is too long or contains NUL")
        return value

    def event_payload(self) -> JsonObject:
        return {
            "effect_id": str(self.effect_id),
            "error_code": self.error_code.value,
            "error_message": handler_error_message(self.error_code),
        }

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        expected_revision: int,
        effect_id: EffectId,
        error_code: str,
        error_message: str | None = None,
        command_id: CommandId | None = None,
        requested_at_utc: datetime | None = None,
    ) -> RecordEffectFailed:
        return cls(
            command_id=command_id or new_id(CommandId),
            command_type=CommandType.RECORD_EFFECT_FAILED,
            schema_version=CURRENT_SCHEMA_VERSION,
            run_id=run_id,
            expected_revision=expected_revision,
            requested_at_utc=requested_at_utc or datetime.now(UTC),
            effect_id=effect_id,
            error_code=HandlerErrorCode(error_code),
            error_message=error_message,
        )


Command: TypeAlias = (
    CreateRun
    | RequestInterrupt
    | ReplaceInterrupt
    | ResolveInterrupt
    | ExpireInterrupt
    | CancelRun
    | RecordEffectSucceeded
    | RecordEffectFailed
)

CommandEnvelope = CommandBase


__all__ = [
    "CancelRun",
    "Command",
    "CommandBase",
    "CommandEnvelope",
    "CommandType",
    "CreateRun",
    "ExpireInterrupt",
    "RecordEffectFailed",
    "RecordEffectSucceeded",
    "ReplaceInterrupt",
    "RequestInterrupt",
    "ResolveInterrupt",
]
