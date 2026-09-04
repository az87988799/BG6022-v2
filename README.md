# BG6022-V2

Clean, independently versioned repository for the BG6022 V2 rebuild.

## Current scope: V2-P2

V2-P0 established the traceable repository and migration baseline. V2-P1 adds
the reproducible Python toolchain, versioned domain contracts, deterministic
canonical JSON/hash primitives, typed errors, import boundaries, and offline
quality gates. V2-P2 adds the durable reducer kernel: versioned commands and
events, SQLite migrations, atomic snapshots, interrupt projections, revision
CAS, command idempotency, and a recoverable transactional outbox.

P2 does not implement business logic, migrate legacy modules, or execute LLM,
PubChem, RDKit, or ORCA workloads. FakeBackend, Gateway, approval, execution,
evidence, reporting, API, and Slurm work remain future packages.

The legacy repository at `E:\BG6022` is a read-only reference. V2 uses a new
database and a new artifact root; legacy active state is never migrated.

## Repository policy

- Source repository: `E:\BG6022`
- V2 repository: `E:\BG6022-v2`
- Default branch: `main`
- Remote: `https://github.com/az87988799/BG6022-v2.git`
- No legacy `auto_dft1.0`, `.tmp`, `.idea`, or SQLite files are copied here.

The migration manifest records the provenance and semantic status of every
future migration. P2 state is configured through an explicit state root; the
legacy repository and its active state remain read-only and are never opened.

## P0 contents

The package under `src/orca_agent` contains P1 domain contracts plus the P2
durable kernel. Runtime execution, molecule identity, and scientific workflows
remain future work packages.

## P2 durable-kernel boundary

- Every accepted command produces one append-only, hash-verified event and one
  revision step.
- Run snapshot, event, interrupt projection, application result, and outbox
  registrations commit atomically.
- Replay verifies event sequence and snapshot hash; corrupted state fails
  closed.
- Outbox delivery is explicitly at-least-once. Lease ownership, retry backoff,
  and dead-letter state are durable and deterministic.
- The worker accepts only an injected handler in P2; no real external handler is
  connected.
