# P5 audit repair — pending owner acceptance

Baseline: `3be5bb271c103807bb0c1d93fcbf0c95f3291ca5`.
Scope: P5 audit fixes only; no P6 scientific assessment, claims, or main merge.

## Changes

- B1: Windows liveness uses query handles and process creation identity, never
  `os.kill(pid, 0)`. Wrong identity fails closed. Windows starts the child
  suspended, assigns the Job Object and aggregate memory budget, then resumes.
- B2: approvals create durable launch effects. The shared `OutboxWorker`,
  `ValidatedAction` ledger and generation-fenced permit authorize a single
  runner-side atomic ticket consumption. Consumed tickets cannot respawn.
  Crash-after-observation recovery acknowledges only the existing reservation.
- Observations and cancellation notifications use shared short control effects;
  collection follows their persisted acknowledgement and remains idempotent.
  Durable cancellation is also polled by the runner, independent of controller
  lifetime. Explicit CLI reconcile remains a recovery command without launch authority.
- B3/B4: fixed `input.inp` → `input.xyz` / `input.hess` contract. Archive actual
  optimized XYZ bytes and compare against the unique post-convergence final
  coordinate section. A genuine four-cycle ORCA 6.1.1 CRLF fixture supplements
  clearly labelled synthetic cases.
- B5: validate complete finite 3N×3N block Hessian, symmetry, atoms, default
  masses, Bohr/Angstrom geometry agreement and complete indexed raw 3N modes.
  Retain negative modes; no minimum or scientific-PASS inference.
- Exact executable hash and probed full version are frozen before approval;
  output version must match. Fresh CLI cancellation/reconciliation restores
  the verified runtime instead of silently selecting the fake adapter.
- Work paths reject traversal, alternate streams, symlinks and Windows reparse
  points before resolution. Cross-platform controlled children retain the
  production `.exe` allowlist.

## Coverage scope

Use `pytest --cov=src/orca_agent --cov-branch --cov-fail-under=80`.
The directory covers **all current package modules**, with no module omissions
and no lowered threshold. Package-name discovery also measured a different
historical checkout launched by migration compatibility tests, duplicating old
source in the denominator. Historical compatibility tests still run, but their
old implementation is not the current implementation's coverage target.
Coverage subprocess instrumentation includes current CLI/runner subprocesses.

## T01–T30 evidence map

Paths below are repository-relative test references. This is a concrete mapping,
not a claim that every adversarial combination in the plan is exhausted.

| Gate | Tests / evidence | Qualification |
|---|---|---|
| T01 | p5 boundaries missing-source tests; p4 audit-repair source-chain tests | Source/ownership integrity checks |
| T02–T03 | persistence migration tests and published-main upgrade regressions | Historical checksums retained; no migration rewritten |
| T04 | registry/planning tests; p5 compiler rejection test | Frozen registry contracts |
| T05 | p5 workflow `all_p5_protocols`, `freq_from_opt` | All five protocols |
| T06 | p5 boundaries `geometry_preparation_preserves_confirmed_identity` | Water, ethanol, benzene, stereo/E-Z; not a universal chemistry validation |
| T07 | p5 boundaries `replaying_prepare_does_not_regenerate_geometry` | Frozen geometry replay |
| T08–T09 | p5 workflow compiler/parser rejection; output-contract tests | Closed compiler and fixed names |
| T10 | p5 approval hash/owner/revision tests; runner invalid-authority cases | Exact approval and launch ledger binding |
| T11 | runtime real switch and origin rejection; fake-vs-local receipt regression | No fake receipt promoted to local |
| T12 | p5 workflow approval/reconcile replay; boundaries prepare/cancel replay; real harness | Original command receipts retained |
| T13 | p5 owned-artifact test; infrastructure artifact tests | Same bytes have independent owner references |
| T14 | p5 work-path tests | Windows junctions and traversal; file symlink case skips without OS privilege |
| T15–T16 | runner concurrent/replayed/expired-permit tests | Physical start at most once; invalid permit zero-start |
| T17 | runner crash test; boundaries crash-after-observation test | Selected critical crash windows; no claim of exhaustive fault injection |
| T18 | workflow reconcile replay; runner receipt replay | No second job on replay |
| T19 | runner cancellation, pre-consume cancellation, terminal immutability | Cancellation request is not itself a stopped fact |
| T20 | synthetic output-contract suite; genuine CRLF multicycle fixture | Final artifact, not initial coordinates |
| T21 | complete-block Hessian and corruption parameterization | Raw modes; linear/nonlinear scientific interpretation deferred |
| T22 | workflow exact optimized-XYZ downstream binding | Freq/SP reuse actual byte hash |
| T23 | fake/local receipt separation and result/export assertions | No scientific PASS or minimum claim |
| T24 | lock/build gates; CLI subprocess tests | Wheel/sdist built; extras via locked sync |
| T25 | runner `long_lived_job_outlasts_dispatch_lease_without_relaunch` | Real controlled process exceeds 30-second lease |
| T26 | runner `stop_terminates_descendant_tree_with_fresh_service` | Cancel and timeout; parent and descendant stopped |
| T27 | runner `supervisor_crash_kills_descendants_without_relaunch` | Windows Job Object crash cleanup |
| T28 | continuous polling and changed creation-identity test | Query failure returns unknown, never a destructive probe |
| T29 | output/workdir budget and aggregate Job memory tests | Enforcement recorded in receipt |
| T30 | independent CLI workflow; fresh service control tests; real gate replay | No in-memory runtime dependency |

## Real acceptance history

First authorized preview: `.tmp/p5-water-audit/water-gate-preview.json`.
ORCA 6.1.1 executable SHA-256:
`8d6b51bf4093c967dbed997cc651f0212b8f94313ee77ea56f548f000672c42f`.

- R01/R02: original Opt completed normally but application rejected CRLF output;
  archived failed record remains unchanged. Fixed parser passes byte-preserving replay.
- R03: real cancellation **PASS**, process stopped, partial output retained.
- R04: **NOT_EXERCISED**; one-second deadline elapsed before spawn (zero physical
  starts). This is not counted as real timeout success.
- Owner subsequently authorized at most four new jobs: Opt/Freq/SP plus one
  three-second timeout attempt, same executable/Water/resources, no automatic retry.
  Independent preview: `.tmp/p5-water-audit-retry/water-gate-preview.json`.

Final results and release links are recorded in `P5_ACCEPTANCE.md`. Neither
offline PASS nor real execution PASS substitutes for owner acceptance.
