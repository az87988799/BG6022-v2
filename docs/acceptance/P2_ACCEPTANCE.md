# V2-P2 Acceptance

- Status: CONDITIONAL PASS
- Date: 2026-09-04
- Branch: `codex/v2-p2-hardening`
- Reviewed implementation commit: `0da26ce0617004cc9d95c15c84e29a31b22ef089`
- Acceptance PR: `#3`
- Latest verified workflow: `33876302395`
- P2.8 hardening status: implementation and CI complete; independent review pending
- Python local: `3.14.6`
- SQLite local: `3.53.2`
- ORCA installed: `6.1`
- ORCA executed in P2: No
- LLM/PubChem/RDKit called in P2: No

The implementation commit and workflow above are fixed review evidence. This
document intentionally does not refer to the commit that updates itself.

## Local gates

| Gate | Result | Evidence |
|---|---|---|
| uv lock | PASS | `uv lock --check` |
| compileall | PASS | `uv run --offline --no-sync python -m compileall -q src tests` |
| pytest | PASS | `123 passed, 1 expected socket-blocking warning` |
| ruff check/format | PASS | `ruff check .`; `67 files already formatted` |
| package build | PASS | `uv build` produced sdist and wheel |
| migration fresh/idempotent/drift/rollback | PASS | `tests/persistence/test_migrations.py` |
| reducer purity/replay | PASS | `tests/unit/orchestration/test_reducer.py`; `tests/persistence/test_restart_replay.py` |
| CAS concurrency | PASS | `tests/persistence/test_revision_cas.py` |
| command idempotency | PASS | `tests/persistence/test_run_event_atomicity.py`; `tests/persistence/test_effect_commands.py` |
| interrupt lifecycle | PASS | `tests/persistence/test_interrupt_lifecycle.py` |
| outbox lease/retry/dead-letter | PASS | `tests/persistence/test_outbox_leases.py` |
| crash/restart | PASS | `tests/persistence/test_atomic_failure_points.py`; `tests/persistence/test_restart_replay.py` |
| P2.8 typed boundary hardening | PASS | `tests/persistence/test_effect_commands.py`; `tests/persistence/test_outbox_leases.py` |
| P2.8 strict history verification | PASS | `tests/persistence/test_restart_replay.py` |
| P2.8 crash-point coverage | PASS | `tests/persistence/test_crash_restart_hardening.py` |
| no tracked SQLite artifacts | PASS | `git ls-files "*.sqlite" "*.sqlite3" "*.db" "*-wal" "*-shm"` returned no paths |

The local suite reports one expected warning because the offline socket test
deliberately attempts to construct a socket.

## CI gates

| OS | Python | Result | Workflow URL |
|---|---:|---|---|
| Ubuntu | 3.11 | PASS | [run 33876302395](https://github.com/az87988799/BG6022-v2/actions/runs/33876302395) |
| Ubuntu | 3.14 | PASS | [run 33876302395](https://github.com/az87988799/BG6022-v2/actions/runs/33876302395) |
| Windows | 3.14 | PASS | [run 33876302395](https://github.com/az87988799/BG6022-v2/actions/runs/33876302395) |
| Quality | 3.14 | PASS | [run 33876302395](https://github.com/az87988799/BG6022-v2/actions/runs/33876302395) |

## Durable-kernel invariants

- One accepted event increments revision exactly once.
- Duplicate command IDs do not duplicate events or effects.
- State/event/outbox/interrupt projection commit atomically.
- Every run has at most one pending interrupt.
- Expired interrupt becomes durable state and typed result.
- Event replay matches the stored snapshot hash.
- State-changing commands verify event count, sequence, last event, revision,
  result hash, and snapshot hash before writing.
- Outbox delivery is documented as at-least-once.
- Leased effects recover after expiry.
- Effect audit commands verify effect existence, run ownership, and terminal
  Outbox status.
- Non-typed worker handler returns fail closed.
- Lease renewal requires a positive duration and strictly extends the lease.

## Scope confirmation

- P2.8 fixes waiting-for-input effect transitions without introducing a
  backend, gateway, ORCA compiler/parser, or subprocess.
- No FakeBackend or ExecutionGateway.
- No ORCA compiler/parser/subprocess.
- No RDKit, PubChem, LLM, evidence, claim, report, API, or Slurm work.
- No legacy active state migration.
- P3 has not started.

## Decision

The P2.8 hardening implementation, local gates, and the four-job GitHub
Actions matrix pass on PR #3. V2-P2 remains `CONDITIONAL PASS` until an
independent GitHub review and user acceptance are recorded. PR #3 is not
merged, `main` is not marked `PASS`, and P3 has not started.
