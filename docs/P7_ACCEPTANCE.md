# BG6022 V2 P7 acceptance record

Status: **Implementation candidate — Owner acceptance pending**

This record distinguishes the attached implementation plan from acceptance of
the work. The plan is the engineering basis; the Owner must inspect the
implementation and explicitly accept it before P7 is marked complete.

## Baseline and scope

- Implementation baseline: `d728ee8a2a6aa488fbfc8aaa98ac33cb7555ba07`.
- Implementation branch: `codex/v2-p7-chat-entry-repair`.
- Implementation commit: `9f5f5222a72427909386c16879bace94215feef5`.
- Scope: implement the plan's current PubChem/SMILES → RDKit ETKDGv3 → ORCA
  preparation route while keeping v4 and legacy records readable; freeze a
  versioned preparation snapshot and one final confirmation; preserve exact
  P4/P5 history references for Opt follow-ups; bind P5 geometry and derived
  node credentials to the parent authorization; validate both P6 input and
  successful-Opt output origins; keep immutable Delivery records separate from
  rendered result views; and update the terminal usage/acceptance documentation.
- No real ORCA calculation, live DeepSeek evaluation, or live PubChem
  evaluation is claimed for this implementation turn.

## Gate status

| Gate | Status | Evidence / limitation |
|---|---|---|
| implementation | READY FOR OWNER REVIEW | Current v5 intake, preparation snapshot, RDKit draft binding, historical Opt route, single final confirmation, authorization credentials, exact P5/P6 source validation, compatibility readers, tests, and docs are present on this branch. |
| offline_ci | PASS | Full offline suite, focused P5/P6/P7 regression, Ruff, compile, wheel build, and lock-file checks passed; one expected socket-block warning is recorded below. |
| live_llm | NOT RUN | Requires an explicitly configured `DEEPSEEK_API_KEY`, model, and evaluation budget. |
| real_identity | NOT RUN | Offline evidence uses fake/local adapters; no network identity query is claimed. |
| real_result_query | NOT RUN | Fake-chain result delivery and historical archive verification are offline evidence, not a live P7 gate. |
| model_benefit | NOT RUN | No frozen DeepSeek holdout evaluation was authorized in this implementation turn. |
| owner_acceptance | PENDING | Must be changed only after the Owner verifies and accepts the pushed branch/commit. |

## Offline verification record

The implementation commit above contains the code and plan-aligned tests. This
acceptance record is updated in a separate documentation commit so the
implementation hash is not self-referential. The final pushed branch contains
both commits. The final verification record is:

```text
ruff check src tests                                      PASS
ruff format --check (changed implementation/test files)    PASS
python -m compileall -q src tests                         PASS
.venv\Scripts\python.exe -m pytest -q tests/p6 tests/p5 tests/p7
                                                          255 passed, 1 skipped
.venv\Scripts\python.exe -m pytest -q                   674 passed, 1 skipped, 1 warning
python -m build --wheel --no-isolation                   PASS
python -m uv lock --check                                PASS
```

The full-suite warning is the expected `pytest_socket` warning from
`tests/test_offline.py::test_pytest_default_blocks_socket_creation`; the test
still passes. The wheel contains the three v5 P7 resources under
`orca_agent/resources/p7/`.

The focused evidence must cover: automatic v5 preparation with one
`confirm_execution`; idempotent snapshot replay; no ORCA start before final
confirmation; historical Opt reuse without fresh RDKit embedding; exact
geometry/result/origin binding through P5/P6; legacy v4 reading; bounded
authorization credentials; cancellation/recovery; and immutable delivery plus
display-only views.

The fake source is labelled as fake in P5 records and is not scientific
real-run evidence. Unsupported requests and unready real profiles must end
without an executable approval token. The plan's later Windows
DeepSeek/PubChem/ORCA validation remains a separate R6 acceptance activity.

## Acceptance boundary

P7 is not marked complete by a green test run, a commit, or a push alone. The
Owner must review the exact pushed commit, gate statuses, limitations, and the
distinction between offline fake evidence and live/real gates, then provide
explicit acceptance. Until then this file remains **Owner acceptance pending**.
