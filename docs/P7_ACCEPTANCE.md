# BG6022 V2 P7 acceptance record

Status: **Implementation candidate — Owner acceptance pending**

This record distinguishes the attached implementation plan from acceptance of
the work. The plan is the engineering basis; the Owner must inspect the
implementation and explicitly accept it before P7 is marked complete.

## Baseline and scope

- Accepted main baseline: `c64f547956f9ede2c0db02757199d1e2c0dc86f0`.
- Implementation branch: `codex/v2-p7-chat-entry-repair`.
- Scope: close the P7 real terminal path on top of the repair boundary;
  preserve accepted P4/P5/P6 handoff identities after response loss; fence late
  cancelled responses; enforce the v2 DeepSeek resource contract and one
  bounded format repair; keep immutable Delivery records separate from
  rendered result views; provide the shared terminal chat driver and fixed
  `start_chat.cmd` entry point; freeze the selected profile into approval
  payloads; add the project-scoped local-ORCA resource slot and explicit
  reconciliation path; and update tests/docs.
- No new real ORCA calculation, live DeepSeek evaluation, or live PubChem
  evaluation was run during this implementation turn.

## Gate status

| Gate | Status | Evidence / limitation |
|---|---|---|
| implementation | READY FOR OWNER REVIEW | Real/offline/deepseek-fake profiles, v2 model contract, durable budgets/cancellation, P4/P5/P6 handoffs, resource slot, immutable result views, shared terminal driver, fixed startup entry point, resources, tests, and docs are present on the branch. |
| offline_ci | PASS | `ruff check src tests scripts`; `ruff format --check src tests scripts`; `python -m compileall -q src tests scripts`; P7 suite `28 passed`; focused P7/P5/migration/import regression `57 passed`; full suite `644 passed, 1 skipped, 1 warning`; `python -m build --wheel --no-isolation`; and `python -m uv lock --check`. |
| live_llm | NOT RUN | Requires an explicitly configured `DEEPSEEK_API_KEY`, model, and evaluation budget. |
| real_identity | NOT RUN | Offline P7 evidence uses the existing fake PubChem adapter; no network identity query is claimed. |
| real_result_query | NOT RUN | P7 fake-chain query is covered; historical P6 archive verification remains prior evidence and is not silently relabeled as a P7 live gate. |
| model_benefit | NOT RUN | No frozen DeepSeek holdout evaluation was authorized in this implementation turn. |
| owner_acceptance | PENDING | Must be changed only after the Owner verifies and accepts this branch. |

## Offline verification record

Implementation commit: `094f8c0ab6be02787f89edaf79ec7902f9c7dd06` on
`codex/v2-p7-chat-entry-repair`.
The commands actually run on the frozen working tree were:

```text
ruff check src tests scripts                               PASS
ruff format --check src tests scripts                      PASS
python -m compileall -q src tests scripts                  PASS
.venv\Scripts\python.exe -m pytest -q tests/p7             28 passed
.venv\Scripts\python.exe -m pytest -q                     644 passed, 1 skipped, 1 warning
python -m build --wheel --no-isolation                     PASS
python -m uv lock --check                                  PASS
```

The local wheel contains the three P7 resources under
`orca_agent/resources/p7/`; the CI build is also configured to install the P7
dependency extra and verify the lock/build/resource path.
The required repair/closure smoke path is:

1. Response loss after an accepted P4 handoff reuses the same command ID and
   does not duplicate the downstream request.
2. Response loss after an accepted identity action replays the original
   payload and does not mint a second action.
3. A cancelled turn cannot publish a late model response or create a task.
4. Invalid model output gets at most one durable format-repair attempt.
5. Current-task and display-only queries retain the task and preserve the
   original query text without creating a new task.
6. The shared terminal driver requires an explicit action token; the real
   profile does not issue an executable token until its ORCA readiness checks
   pass, while the offline profile remains a no-network smoke path.
7. A project-scoped local ORCA slot prevents concurrent physical jobs and an
   unknown launch state remains available for `/reconcile`.
8. The existing fake P4/P5/P6 chain still closes to immutable Delivery,
   separate rendered view, export, and integrity verification without
   recalculation.

The fake source is labelled as fake in P5 records and is not scientific real-run
evidence. Unsupported requests (SP-only, Gibbs/ZPE, solvent, TS/IRC, oxygen
singlet baseline conflict, and arbitrary methods) must end without an
executable plan.

## Acceptance boundary

P7 is not marked complete by a green test run alone. The Owner must review the
code, the exact pushed commit, gate statuses, limitations, and the distinction
between offline fake evidence and live/real gates, then provide explicit
acceptance. Until then this file remains `Owner acceptance pending`.
