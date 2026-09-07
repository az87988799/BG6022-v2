# Closeout evidence (not phase acceptance)

Implementation: `880869f` (parser v4, pending-tree repair and frozen geometry-byte repair;
the final commit also preserves raw Hessian evidence attributes).

`frozen-freq-replay.json` is a **new offline** replay of the unchanged
`../p5-freq-blocker` files, created by `scripts/replay_p5_frozen_freq.py`.
All seven original Freq file hashes/sizes and twelve original R03/R04 control
file hashes/sizes were checked. Parsing now succeeds with 9x9 Hessian and nine
raw frequencies. The recorded control receipts still classify as PASS.
This starts zero ORCA tasks, leaves the old rejected run unchanged, and does
not constitute a new R01/R02 PASS or a scientific minimum assessment.

## New Water chain — Opt, Freq and SP completed; Owner acceptance pending

Local state: `E:\BG6022-v2\.tmp\p5-water-closeout`.
Preview: `water-gate-preview.json` in that state root.
Run: `run_3e96adc04ed048f2aefee660908eaab2`.
Executable: `E:\orca\orca.exe`, version 6.1.1, freshly probed SHA-256
`8d6b51bf4093c967dbed997cc651f0212b8f94313ee77ea56f548f000672c42f`.
Water, `p5.opt_freq_sp.r2scan3c.v1`, 1 core / 2048 MB, maximum three new tasks,
Opt/Freq/SP ceilings 900/1800/300 seconds (total 3000), no automatic retry.

The initial Opt was approved and executed once:

- Action: `action_40c8562e48a047de986c73d1fd9e0853`.
- Action hash: `822d6e1106b2f4d3fe44422dc444454bea1a43d88bd8765e89ec675985e8aa6e`.
- Binding hash: `7243bdfacaacc5667167967d1ea3085e0f6956afd37488cae5fa9d766a8026b1`.
- Budget hash: `62d1ae957f985585df64acc86f0e2b54292c15976277ba4d66149b8946be6ef1`.
- [Opt evidence packet](r01-opt/README.md): normal exit, one physical start,
  complete parse, and no downstream job.

The Freq action was then separately approved and executed once:

- Action: `action_9039ee63cd9647ba840b1534b26b4c20`.
- Execution: `execution_476b19928f774dd69674b613b23bd4d4`.
- [Freq evidence packet](r01-freq/README.md): normal exit, one physical
  start, complete parse, 9x9 Hessian and nine raw frequencies.

The terminal SP action was then separately approved and executed once:

- Action: `action_782d990dee74468da38bbe7cacfe4bc9`.
- Execution: `execution_b2545f5127a94985886ef48539946834`.
- [SP evidence packet](r01-sp/README.md): normal exit, one physical start,
  complete parse, SCF converged, and finite energy `-76.418938721035 Eh`.

The first post-exit collection attempt exposed a geometry-byte preservation
bug while preparing SP. The minimal repair was committed as `7c68e6a`; the
existing Freq receipt was collected afterward without a relaunch or automatic
retry. Both Opt and Freq approval/reconcile commands were replayed idempotently;
each physical start count remained one and no duplicate execution was created.
The SP action used the exact frozen optimized geometry bytes and had its own
approval grant. The saved SP approval was replayed in a fresh CLI process and
returned the original event; the final state has three jobs and three complete
results with no duplicate physical execution. Earlier bounded authorizations
were not reused for this action. The technical R01/R02 evidence is complete,
but no merge, P5 acceptance or Owner acceptance is inferred.
