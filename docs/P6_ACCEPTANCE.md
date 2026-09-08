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
- A: energy-difference Claims now require the revalidated candidate/reference
  comparison, selected reference assessment, ordered evidence hashes and
  ordered source result IDs at construction, publication and report verify.
- C: a trusted terminal local-ORCA receipt is reconciled before the new-start
  memory gate; an untrusted receipt or prior launch evidence remains an
  unknown state and cannot trigger a second start.
- The single active Ethanol four-core protocol is
  `p5.opt_freq_sp.r2scan3c.4core.v2`. It keeps the historical single-core
  protocol hashes unchanged and binds four ORCA processes, 4096 MB total
  memory, `%maxcore 768`, fixed one-thread environment variables, and the
  existing supervisor/Job Object. The retired `.4core.v1` registration is not
  an active execution entry point.

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
  mappings. The Opt last-step energy is `-76.418938721015 Eh`; the independent
  final SP energy is `-76.418938721035 Eh`.

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
| R6-02 Ethanol real chain | **NOT RUN**; replacement four-core preview passed fresh 4096 MB memory and MPI preflight, but no node has been approved or started |
| R6-03 restart | Water fresh-process record; unchanged source fingerprint |
| R6-04 negative checks | Review tests; final results below |
| R6-05 clean archives | Water clean checkout verified at 1b1246d; Ethanol four-core archive absent |
| Corresponding SHA CI | [Run 34221187405](https://github.com/az87988799/BG6022-v2/actions/runs/34221187405) for `833eb827b6d9a74a8e7edc7334b7ef0aded9b719`; quality, Ubuntu 3.11/3.14 and Windows 3.14 all passed |
| Owner acceptance/merge/main CI | Not performed; Owner controls acceptance |

The prior four-core previews were inspected and cancelled because they used the
retired budget; both had no job or execution. The replacement [Ethanol
preview](evidence/p6-ethanol-4core-4096mb-preview.json) uses one deterministic
local `CCO` neutral singlet, gas-phase r2SCAN-3c, ORCA 6.1.1, 4 cores, 4096 MB
total memory, `%maxcore 768`, and a fixed one-thread environment. Its run is
`run_4f5a953aba92448bbccc70ee66ca728b`; preview hash is
`b7af08f341ae21215cf9c5a82cb519ab148632348db2b10897ba796b95726d38`.
Opt/Freq/SP limits are 900/1800/300 seconds, total 3000 seconds, with at most
one concurrent ORCA task and three physical starts. The fresh preflight
observed 4234 MB available physical memory and passed the 4096 MB gate; the
MPI rank smoke passed with MS-MPI 10.1.12498.18 and 31 ORCA MPI modules. The
P5 protocol run budget remains 3600 seconds metadata. Preview generation did
not approve or start a calculation (`new_orca_calculation_starts=0`).

The historical [single-core preview](evidence/p6-ethanol-preview.json) remains
unchanged and is not reused as four-core authorization.

## Final verification log

- Current Windows P5/P6 regression suite: **186 passed, 1 skipped** in the
  second complete run. A first complete run had one load-sensitive failure in
  P5 `test_deadline_stops_the_controlled_process_tree`; its isolated rerun and
  the complete rerun passed. The controlled test uses child processes as a
  lifecycle surrogate; it is not an MPI or quantum-chemistry calculation.
- Four-core resource, compiler, runtime-hash, fixed-budget, worker handoff,
  low-memory receipt replay and controlled Windows child/Job Object coverage
  are included in that current P5/P6 result.
- Ruff check, format check (**223 files**), compileall and diff whitespace all
  passed. GitHub Actions run [34221187405](https://github.com/az87988799/BG6022-v2/actions/runs/34221187405)
  passed quality, package build, Ubuntu 3.11/3.14 tests and the Ubuntu 3.14
  branch-coverage gate, plus Windows 3.14 tests.
- Clean detached checkout at `1b1246d2fd2b378f79c09fe901ff39d13d0a4cde`:
  the imported package path was explicitly the checkout's `src/orca_agent`,
  the working tree was clean, and archived Water verification returned true
  for manifests, typed records, raw artifacts, reports and restored ledger.
  No original `--state-root` was provided.
- [PR #7](https://github.com/az87988799/BG6022-v2/pull/7) triggers the existing
  Ubuntu 3.11/3.14, Windows 3.14 and quality jobs. Its actual head-SHA checks
  are authoritative; a pending or failed check is not a passing gate.
  Old main CI and the old 565/80.45% figure are not repair evidence.
