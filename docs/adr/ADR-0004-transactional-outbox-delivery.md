# ADR-0004: Recoverable transactional outbox delivery

- Status: Accepted
- Date: 2026-09-05
- Scope: V2-P2 outbox, P2 hardening repair

## Context

Reducer transitions may describe work that must happen after the database
transaction commits. Calling a handler inside the transaction makes rollback
ambiguous, while writing the run state first can lose the effect on a process
crash. The earlier P2 implementation also allowed a cancellation or a stale
worker to race with dispatch, and it could leave a terminal receipt without a
matching audit Event.

## Decision

P2 registers immutable effect specifications in the `outbox` table in the
same transaction as the source Event and Run snapshot. An effect ID is a
deterministic UUID5 derived from the source Event ID and zero-based effect
index. `(source_event_id, effect_index)` is unique. The v2 and v3 migration
identities and callbacks are frozen; migration v4 adds dispatch permits,
command receipts, and the stronger constraints without rewriting v1-v3
checksums.

The v4 outbox records the complete effect specification hash, including the
effect identity, Run and source Event ownership, routing type/class, versions,
payload, and payload hash. Composite `(run_id, event_id)` foreign keys bind
source and audit Events to the same Run. SQLite immutability triggers protect
effect identity/specification, terminal metadata, dispatch permit metadata,
and command receipts. A Run has at most one `dispatching` effect.

There are three durable linearization points:

1. `leased -> dispatching` authorizes a handler. The update is guarded by
   owner, generation, live lease, replay-verified Run state, and the exact
   registry policy version.
2. `RUN_CANCELLED` Event, queued/leased sibling cancellation, and Run CAS
   commit together. Cancellation never converts a `dispatching` effect.
3. A terminal completion appends its audit Event, binds the terminal outbox
   receipt, applies interrupt projections and sibling cancellation, updates the
   Run snapshot, and appends the command receipt in one transaction.

Claim and authorization use the same exact, fail-closed effect registry.
Unknown types and class/type mismatches are blocked. External effects are
blocked while a Run waits for input; only explicitly registered safe internal
effects may continue. A permit is rechecked immediately before completion, so
an expired or reclaimed generation cannot report a late success. Only the
worker and generation recorded in a terminal receipt may repeat that exact
terminal completion idempotently.

Worker handlers must return the typed `HandlerResult`. Invalid or non-typed
returns become a fixed `invalid_handler_result`; handler exceptions and other
failures use a closed `HandlerErrorCode` set with fixed public messages.
Successful effects persist only the bounded versioned
`effect-success/v1` receipt. Free-form error text, paths, tokens, tracebacks,
and arbitrary success JSON are not part of the completion contract.

Command receipts are append-only. The authoritative event receipt is created
for every Event. A new Effect audit command ID creates an alias receipt for
the existing authoritative audit Event without creating another Event or
revision. Conflicting command ID/hash/type/Run bindings are typed conflicts;
damaged bindings are state-integrity errors.

Before every state-changing command and worker dispatch, the repositories
verify the event count, sequence and revision chain, last Event, Event result
and envelope hash chain, snapshot hash, and event-derived interrupt/outbox
projections. Waiting Runs must have exactly one matching pending interrupt.
Corrupt IDs, versions, numeric fields, projection links, and locked writes
surface typed storage errors.

P2 promises at-least-once delivery only. If a handler succeeds and the
completion transaction fails before commit, the same effect ID may be
delivered again after lease expiry. Exactly-once physical side effects require
the idempotency contract of the later Gateway/adapter package and are not
claimed here.

## Consequences

- A committed Event cannot lose its registered effect or command receipt.
- Cancel/dispatch races have one durable winner and cannot create a permit
  after an accepted cancellation.
- A protocol-4 terminal effect cannot commit without its matching audit Event.
- Lease expiry makes abandoned work recoverable by another worker, while
  generation and owner fencing reject late completion.
- Two workers cannot hold the same live lease or dispatch two effects for one
  Run, but a handler may see a stable effect ID more than once.
- Event and projection tampering is detected before worker routing or the next
  state-changing command.
- Legacy protocol-3 terminal rows remain readable during v4 migration without
  inventing an audit Event that did not exist.
- P2 workers receive only injected test handlers; no ORCA, LLM, PubChem,
  RDKit, network, or real scientific/filesystem side effect is connected.
