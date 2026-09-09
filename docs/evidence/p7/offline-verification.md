# P7 offline verification note

The completed local smoke path used a fresh temporary SQLite state root and
the registered fake P5 backend. It exercised the existing chain:

`agent-message → accept_plan → P4 worker → confirm_identity → P5 prepare →
approve opt → approve freq → approve sp → P6 assess/worker → Delivery → query`

Observed invariants from the chain:

- no P5 node advanced without its own pending approval token;
- P6 was handed the existing terminal P5 run and did not start ORCA;
- independent SP energy was selected from the SP result and retained its raw
  source token;
- a display-only table request changed only the rendered OutputSpec;
- `agent-verify` succeeded after a fresh process reopened the same state root;
- unsupported SP-only, Gibbs/ZPE, solvent, TS/IRC, arbitrary-method, and
  oxygen-singlet requests did not produce an executable plan.

Repair regressions additionally covered:

- response loss after accepted P4 handoff reused the same command ID and
  linked handoff state;
- response loss after accepted identity reused the consumed payload without a
  second action;
- a cancelled turn fenced a late model response and task creation;
- invalid model output used one durable format-repair slot;
- current-task and display-only queries preserved task/query identity; and
- the shared terminal driver required an explicit action token for generic
  acknowledgements;
- real-profile configuration without a ready ORCA did not issue an executable
  approval token;
- a DeepSeek v1 response was rejected at the v2 boundary without fallback;
- task-context QA used only the program's verified query result, not the model
  draft; and
- ambiguous result sources reported the selector conflict instead of choosing
  an arbitrary record.

This note is offline fake evidence, not a real ORCA or live-model gate.

Final local verification:

- `tests/p7`: 28 passed.
- Full local suite: 644 passed, 1 skipped, 1 warning.
- `ruff check src tests scripts`, `ruff format --check src tests scripts`,
  `compileall`, `python -m build --wheel --no-isolation`, and
  `python -m uv lock --check` passed. The wheel contains all three P7
  resources under `orca_agent/resources/p7/`.
