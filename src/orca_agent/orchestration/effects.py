"""Typed effect specifications emitted by pure reducer transitions."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator

from orca_agent.domain.ids import EffectId, EventId, effect_id_for
from orca_agent.domain.json_types import (
    FrozenJsonObject,
    JsonObject,
    freeze_json_object,
)

from .state import KernelModel


class EffectClass(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class EffectSpec(KernelModel):
    """An immutable effect to be registered in the transactional outbox."""

    effect_index: int = Field(ge=0)
    effect_type: str
    effect_class: EffectClass
    payload: FrozenJsonObject

    @field_validator("effect_type")
    @classmethod
    def _effect_type_is_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("effect_type must not be blank")
        return value.strip()

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_is_json_object(cls, value: JsonObject) -> FrozenJsonObject:
        try:
            return freeze_json_object(value)
        except ValueError as error:
            raise ValueError("effect payload must be a JSON object") from error

    def effect_id(self, source_event_id: EventId) -> EffectId:
        """Return the stable persisted ID for this effect."""

        return effect_id_for(source_event_id, self.effect_index)


__all__ = ["EffectClass", "EffectSpec"]
