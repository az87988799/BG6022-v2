# V2-P2 Acceptance

- Status: PASS
- Date: 2026-09-04
- Branch: `main`
- Reviewed implementation commit: `8433872d8b5a6ec59446f8efb2595ed3ecfb60fa`
- Acceptance PR: `#2`
- Latest verified workflow: `33873115361`
- Python local: `3.14.6`
- SQLite local: `3.53.2`
- ORCA installed: `6.1`
- ORCA executed in P2: No
- LLM/PubChem/RDKit called in P2: No

## Local gates

| Gate | Result | Evidence |
|---|---|---|
| uv lock | PASS | `uv lock --check` |
| compileall | PASS | `uv run --offline --no-sync python -m compileall -q src tests` |
| pytest | PASS | `104 passed, 1 expected socket-blocking warning` |
| ruff check/format | PASS | `ruff check .`; `67 files already formatted` |
| package build | PASS | `uv build` produced sdist and wheel |
| migration fresh/idempotent/drift/rollback | PASS | `tests/persistence/test_migrations.py` |
| reducer purity/replay | PASS | `tests/unit/orchestration/test_reducer.py`; `tests/persistence/test_restart_replay.py` |
| CAS concurrency | PASS | `tests/persistence/test_revision_cas.py` |
| command idempotency | PASS | `tests/persistence/test_run_event_atomicity.py`; `tests/persistence/test_effect_commands.py` |
| interrupt lifecycle | PASS | `tests/persistence/test_interrupt_lifecycle.py` |
| outbox lease/retry/dead-letter | PASS | `tests/persistence/test_outbox_leases.py` |
| crash/restart | PASS | `tests/persistence/test_atomic_failure_points.py`; `tests/persistence/test_restart_replay.py` |
| no tracked SQLite artifacts | PASS | `git ls-files "*.sqlite" "*.sqlite3" "*.db" "*-wal" "*-shm"` returned no paths |

The local suite reports one expected warning because the offline socket test
deliberately attempts to construct a socket.

## CI gates

| OS | Python | Result | Workflow URL |
|---|---:|---|---|
| Ubuntu | 3.11 | PASS | [run 33872788422](https://github.com/az87988799/BG6022-v2/actions/runs/33872788422) |
| Ubuntu | 3.14 | PASS | [run 33872788422](https://github.com/az87988799/BG6022-v2/actions/runs/33872788422) |
| Windows | 3.14 | PASS | [run 33872788422](https://github.com/az87988799/BG6022-v2/actions/runs/33872788422) |
| Quality | 3.14 | PASS | [run 33872788422](https://github.com/az87988799/BG6022-v2/actions/runs/33872788422) |

## Durable-kernel invariants

- One accepted event increments revision exactly once.
- Duplicate command IDs do not duplicate events or effects.
- State/event/outbox/interrupt projection commit atomically.
- Every run has at most one pending interrupt.
- Expired interrupt becomes durable state and typed result.
- Event replay matches the stored snapshot hash.
- Outbox delivery is documented as at-least-once.
- Leased effects recover after expiry.

## Scope confirmation

- No FakeBackend or ExecutionGateway.
- No ORCA compiler/parser/subprocess.
- No RDKit, PubChem, LLM, evidence, claim, report, API, or Slurm work.
- No legacy active state migration.
- P3 has not started.

## Decision

The P2 implementation, local gates, PR #2, and the resulting `main` GitHub
Actions matrix pass. V2-P2 is formally `PASS` on `main`. P3 has not started
and may not start under this work package.
