"""Serializable request/response ports shared by baseline and DeepSeek."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from pydantic import Field, field_validator

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.json_types import FrozenJsonObject, freeze_json_object
from orca_agent.domain.p7_conversation import ContextSnapshot, P7Model, TurnInterpretation
from orca_agent.domain.p7_intake import TurnInterpretationV4, TurnInterpretationV5
from orca_agent.orchestration.p7_versions import TURN_SCHEMA_V4, TURN_SCHEMA_V5


class ModelMessage(P7Model):
    role: str
    content: str

    @field_validator("role", "content")
    @classmethod
    def _message_text(cls, value: str, info: object) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"{getattr(info, 'field_name', 'message')} is invalid")
        return value


class ModelCallRequest(P7Model):
    adapter_id: str
    model: str
    messages: tuple[ModelMessage, ...] = Field(min_length=1, max_length=32)
    response_format: FrozenJsonObject
    max_tokens: int = Field(default=2048, ge=1, le=2048)
    stream: bool = False
    thinking_enabled: bool = False
    request_hash: str

    @field_validator("adapter_id", "model")
    @classmethod
    def _adapter_text(cls, value: str, info: object) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError(f"{getattr(info, 'field_name', 'adapter')} is invalid")
        return value.strip()

    @field_validator("response_format", mode="before")
    @classmethod
    def _format(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object(value)

    @field_validator("request_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        if (
            len(value) != 64
            or value != value.casefold()
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("request_hash is invalid")
        return value

    @classmethod
    def create(
        cls,
        *,
        adapter_id: str,
        model: str,
        messages: Sequence[ModelMessage],
        response_format: Mapping[str, object] | None = None,
        max_tokens: int = 2048,
        stream: bool = False,
        thinking_enabled: bool = False,
    ) -> ModelCallRequest:
        values = {
            "adapter_id": adapter_id,
            "model": model,
            "messages": tuple(messages),
            "response_format": {"type": "json_object"}
            if response_format is None
            else response_format,
            "max_tokens": max_tokens,
            "stream": stream,
            "thinking_enabled": thinking_enabled,
        }
        probe = cls.model_construct(**{**values, "request_hash": "0" * 64})
        return cls(
            **values,
            request_hash=sha256_hex(probe.model_dump(mode="json", exclude={"request_hash"})),
        )


class ModelCallResponse(P7Model):
    provider: str
    model: str | None = None
    content: str | None = None
    raw_bytes: bytes | None = None
    provider_request_id: str | None = None
    usage: FrozenJsonObject = {}
    error_code: str | None = None
    error_message: str | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider is blank")
        return value.strip()

    @field_validator("model", "content", "provider_request_id", "error_code", "error_message")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        return None if value is None else value[:16_384]

    @field_validator("usage", mode="before")
    @classmethod
    def _usage(cls, value: object) -> FrozenJsonObject:
        return freeze_json_object({} if value is None else value)


class PlannerPort(Protocol):
    adapter_id: str

    def interpret(
        self, context: ContextSnapshot
    ) -> ModelCallResponse | TurnInterpretation | TurnInterpretationV4 | TurnInterpretationV5:
        """Return a raw model response or an already validated interpretation."""


class CallablePlanner:
    """Small test adapter for controlled response/failure injection."""

    adapter_id = "callable"

    def __init__(
        self,
        callback: Callable[
            [ContextSnapshot],
            ModelCallResponse | TurnInterpretation | TurnInterpretationV4 | TurnInterpretationV5,
        ],
    ) -> None:
        self._callback = callback

    def interpret(
        self, context: ContextSnapshot
    ) -> ModelCallResponse | TurnInterpretation | TurnInterpretationV4 | TurnInterpretationV5:
        return self._callback(context)


def strict_json_loads(value: str | bytes) -> object:
    """Decode JSON while rejecting duplicate keys and non-finite constants."""

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, child in items:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = child
        return result

    return json.loads(
        value,
        object_pairs_hook=pairs,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {token}")
        ),
    )


def validate_interpretation(
    value: object,
) -> TurnInterpretation | TurnInterpretationV4 | TurnInterpretationV5:
    if isinstance(value, TurnInterpretation):
        return value
    if isinstance(value, TurnInterpretationV4):
        return value
    if isinstance(value, TurnInterpretationV5):
        return value
    if isinstance(value, bytes):
        raw = strict_json_loads(value)
    elif isinstance(value, str):
        raw = strict_json_loads(value)
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raw = value
    if isinstance(raw, Mapping) and raw.get("schema_version") == TURN_SCHEMA_V4:
        # JSON arrays are intentionally represented as tuples by the domain
        # models.  Parse the original JSON again so strict validation accepts
        # the wire-format arrays while still retaining duplicate-key checks.
        encoded = (
            value
            if isinstance(value, (str, bytes))
            else json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        )
        return TurnInterpretationV4.model_validate_json(encoded, strict=True)
    if isinstance(raw, Mapping) and raw.get("schema_version") == TURN_SCHEMA_V5:
        encoded = (
            value
            if isinstance(value, (str, bytes))
            else json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        )
        return TurnInterpretationV5.model_validate_json(encoded, strict=True)
    if isinstance(value, bytes):
        return TurnInterpretation.model_validate_json(value, strict=True)
    if isinstance(value, str):
        return TurnInterpretation.model_validate_json(value, strict=True)
    if isinstance(value, Mapping):
        return TurnInterpretation.model_validate_json(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")), strict=True
        )
    return TurnInterpretation.model_validate(value, strict=True)


__all__ = [
    "CallablePlanner",
    "ModelCallRequest",
    "ModelCallResponse",
    "ModelMessage",
    "PlannerPort",
    "strict_json_loads",
    "validate_interpretation",
]
