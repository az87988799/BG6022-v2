"""Schema version constants and strict validation."""

from __future__ import annotations

from typing import Literal

from .errors import UnsupportedSchemaVersionError

CURRENT_SCHEMA_VERSION = 1
SchemaVersion = Literal[1]


def validate_schema_version(value: object) -> SchemaVersion:
    """Accept only the currently supported integer schema version."""

    if type(value) is not int or value != CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            "unsupported schema version",
            details={"received": str(value), "supported": CURRENT_SCHEMA_VERSION},
        )
    return value
