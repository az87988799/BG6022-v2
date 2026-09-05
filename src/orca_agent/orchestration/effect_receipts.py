"""Versioned, bounded success receipts for effect completion."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.ids import ArtifactId
from orca_agent.domain.json_types import JsonObject, thaw_json

from .state import KernelModel


class EffectSuccessReceiptV1(KernelModel):
    receipt_schema: Literal["effect-success/v1"] = "effect-success/v1"
    outcome_code: Literal["completed"] = "completed"
    artifact_ids: tuple[ArtifactId, ...] = Field(default=(), max_length=16)

    @field_validator("artifact_ids")
    @classmethod
    def _artifact_ids_are_unique(cls, values: tuple[ArtifactId, ...]) -> tuple[ArtifactId, ...]:
        if len(set(values)) != len(values):
            raise ValueError("artifact_ids must be unique")
        return values

    @model_validator(mode="after")
    def _receipt_is_bounded(self) -> EffectSuccessReceiptV1:
        if len(canonical_json_bytes(self.model_dump(mode="json"))) > 2048:
            raise ValueError("effect success receipt exceeds 2 KiB")
        return self


def parse_effect_success_receipt(value: object) -> EffectSuccessReceiptV1:
    """Parse a receipt through the strict JSON boundary."""

    if isinstance(value, EffectSuccessReceiptV1):
        return value
    try:
        raw = thaw_json(value)
        if not isinstance(raw, Mapping):
            raise ValueError("effect success receipt must be a JSON object")
        return EffectSuccessReceiptV1.model_validate_json(
            json.dumps(dict(raw), ensure_ascii=False), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        if (
            isinstance(error, ValueError)
            and str(error) == "effect success receipt must be a JSON object"
        ):
            raise
        raise ValueError("effect success receipt is invalid") from error


def receipt_json(value: EffectSuccessReceiptV1) -> JsonObject:
    return value.model_dump(mode="json")


__all__ = [
    "EffectSuccessReceiptV1",
    "parse_effect_success_receipt",
    "receipt_json",
]
