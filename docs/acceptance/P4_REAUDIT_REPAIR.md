# P4 follow-up repair of 74e809d

Status: PASS. Owner acceptance: ACCEPTED. P5 branch may be created from the
accepted main. Acceptance target: [PR #5](https://github.com/az87988799/BG6022-v2/pull/5).

## Retry source pairing

`verified_attempts()` now reads both committed response envelopes and attempts.
It requires a bijection by run/query/effect/generation/request sequence. The
paired envelope must also equal the exact ID/hash-bearing envelope decoded from
the attempt's actual artifact bytes. Duplicate keys, mismatched owners/queries,
or either orphan record produce `StateIntegrityError` before inspect or claim.

An unrecorded generation is still valid: a crash before source persistence leaves
neither record. No requirement for one attempt per lease generation was added.
No migration, transaction, queue or Worker replacement was introduced.

Permanent regressions in `tests/p4/test_audit_repair.py` include:

- Commit HTTP 429 / Retry-After 10s evidence, crash before completion, remove only
  the attempt, then advance 2s: inspect and claim fail, provider calls and outbox
  generation remain 1.
- Same crash without deletion: inspection succeeds and early claim stays empty.
- Crash before either source record: inspection succeeds and recovery dispatches
  generation 2 normally.
- Duplicate envelope, wrong query, and replacement envelope ID are rejected.
- Existing orphan-attempt and corrupted-artifact regressions remain enabled.

Audit test module: **43 passed**. Final full-suite and remote CI evidence is
recorded in the PR description, which can track the verified commit without
creating self-referential verification commits.

## Additional real PubChem sources

Fetched on 2026-09-06 UTC using the existing live adapter and workflow:

| Query | Checked response | Result |
| --- | --- | --- |
| CID `5950` | L-alanine, returned CID 5950; source and canonical SMILES retain `@`, stereo is defined | awaiting_identity |
| CAS `64-17-5` | ethanol, returned CID 702; CAS uses the complete-name lookup path | awaiting_identity |

The original water source case remains in `P4_AUDIT_LIVE_EVIDENCE.json`.
New [manifest](P4_REAUDIT_LIVE_MANIFEST.json) and
[lossless evidence](P4_REAUDIT_LIVE_EVIDENCE.json) preserve queries, original
response bodies, UTC timestamps, adapter/normalizer versions, candidate bundles,
attempts, envelopes and hashes. New evidence SHA-256:
`ac828a1b48fc8720d01ce9f89400f27f5ee3e9b5f38bc655ae71cc95253c6c99`
(canonical UTF-8 JSON, no final newline).

Reproduction uses `scripts/verify_p4_reaudit_sources.py --allow-network` with a
fresh state root, followed by `scripts/collect_p4_source_evidence.py` for verified
read-only export. No real identity was automatically confirmed; ORCA and LLM
were not run.

## Release record

PR #5 was merged into `main` with merge SHA
`f11216dd0573530de1e565ed5d3defb34e867e53`; the merge contains reviewed
implementation commit `fd72b50e9ed93ff4cd6957c9e3c332ed347f8aa9`. The post-merge
[main CI run 34032683613](https://github.com/az87988799/BG6022-v2/actions/runs/34032683613)
passed all four jobs. Single-maintainer Owner acceptance was recorded as
ACCEPTED on 2026-09-06 UTC. P4 is therefore PASS; P5 starts from a new branch
created from this accepted main.
