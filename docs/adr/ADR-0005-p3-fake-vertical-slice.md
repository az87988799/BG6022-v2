# ADR-0005: P3 Water FakeBackend vertical slice

- Status: Accepted for implementation
- Date: 2026-09-05
- Scope: V2-P3 fixed offline Water workflow

## Decision

P3 uses the existing transactional kernel and worker. New runs use explicit
workflow schema 2 and engine `p3-water-v1`; P2 schema-1 runs remain unchanged.
The only P3 effects are `external.p3.dispatch_fake`, `internal.p3.assess`, and
`internal.p3.render_report`, registered by fixed policy v3. Approval is a typed,
exact grant for the displayed action, not a generic interrupt response.

The fake backend is a separate persistent execution fact store. Its
`submit_or_get` operation runs outside the business SQLite transaction and is
idempotent by a stable action/approval-derived execution identity. Backend facts
are not scientific acceptance; only validated artifact, evidence, assessment
and claim records can advance the workflow.

Completion remains fenced by the P2 permit and writes event, projections,
outbox successors, run CAS and command receipt atomically. Handlers never call
the application service to advance their own run and never choose successor
effects. Reports are generated from one validated report model in Markdown and
JSON and visibly state that values are fake fixture data, not ORCA results.

No ORCA, LLM, PubChem, RDKit, external network, shell execution or real
scientific backend is connected in P3. Physical exactly-once execution and
real cancellation remain later Gateway/adapter concerns.
