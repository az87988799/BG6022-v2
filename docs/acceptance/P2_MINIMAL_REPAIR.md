# P2 minimal repair — implementation evidence

Status: IN PROGRESS; independent review and user acceptance remain required.

## Historical reads and the approved v4 correction

Schema 1 is read using its published contract; new commands and completion
inputs retain the narrowed enums and typed receipts. Historical free text is
internal read data, not public diagnostic output. Event payloads and existing
event hashes are not rewritten. Sequence, revision, identity, hash and audit
binding checks still apply.

The user explicitly approved correcting v4's missing f-string interpolation on
2026-09-05. The v4 post-apply identity is now
`p2-dispatch-permits-atomic-completion-v2`; checksum:
`36ad3bff16d21bdaca3f56fb6b545147e6deb008adb67cabea5a6e02e5ed8eab`.
v1–v3 checksums are unchanged. No schema_migrations records are edited and no
trigger removal/restoration workaround is used.

A failed migration rolls back the entire migration transaction. Retry from its
last committed version. Already-upgraded old-v4 development databases require
backup and recreation; they deliberately fail checksum validation. No existing
database was rebuilt by this repair.

Regression databases are generated using normal APIs from fixed main commit
`a001a31b6c0123a24e7e5d89774b0a1799024a27` in a temporary checkout. The original
eight historical cases pass, including both empty-summary cases and rollback
after hash/integer corruption. A fixed test rejects unexpanded SQL placeholders.
Locked offline suite after this repair: 223 passed. This is intermediate evidence,
not the acceptance result for the remaining minimal repairs.

P2 provides at-least-once delivery and database completion fencing, not physical
exactly-once execution or a guarantee of no side effect after cancellation.
Real execution remains outside this repair's scope.
