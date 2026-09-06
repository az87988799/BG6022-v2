# ADR-0006: P5 local ORCA execution boundary

## Status

Accepted for implementation from the P4-accepted `main` baseline
`aeca3efe5853ce58f8003aff7dae169ad1f3cfa7`. This ADR records contracts, not
evidence that a local ORCA installation has already been run.

## Decision

P5 is an independent schema-4 execution run with engine
`p5-local-orca-v1`. It verifies and references a P4 `plan_ready` source run,
but never changes the P4 run, its schema-3 records, or its planning-only
meaning. The execution registry is closed: r2SCAN-3c, SP, Opt, Freq, and the
five named protocol modes are the only supported mappings.

The first geometry is one deterministic RDKit ETKDGv3 embedding with a fixed
seed and canonical XYZ bytes. ORCA input compilation and output parsing are
pure functions. A local gateway is the only component allowed to launch a
backend; fake execution is explicitly marked simulated and cannot be used as
real ORCA evidence.

P5 appends migration v7 for `local_jobs` and uses owner-scoped immutable
artifacts. Migrations v1-v6 remain unchanged. Launch authorization is a
single-use ticket bound to the complete action, geometry, input, budget,
backend, and executable fingerprint. An uncertain launch is reconciled and
never automatically submitted a second time.

P5 emits only technical execution results. `scientific_assessment` remains
`not_evaluated`, and no minimum/global-energy claim is generated; those are
P6 responsibilities.

## Platform boundary

Windows local ORCA 6.1 is the first live target. The default is one core,
2048 MB per job, and bounded wall-time budgets. The runner uses a controlled
process group/Job Object on Windows when real execution is enabled. Linux CI
covers pure logic and controlled substitute-process contracts only; it is not
evidence of Linux ORCA support.

## Explicit non-goals

P5 does not add LLM planning, Slurm, distributed scheduling, automatic
scientific retry, GOAT/xTB search, NEB/TS/IRC, SCF checkpoint continuation, or
an arbitrary XYZ import path.
