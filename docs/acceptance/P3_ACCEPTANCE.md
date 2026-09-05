# V2-P3 Acceptance

- Status: LOCAL_REPAIR_VERIFIED — PR CI, repair merge, main CI and Owner acceptance pending.
- Scope: offline Water `water_sp_v1` FakeBackend only.
- Original implementation: `05825373b3c279322a9a0539484d2e247d83cad0`.
- Original publication: already present on main at `e13e8db0f1644a7267f7b29e0c408cc0bf8e002b`.
- Original main CI: [33966056353](https://github.com/az87988799/BG6022-v2/actions/runs/33966056353), FAILURE; the former blanket technical-pass statement is withdrawn.
- Repair branch: `codex/v2-p3-minimal-fix`.
- Reviewed repair implementation: `6bdfd90e0cbe556791ae912d1b3090d90ed1c48c`.
- Repair PR / CI: pending.
- Repair merge SHA / post-merge main CI: pending.
- Owner acceptance of repaired P3: pending.

The previous implementation SHA was an invalid concatenation. The original
implementation and the repair publication are recorded separately above;
this document does not refer to its own HEAD as the reviewed implementation.

## Repair evidence

M1–M4 are implemented using the existing Worker, outbox, event replay,
command receipts, UoW and ArtifactStore. No table, column or migration was
added. Migrations v1–v6, their checksums, historical policies, engine/schema
identities and the existing CI matrix are unchanged.

| Gate | Regression evidence |
| --- | --- |
| T01 portable paths | Seven invalid POSIX/Windows path forms plus existing artifact put/read tests |
| T02–T04 permit fencing | Reclaimed generation blocked before backend; reclaim during backend blocks old result write; diagnosed retry recovers through sanitized Handler permit |
| T05–T08 recovery | Unknown submission blocks cancel; both concurrent cancel/submit transaction winners; expired grant permits recovery of original execution, but not first submission; dead-letter preserves unknown execution and exposes reconciliation diagnostic |
| T09–T10 report integrity | Damage before render and raw/MD/JSON damage before completion; no completed revision; restored bytes allow report-only retry with one execution |
| T11–T12 CLI | Real socket-disabled child processes reject marker-only, other-run, missing and unknown-format files; exact MD/JSON exports pass; missing --run creates no state root |
| T13–T14 commands | Expiry at the boundary is durable and replayable; changed bindings rejected; completed Start/Approve replay returns the original full public result in new CLI processes; Cancel full-result replay |
| T15 rollback | Exceptions after approval/expiry workflow-record write and after completion receipt write roll back all transactional writes; prior atomicity regressions retained |

The ordinary pytest suite includes these cases in
`tests/p3/test_minimal_repair.py`. Concurrency tests use separate connections,
threads and explicit synchronization events. CLI child processes install their
own socket blocker; they do not rely on inheritance of pytest's socket plugin.

## Local verification

Environment: Windows 11 (10.0.26200), Python 3.14.6, uv 0.12.9.

- Locked sync and lock check: PASS.
- Compileall, Ruff, format check: PASS.
- Full offline suite with branch coverage: **315 passed**, one expected socket-block warning; **80.35%**, threshold remains 80%.
- Wheel/sdist build and packaged Water fixture smoke: PASS.
- Working, staged and confirmed-baseline whitespace checks: PASS.
- Updated `scripts/verify_p3.ps1 -StateRoot <fresh temporary root> -AutoApprove`: PASS; execution count 1, stage count 3, report verified, stale approval exit 2, conversation isolation true.
- ORCA, LLM, PubChem, RDKit and external business network calls: NOT RUN.

`-AutoApprove` only approves the temporary fake workflow, not this release.

## Remaining acceptance and limitations

Single-maintainer Owner acceptance is permitted. Independent GitHub Approval
is waived; no synthetic Review or Approval is claimed. The repair must still
have green PR CI, merge, green CI for the actual main merge SHA, and explicit
Owner acceptance before final P3 PASS and P4 entry.

An unknown execution retains SUBMITTING; verified submitted facts are also
preserved on workflow failure. The derived
`execution_reconciliation_required` diagnostic does not automatically resume
dead-letter work. Recovery promises one persistent execution with the original
ID/key, not one invocation of the backend method. No terminal reopening or
automatic recovery scheduler was added.
