# BG6022-V2

Clean, independently versioned repository for the BG6022 V2 rebuild.

## Current scope: V2-P0

V2-P0 establishes a traceable repository and migration baseline only. It does
not implement business logic, migrate legacy modules, or execute LLM, PubChem,
or ORCA workloads.

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

The package under `src/orca_agent` is an intentionally empty importable
placeholder. `pyproject.toml` is a minimal packaging skeleton with no runtime
dependencies.
