# V2-P6 review repair and acceptance record

Status: **REVIEW REPAIRS — REAL ACCEPTANCE GATES PENDING — NOT ACCEPTED**

This supersedes the premature `IMPLEMENTED — OWNER ACCEPTANCE PENDING`
description at `a2a31a4`. Technical closure is not merely an Owner signature.
P5 remains accepted; its records, parser v4 and historical fixture hashes are
not migrated. P6 is on `codex/v2-p6-science-report`. No merge, P6 PASS or P7
start is authorized by this record.

## Review repairs

- F1: preparation is separate from publication. Dispatch lease, source and
  revision are checked in the transaction publishing scientific records,
  artifact metadata, event, CAS, receipt and next effect. Expiry rolls back;
  expected publication failures use a savepoint and retry. Process failure
  leaves no committed business rows. Cancellation fences P6 internal effects
  only; completed reports cannot be ordinarily cancelled.
- F2: every stable Evidence field is compared with fresh parsing, including
  value/token/unit/locator. Assessment and comparison semantics are rebuilt,
  including referenced P6 sources. Parser/result/real binding versions agree.
- F3: claim type/quantity/unit/cardinality/ordered subjects/support status are
  constrained. Each comparison side must have valid integrity; isotope policy
  is checked.
- F4: both mandatory manifests and the exact file closure are verified.
  `archived_packet` restores a disposable ledger from fixed-table typed rows
  and artifact bytes, without executing packet SQL or accessing original state.
  Explicit P5/P4 identity owners and comparison references are included.
  This verifies supplied archive consistency, not third-party authenticity of
  the original machine.
- F5: external Opt IDs/hashes retain original ownership and verify identity,
  method and XYZ relationships. Archived rejected executions with matching
  terminal result receipts produce diagnostics and no scientific claims.
  Missing mode matrices are scientifically unsupported, not fabricated.
  New complete-mode Water/Ethanol test backends preserve historical fixtures.
  Cancellation without any archived result/receipt is deliberately rejected as
  an incomplete source, not presented as verified diagnostics.
- Ethanol-4core-v1 is a newly registered `p5.opt_freq_sp.r2scan3c.4core.v1`
  protocol. It keeps the historical single-core protocol hashes unchanged and
  binds four ORCA processes, 8192 MB total memory, `%maxcore 1536`, fixed
  one-thread environment variables, and the existing supervisor/Job Object.

## Actual Water material — zero new ORCA calculations

- P5 source: `run_3e96adc04ed048f2aefee660908eaab2`, revision 25.
- New P6: `run_963622492d6c4e2fa3756367c6fd6a9a`.
- [Report](evidence/p6-water/packet/report.md),
  [JSON](evidence/p6-water/packet/report.json),
  [packet manifest](evidence/p6-water/packet/packet_manifest.json),
  [fresh-process evidence](evidence/p6-water/restart-evidence.json).
- Separate Python processes assessed, resumed each effect, verified, retried
  the idle worker and verified again. Source ledger before/after fingerprints
  match; new P5 starts: **0**.
- Independent archived verification succeeded. Exact energy and signed Hessian
  tokens are in the report, with Claim → Evidence → artifact hash/locator
  mappings. Final SP energy: `-76.418938721015 Eh`.

Reproduce without the original database:

```powershell
python scripts/verify_p6_evidence.py --mode archived_packet --run-id run_963622492d6c4e2fa3756367c6fd6a9a --evidence-root docs/evidence/p6-water/packet
```

## Original P6-plan-v1 test mapping

Names refer to `tests/p6/`: R = review_regressions, S = science,
O = observations, W = workflow, C = CLI. This preserves the original numbering;
implementation checks are not a claim that every acceptance scenario was run.

