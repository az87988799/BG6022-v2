# Water R01 Opt evidence

This is the first, separately approved action of the new R01/R02 chain. The
complete three-action chain is recorded in the closeout index; this packet
contains only the Opt action's evidence.

- Run: `run_3e96adc04ed048f2aefee660908eaab2`
- Execution: `execution_e91e54c4b2a84d6a809dbc824cf9e8f1`
- Action: `action_40c8562e48a047de986c73d1fd9e0853`
- ORCA: `E:\orca\orca.exe`, 6.1.1, SHA-256
  `8d6b51bf4093c967dbed997cc651f0212b8f94313ee77ea56f548f000672c42f`
- Budget: 1 core / 2048 MB / 900 seconds; no automatic retry.
- Result: normal exit 0, physical start count 1, parser `complete`, Opt
  converged, energy `-76.418938721015 Eh`.

The packet preserves the exact input, initial geometry, ORCA stdout/stderr,
final optimized XYZ, receipt, execution export and collector output. The
manifest records byte hashes. The P5 collector independently verified the
database/event/artifact chain with `valid=true`.

The approval and reconcile requests were replayed in fresh CLI processes. Both
replays returned the original accepted event and revision; physical start count
remained 1, no new execution or downstream job was created, and results did not
change. This is command idempotency/restart evidence, not a second computation.

The Freq and SP actions were separately approved later and are preserved in
their own packets. No Freq or SP job is represented in this packet. Scientific
assessment remains `not_evaluated`; no minimum-energy or scientific claim is
made.
