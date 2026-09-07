# V2-P6 acceptance record

Status: **IMPLEMENTED — OWNER ACCEPTANCE PENDING**

This record separates technical implementation from the Owner's final
acceptance. The implementation is on branch `codex/v2-p6-science-report`,
based on `0484b04fa70502da7a32a7cde0b0b09a8fdd4826`. The implementation commit
and GitHub push are recorded in the delivery message after they occur. This
file must not be read as an Owner `PASS` or acceptance decision.

## Fixed P6 contracts

| Contract | Value |
|---|---|
| schema | `5` |
| engine | `p6-science-v1` |
| dispatch policy | `6` |
| scientific policy | `scientific-policy-v1` |
| observation parser | `p6-observation-v1` |
| renderer | `p6-report-v1` |
| phases | `assessment_pending` → `report_pending` → `completed` |
| effects | `internal.p6.assess`, `internal.p6.render_report` |
| migration | no migration v8; P5 records remain unchanged |

## Automated and offline evidence

The P6 unit, workflow, report-integrity, CLI, export, and replay coverage is
implemented under `tests/p6/` (38 P6 tests pass). The full local suite passes
with 565 passed, 1 skipped, and 1 warning; branch coverage is 80.45% against
the unchanged 80% gate. Ruff check/format, compileall, `uv lock --check
--offline`, and the wheel/sdist build also pass locally. GitHub CI remains a
separate post-push gate.

The local Water closeout evidence uses:

- P5 source run: `run_3e96adc04ed048f2aefee660908eaab2`;
- P6 run: `run_ed330a658be14a8e968f28aa0f954809`;
- verified P5 source revision: `25`;
- 3 source results (`opt`, `freq`, `sp`), 14 P6 evidence records, and 7 claims;
- final single-point energy: `-76.418938721015 Eh`;
- Hessian dimension `3N=9`, six projected rigid modes, and three real modes:
  `1653.248845317112`, `3813.580815193918`, and `3932.734982130532 cm^-1`;
- stdout thermochemistry context: `298.15 K`, `1.00 atm`, Quasi-RRHO,
  cutoff `1.00 cm^-1`, QRRHO reference `100.0 cm^-1`, symmetry number `2`;
- local report verification and an exported evidence packet re-verification
  both return `valid: true`.

The Hessian's `actual_temperature=0.000000` field is not used as the report
thermochemistry temperature; the stdout thermochemistry block is authoritative.

## Plan coverage

| Area | Status and evidence |
|---|---|
| T01–T06 contracts, immutable source snapshot, parser-v4 closure | Implemented; `src/orca_agent/domain/p6.py`, `src/orca_agent/evidence/p6_ingestion.py`, workflow tests |
| T07 fixture-origin claim boundary | Implemented and tested; fixture data can only produce `qualified` claims |
| T08 SP-only energy and no free-energy ranking | Implemented and tested; energy is final single-point electronic total in Eh |
| T09–T13 minimum prerequisites, projection, layout, and exact frequency thresholds | Implemented and tested in `tests/p6/test_p6_science.py` |
| T14–T16 claim/comparison/thermochemistry semantics | Implemented and tested; stdout thermo context is preserved |
| T17–T19 deterministic reports, manifest closure, tamper fail-closed | Implemented and tested in workflow/report tests |
| T20–T24 cancellation, retries, idempotence, outbox/effect binding | Implemented and covered by workflow tests and the existing kernel regressions |
| T25 malformed/tampered state and replay | Implemented; reducer replay and report verification fail closed |
| T26 P3/P5 regression | Local full suite verified; GitHub CI remains pending |
| T27 lock, quality, compile, build, and CI gates | Local lock, Ruff, compileall, coverage, and build gates verified; GitHub CI remains pending |
| T28 offline evidence export and verification | Implemented; `scripts/export_p6_evidence.py` and `scripts/verify_p6_evidence.py` pass on Water |
| T29 cross-process CLI/restart behavior | Implemented and tested in `tests/p6/test_p6_cli.py` |
| T30 Water/Ethanol release evidence | Water offline reparse packet verified; Ethanol real gate not run and remains pending explicit bounded approval |

## Owner acceptance checklist

The following decisions remain open until the Owner reviews the code, test
results, report, and evidence packet:

- [ ] Owner reviewed the P6 implementation and ADR.
- [ ] Owner reviewed the Water report and exported evidence packet.
- [ ] Owner reviewed the final full-suite, quality, build, and GitHub CI results.
- [ ] Owner accepted P6 and authorized marking this phase complete.

Until all applicable boxes are checked by the Owner, P6 remains
`OWNER ACCEPTANCE PENDING`.
