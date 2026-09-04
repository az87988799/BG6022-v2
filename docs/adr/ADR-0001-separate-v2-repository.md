# ADR-0001: Keep V2 in a separate repository

- Status: Accepted
- Date: 2026-09-04
- Scope: V2-P0 repository boundary

## Context

The legacy `E:\BG6022` repository is a read-only reference at commit
`fac93b52e247041d9d5bd4ebaf9dd6d827653928`. It contains historical modules,
generated files, IDE state, and local runtime artifacts. Building V2 as a
normal worktree or copying the directory would make those concerns part of the
new project's initial history and could expose legacy active state to a new
execution engine.

## Decision

Create `E:\BG6022-v2` as an independent Git repository with `main` as its
default branch. Keep the legacy repository outside the V2 import and execution
paths. Migrate only deliberately selected code or completed evidence in later
work packages, with exact provenance recorded in the migration manifest.

V2 starts with a new database and artifact root. Active legacy state is never
restored or migrated.

## Consequences

- V2 history is clean, small, and auditable from its first commit.
- Legacy files such as `auto_dft1.0`, `.tmp`, `.idea`, and SQLite databases are
  excluded by construction and by ignore rules.
- Future migrations require explicit source/target mapping and semantic review.
- The two repositories must be inspected and versioned separately.
