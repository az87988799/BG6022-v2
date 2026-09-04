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

Allowed `migration_mode` values are `copied`, `adapted`, and `rewritten`.
Each entry must identify the exact legacy commit and blob SHA, list ported
tests, state semantic differences explicitly, and identify whether a real
external or scientific gate is required.
