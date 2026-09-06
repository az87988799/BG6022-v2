# Temporary Workspace Convention

All local, generated, and ignored temporary state for this repository belongs
under `.tmp/`. The top-level `.tmp-*` names are legacy locations only and must
not be used for new work.

## Layout

Use the phase as the first partition and the operation as the second partition
when the phase has more than one kind of temporary state:

```text
.tmp/
  p0/
  p1/
  p2/
  p3/
  p4/
    implementation/
    verification/
    audit/
  p5/
```

Each verification or runtime attempt should use its own fresh directory below
the relevant operation directory. Databases, artifacts, command output, and
scratch files stay together inside that attempt directory. Do not put SQLite
files, ORCA scratch/artifacts, or generated reports at the repository root.

## Current organization

The previously scattered temporary paths were consolidated as follows:

| Previous path | New path |
| --- | --- |
| `.tmp/p2-minimal-pr-body-eaf4827.md` | `.tmp/p2/pr-body-eaf4827.md` |
| `.tmp/p3-repair-pr-5b20ca.md` | `.tmp/p3/pr-body-5b20ca.md` |
| `.tmp/p4-audit-pr.md` | `.tmp/p4/audit/p4-audit-pr.md` |
| `.tmp/p4-reaudit-pr.md` | `.tmp/p4/audit/p4-reaudit-pr.md` |
| `.tmp-cli-p4-1` | `.tmp/p4/implementation/cli-p4-1` |
| `.tmp-p4-debug-{2..6}` | `.tmp/p4/implementation/debug-{2..6}` |
| `.tmp-p4-smoke-{1..6}` | `.tmp/p4/implementation/smoke-{1..6}` |
| `.tmp-verify-p4-2` | `.tmp/p4/verification/verify-p4-2` |
| `.tmp-verify-p4-doc` | `.tmp/p4/verification/verify-p4-doc` |
| `.tmp-verify-p4-final` | `.tmp/p4/verification/verify-p4-final` |
| `.tmp-p4-audit-live` | `.tmp/p4/audit/audit-live` |
| `.tmp-p4-audit-offline` | `.tmp/p4/audit/audit-offline` |
| `.tmp-p4-reaudit-live` | `.tmp/p4/audit/reaudit-live` |

Acceptance manifests retain the paths recorded at the time their evidence was
created. They are historical evidence, not instructions to create new
top-level temporary paths.
