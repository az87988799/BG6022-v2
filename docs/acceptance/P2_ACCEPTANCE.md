# V2-P2 Acceptance

- Status: NO-GO
- Date: 2026-09-04
- Branch: `codex/v2-p2-hardening`
- Reviewed implementation commit: `d345e3088e9f4c2beecf48c31663d539d2509834`
- Acceptance PR: `#3`
- Latest verified workflow for reviewed implementation: `33880632709`
- P2.8.1 hardening status: implementation and four-job CI complete; independent review and merge pending
- Independent GitHub approval: Pending; PR #3 has no reviews
- `main` merge: Pending; `main` remains `a001a31b6c0123a24e7e5d89774b0a1799024a27`
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
| pytest | PASS (local runnable suite) | `PYTHONPATH=src pytest -q tests/contract tests/integration tests/persistence tests/unit -o addopts=''`: `133 passed` |
| ruff check/format | PASS | `ruff check src tests`; `ruff format --check src tests`; `60 files already formatted` |
| git diff check | PASS | `git diff --check` |
| package build | PASS | `uv build` produced sdist and wheel |
| migration fresh/idempotent/drift/rollback/v1-to-v2 | PASS | `tests/persistence/test_migrations.py` |
| reducer purity/replay | PASS | `tests/unit/orchestration/test_reducer.py`; `tests/persistence/test_restart_replay.py` |
| CAS concurrency | PASS | `tests/persistence/test_revision_cas.py` |
| command idempotency | PASS | `tests/persistence/test_run_event_atomicity.py`; `tests/persistence/test_effect_commands.py` |
| interrupt lifecycle | PASS | `tests/persistence/test_interrupt_lifecycle.py` |
| outbox lease/retry/dead-letter | PASS | `tests/persistence/test_outbox_leases.py` |
| crash/restart | PASS | `tests/persistence/test_atomic_failure_points.py`; `tests/persistence/test_restart_replay.py` |
| P2.8 typed boundary hardening | PASS | `tests/persistence/test_effect_commands.py`; `tests/persistence/test_outbox_leases.py` |
| P2.8 strict history and projection verification | PASS | `tests/persistence/test_restart_replay.py`; `tests/persistence/test_interrupt_lifecycle.py`; `src/orca_agent/infrastructure/integrity.py` |
| P2.8 stale-owner and full Effect spec protection | PASS | `tests/persistence/test_outbox_leases.py` |
| P2.8 composite run ownership | PASS | `tests/persistence/test_migrations.py`; `tests/persistence/test_interrupt_lifecycle.py` |
| P2.8 crash-point coverage | PASS | `tests/persistence/test_atomic_failure_points.py`; `tests/persistence/test_crash_restart_hardening.py` |
| P2.8 typed busy/locked boundary | PASS | `tests/persistence/test_outbox_leases.py`; `src/orca_agent/infrastructure/sqlite.py` |
| no tracked SQLite artifacts | PASS | `git ls-files "*.sqlite" "*.sqlite3" "*.db" "*-wal" "*-shm"` returned no paths |

The local environment does not have the optional `pytest-socket` package, so
the local command above excludes `tests/test_offline.py`; the CI test jobs run
the complete suite with the locked development dependencies.

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
- Waiting runs have exactly one event-derived pending interrupt; non-waiting runs
  have none.
- Interrupt and outbox source events must belong to the same run and match the
  event-derived projection fields.
- Outbox routing is checked against the complete immutable Effect specification
  hash before a worker can handle it.
- Outbox delivery is documented as at-least-once.
- Leased effects recover after expiry.
- Only the worker recorded in `completed_by_worker_id` may repeat terminal
  completion idempotently.
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

The P2.8.1 hardening implementation, local runnable gates, and the four-job
GitHub Actions matrix pass for the fixed implementation commit above. V2-P2
remains `NO-GO` because PR #3 is not independently approved or merged, `main`
has not received the hardening commit and its post-merge CI, and user
acceptance is still pending. P3 has not started.
