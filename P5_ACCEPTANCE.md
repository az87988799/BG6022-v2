# P5 Acceptance Record

Status: `PENDING_OWNER_ACCEPTANCE`

This file records implementation and verification status only. It does not
authorize a real ORCA run and does not claim that the Water real-execution
gates have passed. Scientific assessment, minimum-energy claims, and report
generation remain out of scope until P6.

## Implemented scope

- Schema 4, engine `p5-local-orca-v1`, policy 5, and migration v7
  (`local_jobs`) are implemented.
- The closed r2SCAN-3c/TightSCF SP, Opt, Freq compiler and parser are
  implemented with the five protocol IDs from the P5 plan.
- Deterministic ETKDGv3 geometry generation uses explicit hydrogens and seed
  `6022`; P4 `plan_ready` source ownership is checked.
- Fake execution, owner-scoped immutable artifacts, launch tickets, local
  runner supervision, Windows Job Object integration, cancel/timeout/reconcile
  handling, CLI request replay, and read-only evidence collection are present.
- Fake results are marked `fake_fixture`; all P5 exports keep
  `scientific_assessment=not_evaluated` and `claim_status=not_generated`.

## Verification status

| Gate | Status | Evidence or limitation |
|---|---|---|
| P5 offline workflow tests | `PASS` | `scripts/verify_p5.ps1`: 14 passed. |
| P1–P4 regression suite | `PASS` | Full suite: 433 passed, 1 expected socket-block warning. |
| Ruff / compileall | `PASS` | Run against `src`, `tests`, and `scripts`. |
| Wheel build | `PASS` | `python -m build --wheel --no-isolation`; `uv` is not installed on this host, so `uv lock --check` is not claimed. |
| Evidence collector | `PASS` on a generated fake P5 run | Read-only source DB/artifact verification succeeded; evidence is not real-ORCA evidence. |
| R01 Water real execution | `NOT_RUN` | Requires explicit owner authorization and a verified ORCA 6.1 `.exe`. |
| R02 restart/recovery | `NOT_RUN` | Requires the authorized Water real run. |
| R03 real cancellation | `NOT_EXERCISED` | Must not be inferred from a job that finishes normally. |
| R04 real timeout | `NOT_EXERCISED` | Must not be inferred from a job that finishes normally. |
| Owner acceptance | `PENDING` | Only the owner can change this record to accepted. |

## Real Gate authorization boundary

The real gate is prepared by:

```powershell
pwsh -File scripts/verify_p5_real_orca.ps1 -OrcaExecutable C:\path\to\orca.exe -OrcaVersion 6.1.0
```

The command remains preview-only unless both `-EnableReal` and
`-ConfirmWaterGate` are supplied. Do not run that execution form without
explicit authorization for the displayed Water protocol, executable path,
version, budgets, and state root.

## Owner acceptance checklist

- [ ] Offline and regression verification reviewed.
- [ ] Real ORCA executable path, version, and SHA-256 reviewed.
- [ ] R01 Water `opt_freq_sp` reviewed, including exact Opt geometry reuse.
- [ ] R02 restart/recovery reviewed.
- [ ] R03 cancellation evidence reviewed, or explicitly recorded as not exercised.
- [ ] R04 timeout evidence reviewed, or explicitly recorded as not exercised.
- [ ] Main-branch CI result reviewed after the pushed commit.
- [ ] Owner accepts P5 and authorizes the status to be changed from pending.

Until the final checklist is accepted, P5 must be reported as implemented but
not accepted, and this task must not be marked complete.
