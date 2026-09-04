"""Versioned domain primitives for the BG6022 V2 project."""

from .canonical import canonical_json_bytes
from .errors import (
    CanonicalizationError,
    ContractInvariantError,
    DomainError,
    HashMismatchError,
    InvalidIdentifierError,
    OrcaAgentError,
    UnsupportedSchemaVersionError,
    contract_error_from_validation,
)
from .hashing import sha256_hex, verify_sha256
from .ids import (
    ActionId,
    ArtifactId,
    ClaimId,
    EvidenceId,
    PlanProposalId,
    PrimitiveId,
    ProblemSpecId,
    new_id,
)
from .versions import CURRENT_SCHEMA_VERSION, validate_schema_version

__all__ = [
    "ActionId",
    "ArtifactId",
    "CanonicalizationError",
    "ClaimId",
    "ContractInvariantError",
    "CURRENT_SCHEMA_VERSION",
    "DomainError",
    "EvidenceId",
    "HashMismatchError",
    "InvalidIdentifierError",
    "OrcaAgentError",
    "PlanProposalId",
    "PrimitiveId",
    "ProblemSpecId",
    "UnsupportedSchemaVersionError",
    "canonical_json_bytes",
    "contract_error_from_validation",
    "new_id",
    "sha256_hex",
    "validate_schema_version",
    "verify_sha256",
]
