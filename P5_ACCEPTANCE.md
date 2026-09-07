# P5 Acceptance Record

Status: `PENDING_OWNER_ACCEPTANCE` — P6 remains blocked.

Current review baseline: `c7cd1eab7da646f885fbc6bf040f559d974ae6f2`.
Minimal C1–C3 changes: [completion repair](docs/P5_C_COMPLETION_REPAIR.md).
Earlier B1–B5 and T01–T30 mapping: [P5 audit repair](docs/P5_AUDIT_REPAIR.md).

## Verification

| Gate | Status | Evidence |
|---|---|---|
| Offline full suite | C1–C3 revalidation in progress | Prior c7cd1ea local run: 496 passed, 1 skipped, 80.97%; original CI collected 497 |
| Coverage policy | Unchanged 80% threshold | All current `src/orca_agent` modules; historical checkout not double-counted |
| Windows control | PASS in controlled tests | Non-destructive poll, PID identity, one-shot launch, long job, descendant cancel/timeout, supervisor crash, Job memory/output/workdir limits |
| Ruff / format / compileall | PASS | Latest source, tests and scripts |
| Locked dependencies / build | PASS | Locked sync/check; C1–C3 wheel and sdist built |
| R01 Water Opt→Freq→SP | FAIL / unresolved | Additional Opt parsed successfully; Freq rejected for Hessian coordinate-frame mismatch; SP not launched. No automatic retry |
| R02 process restart and command replay | NOT_COMPLETED | Chain did not complete; no successful replay gate inferred |
| R03 real cancellation | NOT_EXERCISED under C3 / recheck authorized | Old receipt lacks request-time control facts and is no longer counted PASS. One unused 30-second attempt authorized |
| R04 real timeout | NOT_EXERCISED; unused attempt authorized | First 1-second limit expired before spawn; zero physical starts. Unused attempt ceiling is 3 seconds |
| Prior PR / Windows–Ubuntu matrix | PASS at c7cd1ea | [CI 34080330125](https://github.com/az87988799/BG6022-v2/actions/runs/34080330125): Windows 497 passed; Ubuntu 3.11/3.14 495 passed, 2 skipped; Ubuntu branch coverage 80.19%; quality PASS |
| C1–C3 PR matrix | Pending | Must verify the new repair SHA separately |
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
- Separately authorized, unused final cancellation: `.tmp/p5-water-audit-final-cancel` (one job, 30 seconds).
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
