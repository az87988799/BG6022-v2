# Water R01 Freq evidence

This packet records the separately approved Freq action of the new Water
R01/R02 chain. The terminal SP action is preserved in its own packet; this
packet contains only the Freq action's evidence.

- Run: `run_3e96adc04ed048f2aefee660908eaab2`
- Execution: `execution_476b19928f774dd69674b613b23bd4d4`
- Action: `action_9039ee63cd9647ba840b1534b26b4c20`
- ORCA: `E:\orca\orca.exe`, 6.1.1, SHA-256
  `8d6b51bf4093c967dbed997cc651f0212b8f94313ee77ea56f548f000672c42f`
- Budget: 1 core / 2048 MB / 1800 seconds; no automatic retry.
- Result: normal exit 0, physical start count 1, parser `complete`, SCF
  converged, 9x9 Hessian, and frequencies
  `0, 0, 0, 0, 0, 0, 1653.25, 3813.58, 3932.73 cm^-1`.

The packet preserves the exact Freq input, frozen optimized geometry bytes,
ORCA stdout/stderr, Hessian, receipt, execution export and read-only collector
output. `manifest.json` records byte hashes and sizes. The collector verified
the P5 database/event/artifact chain with `valid=true`.

The first post-exit collection attempt was deliberately not treated as a
result: it exposed a byte-preservation bug while preparing the downstream SP
action and returned `unsupported_execution_profile`. The source repair in
`repair-recovery.json` only reuses the current action's already-bound geometry
artifact bytes, while retaining fail-closed hash checks. The existing Freq
receipt was then collected successfully; no Freq relaunch and no automatic
retry occurred.

Approval and reconcile requests were replayed in fresh CLI processes. The
original accepted events were returned, physical start count remained 1, and
no duplicate execution was created. SP was later separately approved and
completed once; see [SP evidence](../r01-sp/README.md). Scientific assessment
remains `not_evaluated`; no minimum-energy or scientific claim is made.
