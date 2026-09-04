"""SHA-256 helpers bound to BG6022 Canonical JSON v1."""

from __future__ import annotations

import hashlib
import hmac
import re

from pydantic import BaseModel

from .canonical import canonical_json_bytes
from .errors import HashMismatchError
from .json_types import JsonValue

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def sha256_hex(value: JsonValue | BaseModel) -> str:
    """Hash a JSON-native value or Pydantic model as lowercase hex."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def verify_sha256(value: JsonValue | BaseModel, expected: str) -> None:
    """Raise a typed error unless ``expected`` matches the canonical hash."""

    actual = sha256_hex(value)
    if not isinstance(expected, str) or _HASH_PATTERN.fullmatch(expected) is None:
        raise HashMismatchError("expected hash is not lowercase SHA-256 hex")
    if not hmac.compare_digest(actual, expected):
        raise HashMismatchError(
            "canonical value hash does not match expected hash",
            details={"expected": expected, "actual": actual},
        )
