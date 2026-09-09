# P7 offline verification note

The completed local smoke path used a fresh temporary SQLite state root and
the registered fake P5 backend. It exercised:

`agent-message → accept_plan → P4 worker → confirm_identity → P5 prepare →
approve opt → approve freq → approve sp → P6 assess/worker → Delivery → query`

Observed invariants:

- no P5 node advanced without its own pending approval token;
- P6 was handed the existing terminal P5 run and did not start ORCA;
- independent SP energy was selected from the SP result and retained its raw
  source token;
- a display-only table request changed only the rendered OutputSpec;
- `agent-verify` succeeded after a fresh process reopened the same state root;
- unsupported SP-only, Gibbs/ZPE, solvent, TS/IRC, arbitrary-method, and
  oxygen-singlet requests did not produce an executable plan.

This note is offline fake evidence, not a real ORCA or live-model gate.

Final local verification:

- `tests/p7`: 14 passed.
- Import-boundary, migration, and P7 focused regression: 28 passed.
- Existing offline suite: 629 passed, 1 skipped, with
  `tests/test_offline.py` excluded because `pytest-socket` is not installed in
  this environment.
- `ruff check src tests`, `compileall`, and wheel build passed; the wheel
  contains the three files under `orca_agent/resources/p7/`.
