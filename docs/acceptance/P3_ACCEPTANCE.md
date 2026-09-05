# V2-P3 Acceptance

- Status: IMPLEMENTING
- Branch: `codex/v2-p3-fake-vertical-slice`
- P3 implementation base: `5d899d4a03bc4600115f4a47c26f8544b9324bcd`
- P2 implementation: `eaf48277c106ec4486cad1785bff1de8e779a887`
- P2 merge: PR #3, merge commit `5d899d4a03bc4600115f4a47c26f8544b9324bcd`
- P2 main CI: [33952574495](https://github.com/az87988799/BG6022-v2/actions/runs/33952574495), four jobs green
- P2 governance: single-maintainer owner acceptance; independent GitHub review waived and not fabricated
- P3 scope: offline Water `water_sp_v1` FakeBackend vertical slice only
- ORCA / LLM / PubChem / RDKit / network business calls: NOT RUN — out of P3 scope

## P3-0 baseline

P3 reuses the P2 runs/events/outbox/interrupts/command-receipt kernel and its
transaction boundaries. P2 schema versions, engine rules, migrations v1-v5 and
policy v1/v2 remain historical contracts. P3 adds explicit workflow schema 2,
engine `p3-water-v1`, policy v3 and migration v6; it does not globally change
the P1 domain schema constant or rewrite old records.

The implementation will add only the fixed Water planning, validation, exact
approval, persistent fake execution, artifact/evidence/claim/report path and
thin CLI required by the approved P3 plan. Real ORCA execution, external
identity services, network retrieval and active legacy-state migration remain
outside scope.

## Interface gap recorded at P3-0

- Existing `KernelState`, `KernelEvent` and P2 command unions are schema-1
  contracts; P3 needs explicit schema-2 workflow types and routing.
- Existing P2 migrations stop at v5; P3 needs a new migration for business
  records, action/job/artifact/evidence indexes and completed workflow support.
- Existing policy v1/v2 registers only test/audit effects; P3 needs a closed
  v3 registry for the three named fake-pipeline effects.
- Existing completion persists P2 effect receipts but does not persist P3
  successor business projections; P3 will add a controlled completion path,
  retaining permit fencing and the shared worker.
- No CLI, package fixture, artifact store, fake backend, evidence pipeline or
  report renderer currently exists; these are the bounded P3 additions.

## Required evidence

This file remains `IMPLEMENTING` until the P3-6 technical gates, CI, merge,
post-merge main CI and final owner acceptance are recorded. It will not claim
PASS from test count alone.
