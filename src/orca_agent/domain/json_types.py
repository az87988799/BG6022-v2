"""Recursive JSON type aliases used by the domain boundary."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, TypeAlias

from pydantic import PlainSerializer

JsonPrimitive: TypeAlias = None | bool | int | float | str
# The recursive shape is enforced by the canonicalizer at runtime. Keeping
# the Pydantic-facing alias non-recursive preserves Python 3.11 support.
JsonValue: TypeAlias = JsonPrimitive | list[Any] | dict[str, Any]
JsonObject: TypeAlias = dict[str, Any]


class FrozenList(tuple[Any, ...]):
    """Immutable JSON array used inside frozen domain contracts."""


class FrozenDict(Mapping[str, Any]):
    """Immutable JSON object with an immutable backing mapping."""

    __slots__ = ("_data",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_data", MappingProxyType(dict(values)))

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("FrozenDict is immutable")

    def __delattr__(self, _name: str) -> None:
        raise TypeError("FrozenDict is immutable")


FrozenJsonValue: TypeAlias = Annotated[
    Any,
    PlainSerializer(lambda value: thaw_json(value), return_type=Any, when_used="json"),
]
FrozenJsonObject: TypeAlias = FrozenJsonValue


def freeze_json_value(value: object) -> object:
    """Recursively validate and freeze a JSON-native value."""

    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite numeric values are not allowed")
        return value
    if isinstance(value, FrozenDict | FrozenList):
        return value
    if isinstance(value, list):
        return FrozenList(freeze_json_value(item) for item in value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return FrozenDict({key: freeze_json_value(item) for key, item in value.items()})
    raise ValueError(f"value contains a non-JSON type: {type(value).__name__}")


def freeze_json_object(value: object) -> FrozenDict:
    """Recursively validate and freeze a JSON object."""

    frozen = freeze_json_value(value)
    if not isinstance(frozen, FrozenDict):
        raise ValueError("value must be a JSON object")
    return frozen


def thaw_json(value: object) -> object:
    """Convert immutable JSON containers back to JSON-native containers."""

    if isinstance(value, FrozenDict):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, FrozenList):
        return [thaw_json(item) for item in value]
    if isinstance(value, list):
        return [thaw_json(item) for item in value]
    if isinstance(value, dict):
        return {key: thaw_json(item) for key, item in value.items()}
    return value
