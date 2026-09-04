# ADR-0004: Recoverable transactional outbox delivery

- Status: Accepted
- Date: 2026-09-04
- Scope: V2-P2 outbox, P2.8.2/P2.9 hardening

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

An explicit one-shot worker claims due effects with a durable lease. The
verified worker path uses `BEGIN IMMEDIATE`, replays the candidate run, checks
all interrupt and outbox projections, and verifies the complete effect
specification immediately before invoking the handler. It claims and dispatches
one effect at a time, so a stale preclaimed batch cannot cross a handler call.
Terminal or corrupt runs fail closed without invoking a handler.

Lease generations fence every renew, success, retry, and dead-letter write.
The SQL predicates include effect ID, owner, generation, and live expiry.
Terminal completion records `completed_by_worker_id` and
`terminal_generation`; a repeated completion is idempotent only for that same
worker and generation with the same receipt, so a stale worker cannot report
success after its lease was reclaimed. Retry delays are deterministic (1, 2,
4, 8, and 16 seconds, capped at 60 seconds) with a maximum of five attempts.
Handler returns must be the typed `HandlerResult`; all other returns fail
closed. Persisted error text is reduced to bounded, fixed safe summaries and
never contains tracebacks, tokens, or other diagnostic secrets.

Migration v3 adds event envelope hashes and a previous-event hash chain,
terminal outbox receipts, cancellation, composite run/source-event ownership,
and append-only/monotonic outbox triggers. Successful summaries are persisted
and hash-bound before an audit event is appended. An effect audit command binds
at most one matching event; retries return the authoritative stored result and
conflicting summaries or errors are typed conflicts. A terminal run cancels
pending and leased sibling effects before its snapshot CAS completes.

Before each state-changing command, the repository verifies the event count,
sequence and revision chain, last event, event result/envelope hashes, state
hash, and the event-derived interrupt/outbox projections. Corrupt IDs,
versions, numeric fields, and locked writes surface as typed storage errors.

P2 promises at-least-once delivery only. If a handler succeeds and the success
record fails to commit, the same effect ID can be delivered again after lease
expiry. Exactly-once physical side effects require the idempotency contract of
the later Gateway/adapter package and are not claimed here.

## Consequences

- A committed event cannot lose its registered effect.
- Lease expiry makes abandoned work recoverable by another worker.
- Two workers cannot hold the same live lease, but a handler may see a stable
  effect ID more than once.
- A run cancellation fences any queued or leased sibling before the terminal
  snapshot is committed; an already-running handler remains subject to the
  at-least-once adapter boundary.
- Event and projection tampering is detected before worker routing or the next
  state-changing command.
- Existing v1/v2 migration checksums remain unchanged; v3 upgrades legacy
  terminal rows without inventing an audit event that did not exist.
- P2 workers receive only injected test handlers; no ORCA, LLM, PubChem, RDKit,
  network, or real filesystem side effect is connected.
