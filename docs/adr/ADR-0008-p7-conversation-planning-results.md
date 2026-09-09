# ADR-0008: P7 conversation, planning, and result delivery

Status: **Accepted for implementation; Owner acceptance pending**

Date: 2026-09-09 (Asia/Hong_Kong)

## Context

P7 adds a natural-language conversation boundary around the existing P4
identity, P5 execution, and P6 assessment services. The boundary must remain
auditable: a model may interpret a request, but it must not invent a molecule,
replace a hard constraint, approve work, start ORCA, or manufacture a result.
The user must be able to continue a session, operate several independent tasks,
and query a completed delivery without recalculation.

## Decisions

1. Persist a versioned conversation, ordered turn records, independent task
   records, pending action tokens, model attempts/receipts, handoffs, and
   immutable delivery records in additive SQLite migration 8. Existing P1-P6
   tables and hashes remain untouched.
2. Use four explicit intent kinds: `chemical_calculation`, `chemistry_qa`,
   `general_qa`, and `context_query`. Calculation requests, plans, and output
   specifications are separate contracts.
3. Keep the registered P7 capability narrow: neutral closed-shell singlet,
   gas-phase r2SCAN-3c, Opt → Freq → independent SP, 4 cores / 2048 MB /
   `%maxcore 384`, one implicit thread. SP-only, solvent, Gibbs/ZPE, TS/IRC,
   arbitrary methods, and unsupported electronic states are rejected without
   silent substitution.
4. Use the deterministic baseline planner by default. The fake planner is for
   offline tests. The DeepSeek adapter is opt-in, uses Chat Completions JSON
   output, and is bounded by durable per-turn/session budgets; it has no tools,
   database access, ORCA access, or implicit retry.
5. Accept plan, confirm identity, and approve each P5 node as separate,
   object-bound server-side tokens. Natural-language acknowledgements can
   accept one unique current token only under a conservative rule; otherwise
   the CLI token action is required.
6. Delivery selects values only from verified P5/P6 records. It preserves raw
   energy tokens, source IDs, status, artifacts, and scientific limitations;
   an output request can change presentation without mutating the physical
   delivery or re-running a calculation.
7. P7 CLI commands are explicit `agent-*` commands so legacy run-oriented
   commands remain compatible. `--allow-real-orca` only enables a backend; it
   never substitutes for a P5 grant.

## Consequences

The P7 state graph is recoverable across processes and crash windows, but the
initial implementation adds more durable records and requires migration 8.
Offline fake-chain tests are strong evidence for orchestration and binding,
not evidence of a new real ORCA calculation, a live DeepSeek benefit, or Owner
acceptance. Those gates remain separately reported in `docs/P7_ACCEPTANCE.md`.
