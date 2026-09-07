# Reproducible R03/R04 control evidence

Executed on Windows with implementation `97a8fc2` (C1–C3 source from `8d89f58`).
Both cases were explicitly authorized before execution. No automatic retry.
ORCA `E:\orca\orca.exe`, full version 6.1.1, executable SHA-256 in manifest.
Water, one core, total Job Object memory cap 2048 MB.

| Gate | Scope | Execution | Result |
|---|---|---|---|
| R03 | Opt, 30-second ceiling; cancel after physical launch | execution_4a48d20f857a4c139eb11ceec39afc78 | PASS |
| R04 | Opt, 3-second ceiling; actual deadline stop | execution_d53e26483281415aa60e34002a3b18be | PASS |

Each subdirectory contains exact input, geometry, partial stdout/stderr, terminal
receipt and read-only collected evidence. SHA-256 and sizes are in `manifest.json`.
The receipt records matching process creation identity, pre-request liveness,
successful OS termination request, matching dedicated termination exit code,
confirmation time and empty Windows Job Object. These are control gates only,
not successful scientific calculations. P5 results for both are rejected with
cancelled/timed-out diagnostics; `scientific_assessment=not_evaluated`.

The collector includes both classifiers for transparency: R03's timeout classifier
is FAIL and R04's cancellation classifier is FAIL because they are different
requested controls; only the intended gate column is the acceptance result.
Old R03 lacked these facts and remains NOT_EXERCISED under the new rule. This
packet does not change the failed R01/R02 chain or imply P5 owner acceptance.

These files can be replayed with `control_gate_status` without starting ORCA.
