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
SQL identities/checksums are frozen; schema-1 read compatibility is version-fixed.
Migration v4 adds dispatch permits,
command receipts, and the stronger constraints without rewriting v1-v3
checksums.

The user approved correcting v4 receipt interpolation and updating its checksum
on 2026-09-05; its callback identity is now
`p2-dispatch-permits-atomic-completion-v2`. Old-v4 development databases require
backup/recreation, not edits to schema_migrations. Migration v5 separately freezes
terminal attempt count and available/created/updated timestamps.

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
Application, worker and completion share a fixed immutable policy configuration;
`KernelApplicationService.create_worker()` is their common creation entry.
P2 policy versions 1 and 2 are a closed map with regression-frozen digests.
Claim scans with a stable keyset cursor; only successful claims count toward limit.
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
Historical schema-1 records retain their published JSON and text semantics in
internal read objects with their original hashes and audit binding checked.
Historical diagnostics are stripped from the handler view; the worker retains
the original permit for completion fencing. Completion protocol alone does not
identify the payload schema. The only accepted receipt conversion is v4's exact
empty-object conversion with verified hash and audit identity.

Command receipts are append-only. The authoritative event receipt is created
for every Event. A new Effect audit command ID creates an alias receipt for
the existing authoritative audit Event without creating another Event or
revision. Conflicting command ID/hash/type/Run bindings are typed conflicts;
damaged bindings are state-integrity errors.
New external command IDs require UUID4, after checking historical idempotency.
Internal completion IDs remain deterministic UUID5. Upgrade and claim checks
reject detected historical namespace collisions without replacing receipts;
every imminent generation is rechecked because UUID5 IDs cannot be reversed.

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
Database busy after handler execution returns a retry-later delivery report and
does not call the handler again within that run_once invocation. Fencing a late
result does not prevent an already-authorized handler from later starting;
physical execution/cancellation coordination remains a P3 Gateway gate.

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
