"""BG6022 Canonical JSON v1."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from .errors import CanonicalizationError
from .json_types import FrozenList, JsonValue


def _normalize(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("non-finite numeric values are not allowed")
        return value
    if isinstance(value, FrozenList):
        return [_normalize(item) for item in value]
    if isinstance(value, tuple):
        raise CanonicalizationError("tuples are not JSON arrays")
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise CanonicalizationError("JSON object keys must be strings")
        return {key: _normalize(value[key]) for key in sorted(value)}
    raise CanonicalizationError(
        "value contains a non-JSON type",
        details={"type": type(value).__name__},
    )


def canonical_json_bytes(value: JsonValue | BaseModel) -> bytes:
    """Return deterministic UTF-8 JSON bytes under Canonical JSON v1."""

    try:
        json_value = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        normalized = _normalize(json_value)
        rendered = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except CanonicalizationError:
        raise
    except (TypeError, ValueError, OverflowError) as error:
        raise CanonicalizationError("value cannot be canonicalized") from error
    return rendered.encode("utf-8")
