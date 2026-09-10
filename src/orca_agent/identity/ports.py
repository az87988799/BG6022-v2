"""Small provider and normalizer ports with no network/scientific imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from orca_agent.domain.p4 import IdentityProvider, MoleculeInputKind, MoleculeQuery


class IdentityErrorCode(StrEnum):
    LOOKUP_TIMEOUT = "identity_lookup_timeout"
    PROVIDER_THROTTLED = "identity_provider_throttled"
    PROVIDER_UNAVAILABLE = "identity_provider_unavailable"
    QUERY_REJECTED = "identity_query_rejected"
    NOT_FOUND = "identity_not_found"
    PROVIDER_SCHEMA_ERROR = "identity_provider_schema_error"
    RESULT_LIMIT_EXCEEDED = "identity_result_limit_exceeded"
    NETWORK_DISABLED = "identity_network_disabled"
    INVALID_IDENTITY = "invalid_identity"
    PROTOCOL_UNSUPPORTED = "protocol_unsupported"


@dataclass(frozen=True)
class IdentityLookupRequest:
    """Provider lookup contract independent from calculation q/M."""

    input_kind: MoleculeInputKind
    normalized_value: str
    query_constraints: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.input_kind, MoleculeInputKind):
            raise TypeError("identity lookup input_kind is invalid")
        if not isinstance(self.normalized_value, str) or not self.normalized_value.strip():
            raise ValueError("identity lookup value is blank")
        if "\x00" in self.normalized_value:
            raise ValueError("identity lookup value contains NUL")


@dataclass(frozen=True)
class RawIdentityCandidate:
    """Provider output before the shared RDKit normalizer runs."""

    candidate_id: str
    smiles: str
    cid: int | None = None
    inchikey: str | None = None
    molecular_formula: str | None = None
    formal_charge: int | None = None
    connectivity_smiles: str | None = None
    inchi: str | None = None


@dataclass(frozen=True)
class RetryAfter:
    raw: str | None
    status: str
    not_before_utc: datetime | None = None


@dataclass(frozen=True)
class LookupResult:
    """Strict adapter output consumed by the P4 handler."""

    provider: IdentityProvider
    adapter_version: str
    body: bytes
    request_metadata: dict[str, object]
    candidates: tuple[RawIdentityCandidate, ...] = ()
    error_code: IdentityErrorCode | None = None
    retryable: bool = False
    status_code: int | None = None
    retry_after: RetryAfter = RetryAfter(raw=None, status="missing")

    @property
    def succeeded(self) -> bool:
        return self.error_code is None


class PubChemPort(Protocol):
    """Provider port shared by fake and explicit live adapters."""

    adapter_version: str

    def resolve(self, query: MoleculeQuery) -> LookupResult:
        """Return raw candidates and lossless response bytes."""

    def lookup_candidates(self, request: IdentityLookupRequest) -> LookupResult:
        """Return identity attributes without requiring calculation q/M."""


def normalize_input(kind: MoleculeInputKind, value: str) -> str:
    """Apply only the input-kind rules that are safe before provider lookup."""

    import unicodedata

    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("molecule input must be non-blank")
    if len(value.strip()) > 4096:
        raise ValueError("molecule input exceeds 4096 characters")
    if kind is MoleculeInputKind.NAME:
        return unicodedata.normalize("NFC", value.strip())
    if kind is MoleculeInputKind.SMILES:
        return value.strip()
    if kind is MoleculeInputKind.CID:
        if not value.strip().isdigit() or int(value.strip()) <= 0:
            raise ValueError("CID must be a positive integer")
        return str(int(value.strip()))
    if kind is MoleculeInputKind.CAS:
        candidate = value.strip()
        parts = candidate.split("-")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise ValueError("CAS must use the NNNNNNN-NN-N format")
        if not 2 <= len(parts[0]) <= 7 or len(parts[1]) != 2 or len(parts[2]) != 1:
            raise ValueError("CAS must use the NNNNNNN-NN-N format")
        digits = "".join(parts)
        checksum = sum(int(digit) * weight for weight, digit in enumerate(reversed(digits[:-1]), 1))
        if checksum % 10 != int(digits[-1]):
            raise ValueError("CAS checksum is invalid")
        return candidate
    raise ValueError("unsupported molecule input kind")


__all__ = [
    "IdentityErrorCode",
    "IdentityLookupRequest",
    "LookupResult",
    "PubChemPort",
    "RawIdentityCandidate",
    "RetryAfter",
    "normalize_input",
]
