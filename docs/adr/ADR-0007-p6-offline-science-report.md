# ADR-0007: P6 offline scientific assessment and report

Status: Implemented; Owner acceptance pending

Date: 2026-09-07

Base commit: `0484b04fa70502da7a32a7cde0b0b09a8fdd4826`

Branch: `codex/v2-p6-science-report`

## Context

P6 must turn a completed P5 result set into a reproducible scientific
assessment without changing P5 history or silently starting another scientific
calculation. The result must be auditable offline, retain raw observation
tokens and locations, and distinguish supported claims from qualified or
inconclusive results.

## Decision

P6 is a derived schema-5 workflow with the following fixed contracts:

- engine `p6-science-v1` and dispatch policy `6`;
- scientific policy `scientific-policy-v1`;
- observation parser `p6-observation-v1`;
- deterministic renderer `p6-report-v1`.

The workflow stores an immutable semantic snapshot of a verified P5 source
run, including the P5 result/action/binding/execution/job/artifact closure and
the P5 parser-v4 reparse. Its own records contain method context,
thermochemistry context, normal-mode observations, assessments,
comparability decisions, claims, and a report manifest. P6 does not mutate P5
records, call ORCA, use the network, call an LLM, or perform a free-energy
ranking.

Only two internal effects are dispatchable: `internal.p6.assess` and
`internal.p6.render_report`. Effect completion is recorded through the same
verified event/CAS/outbox boundary as the existing kernels. Assessment and
report artifacts are owned by the P6 run, while the manifest enumerates the
source and derived dependencies without including itself, avoiding a
self-hash cycle.

The minimum claim policy is deliberately narrow: supported results require
the closed r²SCAN-3c gas-phase neutral-singlet full-Hessian context and the
exact normal-mode boundary rules from the P6 plan. Electronic-energy claims
use the final single-point electronic total in Eh. Comparison is explicit,
directional, and never described as thermodynamic stability; unknown evidence
is not treated as compatible evidence. Fixture-origin data can be qualified
but cannot be promoted to a supported real-ORCA claim.

## Consequences

P6 can be replayed, verified, exported, and checked in a fresh offline
process. The report is deterministic for the same frozen inputs. A missing,
changed, ambiguous, or tampered source/record/artifact closes verification
instead of being repaired implicitly. Real ORCA evidence remains a separate
owner-controlled gate; offline fixtures and parser replay do not satisfy that
gate.

No SQLite migration v8 is required: P6 uses the existing versioned record and
artifact tables and registers schema 5 contracts in the application layer.
