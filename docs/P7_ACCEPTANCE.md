# BG6022 V2 P7 acceptance record

Status: **Implementation candidate — Owner acceptance pending**

This record distinguishes the attached implementation plan from acceptance of
the work. The plan is the engineering basis; the Owner must inspect the
implementation and explicitly accept it before P7 is marked complete.

## Baseline and scope

- Accepted main baseline: `c64f547956f9ede2c0db02757199d1e2c0dc86f0`.
- Implementation branch: `codex/v2-p7-conversation-planning-results`.
- Scope: P7 conversation, constrained planning, P4/P5/P6 handoffs, P6-to-
  Delivery result closure, deterministic query/export/verify, DeepSeek adapter,
  explicit `agent-*` CLI, resources, tests, and docs.
- No new real ORCA calculation or live DeepSeek evaluation is implied by this
  branch.

## Gate status

| Gate | Status | Evidence / limitation |
|---|---|---|
| implementation | READY FOR OWNER REVIEW | P7 contracts, migration 8, services, CLI, and docs are present on the implementation branch. |
| offline_ci | PASS | `ruff check src tests`; `python -m compileall -q src tests`; P7 suite `14 passed`; import/migration/P7 focused set `28 passed`; full offline suite `629 passed, 1 skipped` with `tests/test_offline.py` excluded because the local environment does not provide `pytest-socket`; wheel build and P7 resource inclusion verified. |
| live_llm | NOT RUN | Requires an explicitly configured `DEEPSEEK_API_KEY`, model, and evaluation budget. |
| real_identity | NOT RUN | Offline P7 evidence uses the existing fake PubChem adapter; no network identity query is claimed. |
| real_result_query | NOT RUN | P7 fake-chain query is covered; historical P6 archive verification remains prior evidence and is not silently relabeled as a P7 live gate. |
| model_benefit | NOT RUN | No frozen DeepSeek holdout evaluation was authorized in this implementation turn. |
| owner_acceptance | PENDING | Must be changed only after the Owner verifies and accepts this branch. |

## Offline verification record

The implementation commit SHA will be recorded immediately after the code
commit in the follow-up documentation commit. The commands actually run on
the frozen working tree were:

```text
ruff check src tests                                      PASS
python -m compileall -q src tests                         PASS
pytest -o addopts='' -q tests/p7                         14 passed
pytest -o addopts='' -q tests/test_import_boundaries.py \
  tests/test_p2_import_boundaries.py tests/persistence/test_migrations.py \
  tests/p7/test_p7_conversation_and_results.py            28 passed
python -m pytest -o addopts='' -q --ignore=tests/test_offline.py
                                                           629 passed, 1 skipped
python -m build --wheel --no-isolation                       PASS
```

The wheel contains all three P7 resources under
`orca_agent/resources/p7/`. The required P7 smoke path is:

1. Natural-language calculation request → constrained Opt/Freq/independent SP
   plan.
2. Explicit plan token → P4 identity pending.
3. Explicit identity token → P5 node approval pending.
4. Three separate P5 node tokens → fake P5 completion.
5. Automatic P6 assessment handoff → immutable Delivery.
6. Same-conversation query, display-only formatting change, export, and
   integrity verification without recalculation.

The fake source is labelled as fake in P5 records and is not scientific real-run
evidence. Unsupported requests (SP-only, Gibbs/ZPE, solvent, TS/IRC, oxygen
singlet baseline conflict, and arbitrary methods) must end without an
executable plan.

## Acceptance boundary

P7 is not marked complete by a green test run alone. The Owner must review the
code, the exact pushed commit, gate statuses, limitations, and the distinction
between offline fake evidence and live/real gates, then provide explicit
acceptance. Until then this file remains `Owner acceptance pending`.
