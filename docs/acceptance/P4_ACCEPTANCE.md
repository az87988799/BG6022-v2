# V2-P4 Acceptance Record

- Status: AUDIT REPAIR IMPLEMENTED; OWNER ACCEPTANCE PENDING
- Latest repair evidence: [P4_AUDIT_REPAIR.md](P4_AUDIT_REPAIR.md).
- Scope: schema-3 molecule identity confirmation and planning-only workflow.
- Base `main`: `4f6ee0e8b4d1e604278276ec777f752e0e32a41f`.
- Reviewed implementation commit: `f5451f828fa792cc11f7476138d6ae3eb6b4d03e`.
- Working branch: `codex/v2-p4-identity-protocol`.
- Original P4 publication: directly published to `main` at
  `dbb36f05ba721c88b56894b815bb7bf4e31e2191`; no synthetic merge commit is claimed.
- Published `main` CI: [34015177327](https://github.com/az87988799/BG6022-v2/actions/runs/34015177327),
  all four jobs successful for that SHA. This is the pre-repair baseline.
- Repair publication and CI: see the latest repair evidence above.
- Owner acceptance: PENDING.

## Original implementation environment and fixed registries

- Platform: Windows-11-10.0.26200-SP0.
- Python: `3.14.6`.
- uv: `0.12.9`.
- RDKit: `2026.3.5`.
- HTTPX: `0.28.1`.
- Schema / engine / dispatch policy: `3` / `p4-identity-plan-v1` / `4`.
- Registry version: `registry-v1`.
- Fixed registry snapshot hash for record ID
  `workflow_00000000000000000000000000000000`:
  `be9dc82f0bf167af06060f0c018fccab790b8e2070f19c57dfce6073dcd56d26`.
- Protocol version: `ground-state-baseline-r2scan3c-v1`.
- Protocol entry hash:
  `1b903e8d5657d5659ba60eb6298913214e1bab558cfe4fb640cad119cebc93d3`.
- Normalizer version: `rdkit-normalizer-v1`.
- Normalizer source SHA-256:
  `8deaec4e6c4a236203f05ae4480b502eeecd1d3d265366a732c196cd1c6c0f23`.
- Water (`O`) normalized structure hash:
  `53b551cf4e42a73c089ffaeec1fe32e0ac2f34ca145110233d67401e1bcc9760`.

## Implemented boundary

P4 accepts an explicit name, CAS, CID, or SMILES input; normalizes candidate
structures through the local RDKit adapter; persists owner-bound response
evidence; pauses for explicit identity confirmation; and creates a frozen,
registry-bound Opt-to-Freq `PreparedPlan`.

The P4 result is planning-only:

- `identity_confirmed=true` and `planning_valid=true` only after confirmation;
- `execution_ready=false`, `execution_approved=false`, and
  `real_scientific_result=false`;
- missing execution prerequisites remain `initial_3d_geometry`,
  `implemented_orca_backend`, and `validated_action_and_execution_approval`;
- no action, approval grant, execution intent, job, ORCA input, backend call,
  LLM call, PubChem call by default, or scientific result is created;
- the P1/P2/P3 migrations and their checksums are unchanged.

## T01–T24 evidence map

| Test | Evidence |
| --- | --- |
| T01 | `test_names_require_identity_confirmation_and_create_only_a_plan`; `scripts/verify_p4.ps1` fake Water/Ethanol/Benzene cases |
| T02 | `test_input_kinds_route_without_guessing`, invalid CAS/CID, SMILES local-only, URL encoding, and `normalize_input` tests |
| T03 | single-candidate confirmation, multiple candidates, and over-limit non-truncation tests |
| T04 | equivalent SMILES structure hash test |
| T05 | enantiomer, E/Z, unspecified stereo, and isotope assertions |
| T06 | charge/multiplicity/fragment and protocol-range tests |
| T07 | response envelope hash, raw body hash, source-artifact binding, and fake provider tests |
| T08 | owner-bound identical-response two-run test |
| T09 | HTTP 429/503/502/504/400/404/500 typed status test; retry worker test |
| T10 | Retry-After missing/valid/date/invalid parsing and readiness/rate-limit tests |
| T11 | persisted retry deadline and post-terminal-write crash/restart recovery test |
| T12 | corrupted response artifact blocks inspect/export test |
| T13 | malformed/empty/over-limit/CID-mismatch provider and typed handler-failure tests |
| T14 | shared outbox permit and generation fencing regression coverage plus P4 completion recovery |
| T15 | cross-boundary run/conversation/revision/interrupt/query/set/candidate binding test |
| T16 | durable confirmation expiry, reject replay, cancel replay, and duplicate-payload rejection |
| T17 | shared atomic failure and rollback suites; P4 completion crash regression |
| T18 | registry and plan binding corruption tests |
| T19 | restart inspection verifies history and does not call the provider |
| T20 | planning-only flags and zero `actions/jobs` rows in workflow and script tests |
| T21 | retained P3 suite rejects non-water fixture inputs; no P4 route to P3 execution |
| T22 | CLI independent-process script plus P3/P4 version routing and import-boundary suites |
| T23 | locked P4 extra sync, fixture-inclusive `uv build`, and package metadata checks |
| T24 | default offline socket-blocking suite; fake/live separation; live smoke recorded below |

## Original implementation offline technical evidence

- Locked dependency sync: PASS (`python -m uv sync --locked --extra p4`).
- Lockfile consistency: PASS (`python -m uv lock --check`).
- Compileall: PASS (`python -m uv run --offline --no-sync python -m compileall -q src tests`).
- Ruff check: PASS.
- Ruff format check: PASS.
- Full test suite: `375 passed`.
- Branch coverage: `81.27%` with `--cov-fail-under=80`.
- Package build: PASS (`python -m uv build`).
- Git whitespace check: PASS (`git diff --check` and staged diff check).
- P4 focused tests: `60 passed` in the final local run.
- Windows independent-process manifest: [P4_WINDOWS_MANIFEST.json](P4_WINDOWS_MANIFEST.json).
- Manifest SHA-256:
  `1a082e0d8794eff76d75b864bcf57dbcadedbc05d1c11fadd46c3169d19be11c`.
  This digest describes the original Windows CRLF bytes. Git checkouts may use
  LF; it is not a cross-platform byte digest. New audit evidence records its
  byte convention separately.

The manifest uses the fake provider only. It records three successful
`plan_ready` flows, confirmation replay, wrong candidate binding rejection,
cancellation, and zero `actions/jobs` rows. The fake provider is not evidence
of a live PubChem source.

## External and execution gates

- Default business network access: OFF by default and covered by offline tests.
- PubChem live source smoke: three real responses captured in the audit repair;
  see [P4_AUDIT_LIVE_EVIDENCE.json](P4_AUDIT_LIVE_EVIDENCE.json).
- ORCA 6.1 compile/execute: NOT RUN (P5 scope).
- LLM, scientific execution, action, grant, intent, and job: NOT RUN / NOT
  CREATED.
- Repair PR and CI: tracked in the latest repair record. The baseline main CI
  above does not certify the repair.
- Owner acceptance: PENDING.

P4 complete acceptance is intentionally not closed by this local result alone.
It still requires a repaired implementation with successful `main` CI and
explicit single-maintainer Owner acceptance. Live source evidence does not
constitute identity confirmation or scientific execution approval.
