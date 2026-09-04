# Source baseline

This document records the read-only source and environment observed while
creating the V2-P0 repository.

## Capture

- Capture date: 2026-09-04
- Time zone: `Asia/Hong_Kong`
- Work package: `V2-P0`

## Legacy source repository

| Field | Value |
|---|---|
| Source repository | `az87988799/BG6022` |
| Source remote URL | `https://github.com/az87988799/BG6022.git` |
| Source path | `E:\BG6022` |
| Source branch | `agent/rename-orca-dft-agent` |
| Source commit | `fac93b52e247041d9d5bd4ebaf9dd6d827653928` |
| Known remote audit commit | `fac93b52e247041d9d5bd4ebaf9dd6d827653928` |
| Local HEAD matches known baseline | Yes |
| Local working tree clean | No |

The source status at capture time was:

```text
?? .tmp/tritondft-analysis/
```

The untracked legacy `.tmp` path was not copied or modified. No checkout,
reset, or other source-tree write was performed.

## Environment version probes

| Component | Observed value |
|---|---|
| Git | `2.55.0.windows.3` |
| Python executable | `E:\anaconda\python.exe` |
| Python | `3.14.6` |
| Platform | `Windows-11-10.0.26200-SP0`, `AMD64` |
| RDKit | `2026.03.5` |
| ORCA environment | `ORCADIR=E:\orca\` |
| ORCA executable | `E:\orca\orca.exe` exists; not on PATH |
| ORCA version | Unknown; executable file metadata exposed no version |

Only executable/file metadata was inspected for ORCA. No ORCA process was
started and no calculation was run. No LLM or PubChem call was made.

## V2 isolation rules

- V2 uses a completely new database location and artifact root.
- The legacy repository remains a read-only reference.
- Legacy active state, including pending runs, approvals, leases, and
  interrupts, is not migrated.
- Legacy source modules, `.tmp`, `.idea`, and SQLite files are not copied.
- Future migration provenance must be recorded in
  `docs/MIGRATION_MANIFEST.md`.

## Target repository

- Path: `E:\BG6022-v2`
- Default branch: `main`
- Initial state: empty directory before `git init`
- Repository history: independent from the legacy repository
