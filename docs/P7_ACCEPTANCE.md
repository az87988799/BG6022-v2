# BG6022 V2 P7 acceptance record

Status: **Implementation candidate — Owner acceptance pending**

This record distinguishes the attached implementation plan from acceptance of
the work. The plan is the engineering basis; the Owner must inspect the
implementation and explicitly accept it before P7 is marked complete.

## Baseline and scope

- Accepted main baseline: `c64f547956f9ede2c0db02757199d1e2c0dc86f0`.
- Implementation branch: `codex/v2-p7-chat-entry-repair`.
- Scope: repair the P7 conversation boundary and restart/reconciliation paths;
  preserve accepted P4/P5/P6 handoff identities after response loss; fence late
  cancelled responses; enforce the published DeepSeek resource contract and
  one bounded format repair; keep immutable Delivery records separate from
  rendered result views; provide the shared terminal chat driver and fixed
  `start_chat.cmd` entry point; and update tests/docs.
- No new real ORCA calculation or live DeepSeek evaluation is implied by this
  branch.

## Gate status

| Gate | Status | Evidence / limitation |
|---|---|---|
| implementation | READY FOR OWNER REVIEW | Repair services, immutable view model, shared terminal driver, fixed startup entry point, resources, tests, and docs are present on the repair branch. |
| offline_ci | PASS | `ruff check src tests scripts`; `python -m compileall -q src tests scripts`; P7 suite `22 passed`; full offline suite `637 passed, 1 skipped` with `tests/test_offline.py` excluded because the local environment does not provide `pytest-socket`; wheel/resource inclusion verified. |
| live_llm | NOT RUN | Requires an explicitly configured `DEEPSEEK_API_KEY`, model, and evaluation budget. |
| real_identity | NOT RUN | Offline P7 evidence uses the existing fake PubChem adapter; no network identity query is claimed. |
| real_result_query | NOT RUN | P7 fake-chain query is covered; historical P6 archive verification remains prior evidence and is not silently relabeled as a P7 live gate. |
| model_benefit | NOT RUN | No frozen DeepSeek holdout evaluation was authorized in this implementation turn. |
| owner_acceptance | PENDING | Must be changed only after the Owner verifies and accepts this branch. |

## Offline verification record

Implementation commit: the pushed repair commit on
`codex/v2-p7-chat-entry-repair` (the exact hash is reported with the handoff).
The commands actually run on the frozen working tree were:

```text
ruff check src tests scripts                             PASS
python -m compileall -q src tests scripts                 PASS
pytest -o addopts='' -q tests/p7                         22 passed
python -m pytest -o addopts='' -q --ignore=tests/test_offline.py
                                                           637 passed, 1 skipped
python -m build --wheel --no-isolation                       PASS
```

The wheel contains all three P7 resources under
`orca_agent/resources/p7/`. The required repair smoke path is:

1. Response loss after an accepted P4 handoff reuses the same command ID and
   does not duplicate the downstream request.
2. Response loss after an accepted identity action replays the original
   payload and does not mint a second action.
3. A cancelled turn cannot publish a late model response or create a task.
4. Invalid model output gets at most one durable format-repair attempt.
5. Current-task and display-only queries retain the task and preserve the
   original query text without creating a new task.
6. The shared terminal driver requires an explicit action token and keeps the
   direct entry point on the fake backend unless real ORCA is explicitly
   enabled.
7. The existing fake P4/P5/P6 chain still closes to immutable Delivery,
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
