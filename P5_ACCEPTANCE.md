# P5 Acceptance Record

Status: `PENDING_OWNER_ACCEPTANCE` — P6 remains blocked.

Baseline under repair: `3be5bb271c103807bb0c1d93fcbf0c95f3291ca5`.
Repair details and the T01–T30 evidence map: [P5 audit repair](docs/P5_AUDIT_REPAIR.md).

## Verification

| Gate | Status | Evidence |
|---|---|---|
| Offline full suite | Revalidation in progress | Previous clean run: 483 passed, branch coverage 80.83%; latest P5 targeted suites: 77 passed, 1 Windows symlink-privilege skip |
| Coverage policy | Unchanged 80% threshold | All current `src/orca_agent` modules; historical checkout not double-counted |
| Windows control | PASS in controlled tests | Non-destructive poll, PID identity, one-shot launch, long job, descendant cancel/timeout, supervisor crash, Job memory/output/workdir limits |
| Ruff / format / compileall | PASS | Latest source, tests and scripts |
| Locked dependencies / build | PASS; final rebuild pending | `uv sync --locked --extra p5`, `uv lock --check`, wheel and sdist |
| R01 Water Opt→Freq→SP | Pending authorized additional attempt | First Opt exit 0; application rejected CRLF. Original failed record retained; genuine fixture replay now passes |
| R02 process restart and command replay | Pending | Embedded in R01 without extra numerical jobs |
| R03 real cancellation | PASS | First authorization, `execution_db3395b82c1542e7bac04d2a6a18a933`; real child stopped and partial output retained |
| R04 real timeout | NOT_EXERCISED; additional attempt authorized | First 1-second limit expired before spawn; zero physical starts. New attempt ceiling is 3 seconds |
| PR / Windows–Ubuntu matrix | Pending | Will bind results to pushed repair SHA |
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
- Byte-preserving real Opt fixture: `tests/p5/fixtures/orca_6_1_1_water_opt`.

Synthetic fixtures and controlled Python processes are **not** real ORCA
acceptance evidence. P5 exports retain `scientific_assessment=not_evaluated`
and `claim_status=not_generated`.

## Owner checklist

- [ ] Review repaired implementation, T01–T30 map and platform CI.
- [ ] Review R01–R04 original evidence and exact executable/budget binding.
- [ ] Confirm all required real gates passed; NOT_EXERCISED is not a pass.
- [ ] Authorize acceptance; until then retain pending status.
- [ ] Review main CI after an explicitly authorized merge.

No scientific-PASS, minimum-energy claim, P6 readiness or owner acceptance is
inferred from implementation/test completion.
