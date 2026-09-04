# ADR-0002: Use strict, versioned domain contracts

- Status: Accepted
- Date: 2026-09-04
- Scope: V2-P1 domain boundary

## Context

Later V2 layers will exchange problem specifications, plans, actions, evidence,
and claims across process and persistence boundaries. These records need stable
identity, deterministic serialization, explicit schema evolution, and safe
failure behavior before a durable kernel or execution backend is introduced.

## Decision

P1 defines six minimal Pydantic contracts: `ProblemSpec`, `PrimitiveSpec`,
`PlanProposal`, `ValidatedAction`, `EvidenceRecord`, and `ValidatedClaim`.
Contracts use a shared strict configuration (`extra=forbid`, `frozen=True`,
`strict=True`, and `validate_default=True`) and carry integer
`schema_version=1`. Unknown schema versions are rejected; future upgrades must
be explicit functions rather than best-effort parsing.

Entity IDs are prefixed UUID4 hex strings. Canonical JSON v1 uses sorted object
keys, compact separators, UTF-8, unescaped Unicode, finite numbers only, and
explicitly normalized Pydantic JSON output. SHA-256 hashes are lowercase
64-character hex strings over those canonical bytes.

Proposal/configuration JSON is checked for shell, command, executable, path,
raw ORCA, and secret-shaped keys. P1 contracts carry only logical IDs and typed
resource ceilings; they do not contain subprocess, local path, raw ORCA, or
network behavior.

## Consequences

- Cross-layer records can be compared and hashed deterministically.
- Unknown fields and implicit scalar conversions fail early.
- Contract errors can cross an application boundary as stable typed errors.
- Scientific validation, capability validation, persistence, and execution
  remain outside P1 and must be implemented in later work packages.
- Python 3.11 remains supported while avoiding Pydantic's implicit recursive
  type-alias generation path for JSON containers.
