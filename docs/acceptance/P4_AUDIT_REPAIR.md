# P4 audit repair of dbb36f0

Status: LOCAL REPAIR VERIFIED; PR CI AND OWNER ACCEPTANCE PENDING. P5: NO-GO.

Baseline main: `dbb36f05ba721c88b56894b815bb7bf4e31e2191`, directly published;
baseline CI [34015177327](https://github.com/az87988799/BG6022-v2/actions/runs/34015177327)
was independently checked as successful for that exact SHA.
Repair branch: `codex/v2-p4-audit-repair`.
Reviewed implementation commit: `14607e5c9c4bfa6f286cda68021b308c78595ea0`.

## Changes and regression evidence

The existing Worker, transactions, Outbox and schema-3 state machine remain in
use. No database migration or frozen migration checksum changed.

| Audit | Repair | Permanent regression evidence |
| --- | --- | --- |
| R1 | Add all hydrogens once before counting electrons; normalizer v2 | N=10, CN=18, FC=18, ClC=26, water=10, ethanol=26, benzene=42; isotope and explicit/implicit H cases |
| R2 | Reject CXSMILES and annotations that the initial identity contract cannot preserve | OR, AND, labels and annotated SMILES rejected |
| R3 | Verify complete history then replay the receipt event prefix | Original prepare result remains exactly equal after resolution and confirmation |
| R4 | Shared phase-required source-chain reader binds query/registry/bundle/confirmation/plan to verified state and source events | Deleted candidate, confirmed identity, response envelope or attempt blocks inspect/export; locally rehashed plan rejected against state hash |
| R5 | Unique registry kind/ID/version; manifest reconstructed from entries; protocol/method/problem ID/hash and constraints checked | Conflicting r2SCAN-3c/B3LYP identity, forged manifest, unknown IDs, wrong hashes and proposal problem references rejected |
| R6 | Verify owner/query/effect/generation and actual envelope bytes before claim and handler replay | Corrupt 429 evidence blocks next claim with provider call count unchanged |
| R7 | Stream response with 2 MiB cumulative limit; check deadline after rate limiting and candidate processing; persist invalid Retry-After verbatim | Stops at fifth 512 KiB chunk; 25-second normalizer cannot succeed; empty/whitespace/overflow headers persist and back off |
| R8 | Normal optional entry-point imports in named adapters; explicit allowlist and dynamic import detection | Package/reducer/P3 boundaries and aliased dynamic-import regression |

Cases are in `tests/p4/test_audit_repair.py` and
`tests/test_p2_import_boundaries.py`. Existing P4 regressions remain enabled.

## Normalization compatibility

New normalization uses `rdkit-normalizer-v2`; its version participates in the
structure hash. Persisted v1 candidate/confirmation/plan records are never
rewritten or silently recomputed. Strict inspection of intact historical
records remains available. Confirming or exporting v1 candidate-based plans is
rejected with a typed integrity error requiring a fresh prepare/confirmation.
This boundary is tested. No database is rebuilt automatically.

The 20-second elapsed-time budget covers rate limiting, response reading and
candidate processing, with a check before accepting results. It does not
preempt an arbitrary blocking native RDKit call; an over-budget return is
discarded and recorded as a typed timeout.

## Source evidence

Real PubChem Water/Ethanol/Benzene responses were fetched on 2026-09-06 UTC.
All three were subsequently read through strict inspection and reached
`awaiting_identity`. No live identity was automatically confirmed.

- [Live workflow manifest](P4_AUDIT_LIVE_MANIFEST.json).
- [Lossless response evidence](P4_AUDIT_LIVE_EVIDENCE.json): request/query,
  candidates, typed attempts, response envelopes and base64 original body bytes.
- Evidence SHA-256 (canonical UTF-8 JSON, no newline):
  `53982df7c935975ddd52b3e278c8948ca9ff82bd028ac55984edb32debdc436f`.
- [Fresh Windows fake workflow manifest](P4_AUDIT_WINDOWS_MANIFEST.json).

Reproduction: `scripts/verify_p4.ps1` with a fresh state root, then
`scripts/collect_p4_source_evidence.py` exports already-verified live sources
without making a new network request. The original Windows manifest remains
historical; its documented digest is explicitly for CRLF bytes. The new lossless
evidence uses canonical bytes, so its digest survives checkout line conventions.

## Verification and release gates

- Audit regressions: 37 passed.
- Full offline regression: **413 passed**, branch coverage **81.25%** (80% gate).
  Command: `python -m uv run --offline --no-sync pytest --cov=orca_agent
  --cov-branch --cov-report=term --cov-fail-under=80 -q`.
  The run emitted 17 warnings (SQLite resource warnings and the expected
  offline socket-blocking warning); no test failed.
- Ruff, format, compileall, lock check and package build: checked locally.
- Repair PR: [#5](https://github.com/az87988799/BG6022-v2/pull/5).
  The PR checks are the live CI acceptance target; its reviewed implementation
  commit is recorded above. CI results will be attached to the PR description
  after completion, without creating self-referential verification commits.
- Actual repaired main SHA/CI: pending owner-approved merge.
- Owner acceptance: PENDING (single maintainer; no independent approval invented).
- ORCA/LLM/scientific execution: not run; action/job rows remain zero.

P4 complete acceptance and P5 readiness remain open until repaired-main CI and
explicit Owner acceptance are recorded. This document does not name its own
containing commit as an implementation review target.
