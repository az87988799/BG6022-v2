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
GENESIS_EVENT_HASH = "0" * 64


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


def effect_spec_hash(
    *,
    effect_id: str,
    run_id: str,
    source_event_id: str,
    effect_index: int,
    effect_type: str,
    effect_class: str,
    schema_version: int,
    engine_version: str,
    payload: object,
    payload_hash: str,
) -> str:
    """Hash every immutable field of a persisted effect specification."""

    return sha256_hex(
        {
            "effect_class": effect_class,
            "effect_id": effect_id,
            "effect_index": effect_index,
            "effect_type": effect_type,
            "engine_version": engine_version,
            "payload": payload,
            "payload_hash": payload_hash,
            "run_id": run_id,
            "schema_version": schema_version,
            "source_event_id": source_event_id,
        }
    )


def event_envelope_hash(
    *,
    event_id: str,
    previous_event_hash: str,
    command_id: str,
    command_type: str,
    command_hash: str,
    run_id: str,
    sequence_no: int,
    expected_revision: int,
    new_revision: int,
    event_type: str,
    schema_version: int,
    engine_version: str,
    payload: object,
    payload_hash: str,
    result: object,
    result_hash: str,
    occurred_at_utc: str,
    recorded_at_utc: str,
) -> str:
    """Hash the complete persisted event envelope and its predecessor link."""

    return sha256_hex(
        {
            "command_hash": command_hash,
            "command_id": command_id,
            "command_type": command_type,
            "engine_version": engine_version,
            "event_id": event_id,
            "event_type": event_type,
            "expected_revision": expected_revision,
            "new_revision": new_revision,
            "occurred_at_utc": occurred_at_utc,
            "payload": payload,
            "payload_hash": payload_hash,
            "previous_event_hash": previous_event_hash,
            "recorded_at_utc": recorded_at_utc,
            "result": result,
            "result_hash": result_hash,
            "run_id": run_id,
            "schema_version": schema_version,
            "sequence_no": sequence_no,
        }
    )
