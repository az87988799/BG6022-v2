# V2-P2 Acceptance

- Status: PASS — single-maintainer Owner acceptance; independent GitHub review waived
- Date: 2026-09-05
- Branch: `codex/v2-p2-hardening`
- Implementation submitted for review: `e12de3e10d4b33df098899e3474ac1fef80138f2`
- Acceptance PR: [#3](https://github.com/az87988799/BG6022-v2/pull/3)
- Verified implementation workflow: [33937447424](https://github.com/az87988799/BG6022-v2/actions/runs/33937447424)
- Independent GitHub approval: Waived by repository owner `az87988799`; GitHub does not permit a PR author to approve their own PR
- Main merge: `5d899d4a03bc4600115f4a47c26f8544b9324bcd`
- Main post-merge CI: [33952574495](https://github.com/az87988799/BG6022-v2/actions/runs/33952574495), all four jobs passed
- User acceptance: PASS, single-maintainer owner instruction received 2026-09-05
- Local environment: Python 3.14.6, SQLite 3.53.2; locked uv environment
- Real ORCA/LLM/PubChem/RDKit execution: None
- P3: Not started

The implementation commit and CI above are fixed evidence, not a self-reference
to this document's HEAD. The owner acceptance is a project governance decision,
not a synthetic GitHub review. Earlier 214-test/80.28% evidence is superseded.
See [minimal-repair details](P2_MINIMAL_REPAIR.md) for the approved v4 checksum
change, upgrade procedure, and separately committed repairs.

## Local verification

All commands ran successfully in E:\BG6022-v2. uv is installed but not on PATH,
so the commands used `python -m uv` in place of `uv`.

| Gate | Result |
|---|---|
| git fetch origin | PASS |
| uv sync --locked | PASS |
| uv lock --check | PASS |
| uv run --offline --no-sync python -m compileall -q src tests | PASS |
| uv run --offline --no-sync pytest --cov=orca_agent --cov-branch --cov-report=term-missing --cov-fail-under=80 | PASS: 253 tests, 80.71% |
| uv run --offline --no-sync ruff check . | PASS |
| uv run --offline --no-sync ruff format --check . | PASS |
| uv build | PASS: sdist and wheel |
| git diff --check origin/main...HEAD | PASS |
| git diff --check; git diff --cached --check | PASS |
| tracked SQLite/artifact scan | No tracked database files |

The full locked suite includes pytest-socket. Its intentional socket-blocking
test emits one expected warning; no business-network calls occur.

## Minimal repair regressions

| Requirement | Evidence |
|---|---|
| Original published-main historical regression | Original 8/8 PASS, including both previously failing empty summaries and migration rollback |
| Historical error text stays internal | Additional old-main retry fixture verifies it never enters the handler view |
| Immutable historical evidence | Original event IDs, JSON and payload/result hashes retained; strict integer and hash corruption rejected |
| v4 interpolation/checksum | Both literal placeholder strings absent from executed SQL; v1–v5 checksums frozen |
| Queue pagination | 140 candidates, waiting/unknown/in-flight sibling cases; later healthy run executes; all-blocked returns |
| Fixed policy and worker races | Versions bound to immutable rules/digests; common service factory; real two-worker contention; old permit rejected; committed retry preserved; busy completion never reinvokes handler |
| Command ID namespace | New external UUID5 rejected before writing; historical identical retry remains valid; conflicts reject; upgrade/claim reports observed or imminent-generation collisions |
| Terminal metadata | Direct SQL changes to four columns rejected in all three terminal states; v5 collision failure restores the pre-migration database |
| Prior atomic/crash gates | Existing tests retained: seven crash points, actual write-before-commit rollback, CAS/claim competition, replay and projection corruption |

Historical schema 1 is read under its published contract. New commands and
completion writes retain current closed enums and bounded typed receipts.
Completion protocol 4 alone is not a payload schema discriminator.

## Implementation CI

All four jobs passed in workflow 33937447424 for the fixed implementation commit:

- Ubuntu Python 3.11
- Ubuntu Python 3.14, including branch coverage gate
- Windows Python 3.14
- Quality

## Acceptance closure

The single-maintainer owner explicitly waived the independent GitHub review gate;
GitHub's author-self-approval restriction remains recorded and no review was
fabricated. PR #3 is merged, post-merge main CI is green, and owner acceptance is
recorded above. P2 is accepted for this repository and P3 may begin from the
merged main commit.

No production or legacy database was changed, deleted or rebuilt. Old-v4 P2
development databases must be backed up before recreation; no manual edits to
schema_migrations or hidden trigger workarounds are permitted.

P2 promises at-least-once delivery and database completion fencing, not
external exactly-once execution or prevention of every physical effect after
cancellation. Real Gateway idempotency, long-running lease and cancellation
coordination remain outside this patch and are required before real ORCA use.
