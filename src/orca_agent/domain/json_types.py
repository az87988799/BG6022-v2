"""Recursive JSON type aliases used by the domain boundary."""

from __future__ import annotations

from typing import Any, TypeAlias

JsonPrimitive: TypeAlias = None | bool | int | float | str
# The recursive shape is enforced by the canonicalizer at runtime. Keeping
# the Pydantic-facing alias non-recursive preserves Python 3.11 support.
JsonValue: TypeAlias = JsonPrimitive | list[Any] | dict[str, Any]
JsonObject: TypeAlias = dict[str, Any]