| Plan ID | Original requirement and current evidence |
|---|---|
| T01 | Strict contracts/hash/version: S strict policy roundtrip; shared contract tests |
| T02 | P5 unchanged: W immutable source; Water ledger fingerprints |
| T03 | Per-action result/binding closure: exact ingestion bindings; R full three-node chain |
| T04 | External Opt: R original owner; ingestion hash/identity/method/XYZ/cycle rejection |
| T05 | Raw hash/size/path/owner: ArtifactStore; W tamper; packet file closure |
| T06 | Persisted values vs reparse: R valid-hash -999 producer injection blocks report |
| T07 | Real/fake origin: S fixture qualification; R qualified complete-mode chain |
| T08 | SP-only: W derived SP report, no thermodynamic stability inference |
| T09 | Opt XYZ → Freq → Hessian: real Water reparse and R full chains |
| T10 | Water 9/3, Ethanol 27/21: R full-mode fixtures; real Water packet |
| T11 | Six projected/extra zero/malformed columns: S layout and O matrix tests |
| T12 | Linear/partial/unknown layout: S unsupported; R historical missing modes |
| T13 | Negative/inconclusive/positive: S minimum policy matrix |
| T14 | Exact -20/+1/50 boundaries: S boundary matrix; O signed tokens |
| T15 | Signed frequencies: S fixed projection; O token tests |
| T16 | Stdout thermo vs Hessian/missing values: O thermo tests; real Water |
| T17 | Electronic energy is not Gibbs: R Gibbs quantity mutation rejected |
| T18 | Comparison context/direction/isotope/integrity: S comparison; R invalid/isotope |
| T19 | Unknown is not compatible: S unknown context; identity comparison dimensions |
| T20 | Claim subject/unit/quantity/value/formula: S binding; R rehashed mutations |
| T21 | Claim → Evidence → locator/policy: report mappings; assessment reconstruction |
| T22 | Deterministic/replay/no duplicates: W/C replay; R two-generation race |
| T23 | Crash/lease/two workers: R publication rollback, expiry and interleaved workers |
| T24 | Cancel/late completion/P5 unchanged: R both internal stages; W isolation |
| T25 | Manifest/raw/report/dependency tamper: W tamper; R missing/forged manifests |
| T26 | Old routing/version handling: shared schema/kernel/P3 regressions |
| T27 | Core/no extras/CLI/build/lock: separate quality/build gates; P6 has no runner |
| T28 | Clean checkout actual bytes: committed Water archive; checkout gate below |
| T29 | Actual processes/no P5 starts: restart-evidence.json; R subprocess |
| T30 | Scientific unsupported vs integrity error: R missing modes/failed receipt vs -999 |

## Remaining release gates

| Gate | Status |
|---|---|
| R6-01 Water | New packet independently verified; zero new P5 starts |
| R6-02 Ethanol real chain | **NOT RUN**; four-core bounded preview prepared; current memory preflight is 5240 MB < 8192 MB, so startup is blocked until the host is suitable or the owner explicitly changes the fixed plan |
| R6-03 restart | Water fresh-process record; unchanged source fingerprint |
| R6-04 negative checks | Review tests; final results below |
| R6-05 clean archives | Water clean checkout verified at 1b1246d; Ethanol four-core archive absent |
| Corresponding SHA CI | [PR #7 checks](https://github.com/az87988799/BG6022-v2/pull/7/checks); actual head SHA must pass |
| Owner acceptance/merge/main CI | Not performed; Owner controls acceptance |

Ethanol [four-core preview](evidence/p6-ethanol-4core-preview-v2.json): one
deterministic local `CCO` neutral singlet, gas-phase r2SCAN-3c, ORCA 6.1.1,
4 cores, 8192 MB total memory, `%maxcore 1536`, and a fixed one-thread
environment. Opt/Freq/SP limits are 900/1800/300 seconds, total 3000 seconds,
with at most one concurrent ORCA task and three physical starts. The existing
P5 protocol run budget remains 3600 seconds metadata; it does not expand the
node ceiling. The preview records `mpiexec` 10.1.12498.18 and 31 ORCA MPI
modules, but its measured available physical memory was 5240 MB, below the
required 8192 MB. No execution approval or calculation was submitted, and no
budget was silently changed.

The historical [single-core preview](evidence/p6-ethanol-preview.json) remains
unchanged and is not reused as four-core authorization.

## Final verification log

- P6 suite after the transaction/source/archive repairs: **56 passed**.
  Two additional semantic/version regressions were then added and tested.
- Four-core resource, compiler, runtime-hash, fixed-budget, worker handoff and
  controlled Windows child/Job Object tests: **9 passed**. The controlled test
  uses four child processes as a lifecycle surrogate; it is not an MPI or
  quantum-chemistry calculation.
- Full Windows coverage run: **582 passed, 1 failed, 1 skipped, 31 warnings**;
  branch coverage **80.98%**, exceeding the unchanged 80% threshold.
  The failure was P5 `test_deadline_stops_the_controlled_process_tree`:
  its two-second launch returned `launch_state_unknown` under coverage/load.
  Its isolated rerun passed. This does not turn the failed full run green;
  the final GitHub matrix remains a separate required gate.
- Ruff check/format (222 files), compileall, diff whitespace, offline lock
  check and wheel/sdist build passed. The wheel imports and P6 CLI help passed
  in a separate core-only environment without NumPy, RDKit or httpx.
  Its first offline dependency installation lacked a cached Pydantic wheel;
  dependency setup was retried online, not hidden as an offline setup pass.
- Clean detached checkout at `1b1246d2fd2b378f79c09fe901ff39d13d0a4cde`:
  the imported package path was explicitly the checkout's `src/orca_agent`,
  the working tree was clean, and archived Water verification returned true
  for manifests, typed records, raw artifacts, reports and restored ledger.
  No original `--state-root` was provided.
- [PR #7](https://github.com/az87988799/BG6022-v2/pull/7) triggers the existing
  Ubuntu 3.11/3.14, Windows 3.14 and quality jobs. Its actual head-SHA checks
  are authoritative; a pending or failed check is not a passing gate.
  Old main CI and the old 565/80.45% figure are not repair evidence.
