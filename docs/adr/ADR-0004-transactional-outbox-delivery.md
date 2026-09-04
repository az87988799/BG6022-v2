# ADR-0004: Recoverable transactional outbox delivery

- Status: Accepted
- Date: 2026-09-04
- Scope: V2-P2 outbox

## Context

Reducer transitions may describe work that must happen after the database
transaction commits. Calling an external handler inside the transaction would
make rollback ambiguous, while writing the run state first could lose the
effect on a process crash.

## Decision

P2 registers immutable effect specifications in the `outbox` table in the
same transaction as the source event and run snapshot. An effect ID is a
deterministic UUID5 derived from the source event ID and zero-based effect
index. `(source_event_id, effect_index)` is unique, and replaying a transition
is therefore a no-op for an already registered effect. The V2 hardening
migration adds a complete `spec_hash` over the effect identity, run and source
event ownership, routing class/type, versions, payload, and payload hash. The
outbox and interrupt source-event foreign keys are composite `(run_id, event_id)`
references, so a valid ID from another run cannot be attached to this run.

An explicit one-shot worker claims due effects with a durable lease. Claim,
renew, success, retry, and dead-letter operations verify the lease owner and
use the injected UTC clock. Terminal completion records
`completed_by_worker_id`; a repeated completion is idempotent only for that
same worker, so a stale worker cannot report success after its lease was
reclaimed. Retry delays are deterministic (1, 2, 4, 8, and 16 seconds, capped
at 60 seconds) with a maximum of five attempts. Error messages are bounded
safe summaries and never contain tracebacks.

P2 promises at-least-once delivery only. If a handler succeeds and the success
record fails to commit, the same effect ID can be delivered again after lease
expiry. Exactly-once physical side effects require the idempotency contract of
the later Gateway/adapter package and are not claimed here.

## Consequences

- A committed event cannot lose its registered effect.
- Lease expiry makes abandoned work recoverable by another worker.
- Two workers cannot hold the same live lease, but a handler may see a stable
  effect ID more than once.
- P2 workers receive only injected test handlers; no ORCA, LLM, PubChem, RDKit,
  network, or real filesystem side effect is connected.
