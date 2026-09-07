# P5 Acceptance Record

Status: `PENDING_OWNER_ACCEPTANCE` — P6 remains blocked.

Current review baseline: `c7cd1eab7da646f885fbc6bf040f559d974ae6f2`.
Latest closeout review baseline: `f5991d07f6e25d192a65e3b6bdb705dc1dc6cf22`.
Limited follow-up repair: [closeout repair](docs/P5_CLOSEOUT_REPAIR.md).
Minimal C1–C3 changes: [completion repair](docs/P5_C_COMPLETION_REPAIR.md).
Earlier B1–B5 and T01–T30 mapping: [P5 audit repair](docs/P5_AUDIT_REPAIR.md).

## Verification

| Gate | Status | Evidence |
|---|---|---|
| Offline full suite | PASS at 97a8fc2 | 516 passed, 1 file-symlink privilege skip, 25 warnings; branch coverage 80.97% |
| Coverage policy | Unchanged 80% threshold | All current `src/orca_agent` modules; historical checkout not double-counted |
| Windows control | PASS in controlled tests | Non-destructive poll, PID identity, one-shot launch, long job, descendant cancel/timeout, supervisor crash, Job memory/output/workdir limits |
| Ruff / format / compileall | PASS | Latest source, tests and scripts |
| Locked dependencies / build | PASS | Locked sync/check; C1–C3 wheel and sdist built |
| Frozen real Freq replay | PASS at e634305 | Parser v4 allows translation only; 9x9 Hessian and 9 raw modes; [separate offline evidence](docs/evidence/p5-closeout/frozen-freq-replay.json). Historical run not rewritten |
| Unconfirmed Windows tree | PASS in targeted regression | Nonempty/query-error Job for cancel/timeout remains pending through runner, backend and service; no downstream result or physical relaunch |
| R01 Water Opt→Freq→SP | IN_PROGRESS / Opt PASS only | New chain Opt completed once and parsed completely; Freq/SP remain unapproved and unstarted. [Opt evidence](docs/evidence/p5-closeout/r01-opt/README.md), [chain status](docs/evidence/p5-closeout/README.md); no automatic retry |
| R02 process restart and command replay | PARTIAL | Approval/reconcile replay returned original events, physical Opt start remained 1 and no downstream job was created; full chain replay remains incomplete |
| R03 real cancellation | PASS at 97a8fc2 | Authorized one-shot 30-second case; identity/liveness/request/stop/tree-empty facts and [raw evidence](docs/evidence/p5-c-controls/README.md). Old receipt is not reused as PASS |
| R04 real timeout | PASS at 97a8fc2 | Authorized one-shot 3-second case physically started then stopped at deadline; [raw evidence](docs/evidence/p5-c-controls/README.md). Old zero-start attempt remains NOT_EXERCISED |
| Prior PR / Windows–Ubuntu matrix | PASS at c7cd1ea | [CI 34080330125](https://github.com/az87988799/BG6022-v2/actions/runs/34080330125): Windows 497 passed; Ubuntu 3.11/3.14 495 passed, 2 skipped; Ubuntu branch coverage 80.19%; quality PASS |
| C1–C3 PR matrix | PASS at 97a8fc2 | [CI 34086229322](https://github.com/az87988799/BG6022-v2/actions/runs/34086229322): Windows 517 passed; Ubuntu 3.11/3.14 515 passed, 2 Windows-only skips; Ubuntu branch coverage 80.16%; quality PASS |
| Closeout PR matrix | PASS at e634305 | [CI 34100584094](https://github.com/az87988799/BG6022-v2/actions/runs/34100584094): Windows 527 passed; Ubuntu 3.11/3.14 525 passed, 2 Windows-only skips; Ubuntu branch coverage 80.31%; quality PASS. No main CI inferred |
| Main merge / main CI | NOT_RUN | No automatic merge or P6 start |
| Owner acceptance | PENDING | Only the owner can accept this phase |

## Real execution authorization and provenance

ORCA executable: `E:\orca\orca.exe`, exact version `6.1.1`.
SHA-256: `8d6b51bf4093c967dbed997cc651f0212b8f94313ee77ea56f548f000672c42f`.

First authorization: Water, 1 core / 2048 MB, Opt/Freq/SP ceilings
900/1800/300 seconds, one cancellation within 30 seconds and one 1-second
timeout attempt, at most five tasks, no automatic rerun.
Actual first-round reservations: three; physical ORCA starts: two.

Additional explicit owner authorization: at most four new Water tasks on the
same executable/resources: Opt/Freq/SP 900/1800/300 seconds and one 3-second
timeout attempt, no automatic rerun. Separate state roots preserve all earlier
outcomes:

- First: `.tmp/p5-water-audit`.
- Additional: `.tmp/p5-water-audit-retry`.
- Separately authorized final cancellation, executed once and PASS at 97a8fc2: `.tmp/p5-water-audit-final-cancel` (one job, 30 seconds).
- Byte-preserving real Opt fixture: `tests/p5/fixtures/orca_6_1_1_water_opt`.

Synthetic fixtures and controlled Python processes are **not** real ORCA
acceptance evidence. P5 exports retain `scientific_assessment=not_evaluated`
and `claim_status=not_generated`.

The reviewer's isolated Linux/Python 3.12 run had four process-identity failures;
these were not GitHub CI failures. Missing/denied/disappearing `/proc` identity
remains fail-closed; no PID-only fallback or broader Linux production claim.

## Owner checklist

- [ ] Review repaired implementation, T01–T30 map and platform CI.
- [ ] Review R01–R04 original evidence and exact executable/budget binding.
- [ ] Confirm all required real gates passed; NOT_EXERCISED is not a pass.
- [ ] Authorize acceptance; until then retain pending status.
- [ ] Review main CI after an explicitly authorized merge.

No scientific-PASS, minimum-energy claim, P6 readiness or owner acceptance is
inferred from implementation/test completion.

Repair source commits: `8d89f582b79add42d8b0236892a95f2a7290ccde` and
`97a8fc2197d8027559f4fb58b66463aded5a91cb`. Subsequent documentation/evidence
commits do not alter that tested execution source. PR remains [#6, Draft](https://github.com/az87988799/BG6022-v2/pull/6).
