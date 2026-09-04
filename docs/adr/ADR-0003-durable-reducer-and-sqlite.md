# ADR-0003: Durable reducer and transactional SQLite kernel

- Status: Accepted
- Date: 2026-09-04
- Scope: V2-P2 durable kernel

## Context

The later agent and execution layers need a small authoritative state machine
that survives process restarts and does not confuse a partially written record
with an accepted command. A Python-only in-memory state object cannot provide
the required revision, replay, and crash boundaries.

## Decision

P2 uses typed command and event envelopes around a pure reducer. The reducer
receives only the current `KernelState` and a fully formed `KernelEvent`; it
does not read a clock, generate IDs, access SQLite, inspect files, or call an
external service. `ENGINE_VERSION` is kept separate from record schema and
package versions.

SQLite is the first durable store. Each unit of work owns one connection with
foreign keys enabled, WAL journaling, `synchronous=FULL`, and a 5000 ms busy
timeout. Writes begin with `BEGIN IMMEDIATE`. A command transaction performs
the run CAS, event append, interrupt projection, outbox registration, and
application result persistence together. Any exception rolls the whole unit of
work back.

The `runs` snapshot is a cache of the event stream, not an independent source
of truth. Reads can use strict replay to verify contiguous sequence numbers,
payload hashes, the snapshot hash, revision, and last event ID. A mismatch
raises a typed `StateIntegrityError`; P2 never silently repairs history.

## Consequences

- One accepted event advances a run revision exactly once.
- Stale commands fail through a typed CAS conflict rather than overwriting a
  newer state.
- Close/reopen and crash tests can prove recovery without invoking a backend.
- SQLite WAL is a same-host policy; a network filesystem is outside P2 scope.
- P2 creates only `schema_migrations`, `runs`, `events`, `interrupts`, and
  `outbox`; scientific and execution tables are intentionally deferred.
