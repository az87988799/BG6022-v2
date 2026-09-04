# BG6022-V2

Clean, independently versioned repository for the BG6022 V2 rebuild.

## Current scope: V2-P1

V2-P0 established the traceable repository and migration baseline. V2-P1 adds
the reproducible Python toolchain, versioned domain contracts, deterministic
canonical JSON/hash primitives, typed errors, import boundaries, and offline
quality gates.

P1 does not implement business logic, migrate legacy modules, or execute LLM,
PubChem, or ORCA workloads. P2 has not started.

The legacy repository at `E:\BG6022` is a read-only reference. V2 uses a new
database and a new artifact root; legacy active state is never migrated.

## Repository policy

- Source repository: `E:\BG6022`
- V2 repository: `E:\BG6022-v2`
- Default branch: `main`
- Remote: `https://github.com/az87988799/BG6022-v2.git`
- No legacy `auto_dft1.0`, `.tmp`, `.idea`, or SQLite files are copied here.

The migration manifest records the provenance and semantic status of every
future migration. Work beyond V2-P0 begins only after this baseline commit is
reviewed.

## P0 contents

The package under `src/orca_agent` currently contains only P1 domain contracts
and deterministic primitives. Runtime execution, persistence, molecule
identity, and scientific workflows remain future work packages.
