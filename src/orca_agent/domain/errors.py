"""Stable, safe errors exposed by domain primitives."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from pydantic import ValidationError

from .json_types import JsonObject, JsonValue


class OrcaAgentError(Exception):
    """Base error with a stable code and non-sensitive structured details."""

    code: ClassVar[str] = "orca_agent_error"

    def __init__(self, message: str, *, details: Mapping[str, JsonValue] | None = None) -> None:
        self.message = message
        self.details: JsonObject = dict(details or {})
        super().__init__(message)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class DomainError(OrcaAgentError):
    """Base class for deterministic domain failures."""

    code = "domain_error"


class InvalidIdentifierError(DomainError):
    code = "invalid_identifier"


class UnsupportedSchemaVersionError(DomainError):
    code = "unsupported_schema_version"


class CanonicalizationError(DomainError):
    code = "canonicalization_error"


class HashMismatchError(DomainError):
    code = "hash_mismatch"


class ContractInvariantError(DomainError):
    code = "contract_invariant_error"


def contract_error_from_validation(error: ValidationError) -> ContractInvariantError:
    """Convert Pydantic details into a safe, stable domain error."""

    safe_errors: list[JsonObject] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        safe_errors.append(
            {
                "loc": location,
                "type": str(item.get("type", "validation_error")),
                "message": str(item.get("msg", "invalid value")),
            }
        )
    return ContractInvariantError(
        "domain contract validation failed",
        details={"errors": safe_errors},
    )
