# Migration manifest

This manifest is the required provenance record for every future legacy-to-V2
migration. V2-P0 performs no code or state migration, so there are no completed
migration entries yet.

## Fixed fields

Every record must contain these fields, without changing their meaning:

`legacy_path` / `legacy_commit` / `legacy_blob_sha` / `target_path` /
`migration_mode` / `behavior_preserved` / `behavior_changed` /
`tests_ported` / `real_gate_required` / `status`

## Records

| legacy_path | legacy_commit | legacy_blob_sha | target_path | migration_mode | behavior_preserved | behavior_changed | tests_ported | real_gate_required | status |
|---|---|---|---|---|---|---|---|---|---|
|`auto_dft1.0/run_orca.py`|`fac93b52e247041d9d5bd4ebaf9dd6d827653928`|`ae54c885bd3c5001236cf1c13fb1b30bb436fb5a`|`src/orca_agent/execution/orca_compiler.py`, `src/orca_agent/execution/orca_parser.py`, `src/orca_agent/execution/local_backend.py`|`rewritten`|Fixed ORCA input/output facts and bounded process observations only|Removed free-form command construction, automatic scientific retry, and legacy state|P5 T08-T11,T20-T23|Yes: Windows ORCA R01-R04|implemented; live gate pending|
|`auto_dft1.0/dft_core/parameters.py`|`fac93b52e247041d9d5bd4ebaf9dd6d827653928`|`7c548eb17bb649ee89a59f4f4dc8699703434ded`|`src/orca_agent/execution/orca_compiler.py`|`rewritten`|Closed method/resource mapping|No arbitrary keywords, basis, D4, paths, shell, or MPI injection|P5 T08-T09|Yes: ORCA compiler compatibility check|implemented; offline verified|
|`auto_dft1.0/dft_core/chemistry.py`|`fac93b52e247041d9d5bd4ebaf9dd6d827653928`|`12366b7bbfa05f4772351bae4ceccefad5b55a34`|`src/orca_agent/identity/geometry.py`|`adapted`|XYZ shape, element, charge, and coordinate validation|Geometry is sourced from confirmed P4 identity and frozen; no global conformer search|P5 T06-T08,T22|No additional external gate for offline; Water live gate applies|implemented; offline verified|
|`auto_dft1.0/dft_core/runtime_monitor.py`|`fac93b52e247041d9d5bd4ebaf9dd6d827653928`|`241e8a4bf0f81eb9a0f8f5f5b9f631cc1dcb022d`|`src/orca_agent/execution/local_runner.py`, `windows_job.py`|`rewritten`|Bounded polling, identity checks, cancellation, and timeout facts|No name-based killing or PID-only authority; no second business worker|P5 T25-T29|Yes: Windows Job Object R03-R04|implemented; live gate pending|
|`auto_dft1.0/dft_core/diagnostics.py`|`fac93b52e247041d9d5bd4ebaf9dd6d827653928`|`a2f9edea55bcefe2303d6545772947bb7b6516eb`|`src/orca_agent/application/p5_errors.py`, `src/orca_agent/execution/orca_parser.py`|`adapted`|Stable typed diagnostics for failed execution and parsing|Legacy diagnostics cannot certify scientific validity or alter methods|P5 T20-T23|Yes: real output parser evidence|implemented; offline verified|
|`auto_dft1.0/dft_core/recovery.py`|`fac93b52e247041d9d5bd4ebaf9dd6d827653928`|`89c75c4a27b04096af6f8d8468cb4500f5fce5ff`|`src/orca_agent/execution/local_backend.py`, `src/orca_agent/infrastructure/p5_records.py`|`rewritten`|Reconciliation and interrupted/unknown semantics|No automatic restart; a new action and grant are required for recomputation|P5 T15-T18,T27-T30|Yes: crash-window substitute tests|implemented; offline verified|
|`auto_dft1.0/dft_core/phase_a_runner.py`|`fac93b52e247041d9d5bd4ebaf9dd6d827653928`|`cf624399e514253c2e0442715b78a17cb36bdb00`|`src/orca_agent/execution/local_runner.py`|`rewritten`|Per-job supervision behavior|Runner cannot plan, approve, mutate business state, or consume outbox|P5 T25-T30|Yes: Windows process-tree tests|implemented; live gate pending|
|`auto_dft1.0/dft_core/resumable_workflow.py`|`fac93b52e247041d9d5bd4ebaf9dd6d827653928`|`400b7b3f24b2e44e04437d2f651faadc7dc3eb69`|`src/orca_agent/application/p5_service.py`, `src/orca_agent/orchestration/p5_replay.py`|`rewritten`|Durable command replay and existing-job reconciliation|No legacy SQLite, scratch, or in-memory workflow state is imported|P5 T12,T17,T18,T30|Yes: real R02 recovery evidence|implemented; live gate pending|

## P6 migration note

P6 is a new derived workflow, not a legacy-code or legacy-state migration.
It stores schema-5 typed records and owned artifacts in the existing V2
versioned tables, while retaining P5 parser-v4 records as the source
authority. No P5 history is rewritten, and no SQLite migration v8 is added.
The P6 report manifest records the exact P5 source and derived artifact
dependencies without creating a manifest self-hash cycle. Any future P6
adaptation from legacy code must add an explicit record above with the fixed
legacy commit and blob SHA.

Allowed `migration_mode` values are `copied`, `adapted`, and `rewritten`.
Each entry must identify the exact legacy commit and blob SHA, list ported
tests, state semantic differences explicitly, and identify whether a real
external or scientific gate is required.
