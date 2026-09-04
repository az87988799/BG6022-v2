# V2-P0 Acceptance

- Status: PASS
- Date: 2026-09-04
- Baseline commit: `71d2a77325b778d46df4d884fb4e4b28835fdfe5`
- Python: `3.14.6`
- RDKit: `2026.03.5`
- ORCA: `6.1`
- ORCA calculation executed: No
- LLM/PubChem called: No

## Local gates

| Gate | Command | Result |
|---|---|---|
| Package compilation | `python -m compileall -q src` | PASS |
| Root-commit whitespace | `git diff-tree --check --root --no-commit-id HEAD` | PASS |
| Clean worktree before acceptance edits | `git status --porcelain=v1` | PASS |

## Isolation checks

- Legacy repository remains read-only.
- No legacy active state was migrated.
- No `auto_dft1.0`, `.tmp`, `.idea`, SQLite, ORCA scratch, or artifact root was copied.
- V2 keeps an independent Git history, database root, and artifact root.

## Decision

V2-P0 is accepted. V2-P1 may start on a dedicated feature branch.
