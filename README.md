# BG6022-V2

Clean, independently versioned repository for the BG6022 V2 rebuild.

## Current phase: V2-P7 (repair candidate; Owner acceptance pending)

P5 local ORCA implementation has been Owner-accepted and merged to `main`;
the actual post-merge main CI passed. See
[P5 acceptance](P5_ACCEPTANCE.md) and the [limited closeout repair](docs/P5_CLOSEOUT_REPAIR.md)
for the complete verification record and release evidence. P5 is marked
complete. P6 provides an offline derived scientific assessment and
deterministic report over a fixed P5 source snapshot. Review repairs and real
acceptance gates must close before Owner acceptance; P6 is not complete.
See [P6 acceptance](docs/P6_ACCEPTANCE.md) and
[ADR-0007](docs/adr/ADR-0007-p6-offline-science-report.md). Real execution
requires explicit bounded approval; offline fixtures and parser replay do not
replace real acceptance.

P7 adds a guarded natural-language conversation surface, deterministic task
planning, P4/P5/P6 handoffs, immutable delivery records, separately hashed
display views, and one direct terminal launcher. The launcher defaults to the
real DeepSeek → PubChem/RDKit → local ORCA profile; `offline` and
`deepseek_fake` are explicit test profiles. Copy `.env.example` to `.env` and
use [P7 usage](docs/p7-usage.md) for the terminal entry and commands. P7
remains an implementation candidate until the Owner accepts the pushed
commit.

## V2-P0–P3 foundation

V2-P0 established the traceable repository and migration baseline. V2-P1 adds
the reproducible Python toolchain, versioned domain contracts, deterministic
canonical JSON/hash primitives, typed errors, import boundaries, and offline
quality gates. V2-P2 adds the durable reducer kernel: versioned commands and
events, SQLite migrations, atomic snapshots, interrupt projections, revision
CAS, command idempotency, and a recoverable transactional outbox. V2-P3 adds
one bounded, offline Water `water_sp_v1` vertical slice: deterministic planning,
exact approval, persistent FakeBackend execution, evidence/assessment/claim
records, and verifiable Markdown/JSON reports.

P3 does not implement real scientific execution, migrate legacy modules, or
call LLM, PubChem, RDKit, ORCA, Slurm, shell commands, or external network
services. The FakeBackend fixture is explicitly not an ORCA result.

The legacy repository at `E:\BG6022` is a read-only reference. V2 uses a new
database and a new artifact root; legacy active state is never migrated.

## Repository policy

- Source repository: `E:\BG6022`
- V2 repository: `E:\BG6022-v2`
- Default branch: `main`
- Remote: `https://github.com/az87988799/BG6022-v2.git`
- No legacy `auto_dft1.0`, `.tmp`, `.idea`, or SQLite files are copied here.

The migration manifest records the provenance and semantic status of every
future migration. P3 state is configured through an explicit state root; the
legacy repository and its active state remain read-only and are never opened.
All local temporary state is kept below `.tmp/<phase>/`; see
`docs/TEMP_WORKSPACE.md` for the partitioning convention.

## P0 contents

The package under `src/orca_agent` contains P1 domain contracts, the P2
durable kernel, and the bounded P3 fake vertical slice. Runtime execution
against ORCA, external identity services, and real scientific workflows remain
future work packages.

## P2 durable-kernel boundary

- Every accepted command produces one append-only, hash-verified event and one
  revision step.
- Run snapshot, event, interrupt projection, application result, and outbox
  registrations commit atomically.
- Replay verifies event sequence and snapshot hash; corrupted state fails
  closed.
- Outbox delivery is explicitly at-least-once. Lease ownership, retry backoff,
  and dead-letter state are durable and deterministic.
- P3 workers route only the closed fake-pipeline effect registry. Approval is
  required before dispatch, and a persistent execution identity prevents a
  retry from creating a second fake execution.
- Reports are generated from validated durable records and identify their data
  origin as `fake_fixture`; report verification rechecks the stored chain.

## P3 offline commands

Use a new, empty state root for each isolated run. These commands never invoke
ORCA or any external scientific service:

```text
python -m uv run --offline --no-sync python -m orca_agent --state-root <root> start --fixture water_sp_v1 --new-conversation --json
python -m uv run --offline --no-sync python -m orca_agent --state-root <root> approve --run <run_id> ... --json
python -m uv run --offline --no-sync python -m orca_agent --state-root <root> worker --drain --max-effects 20 --json
python -m uv run --offline --no-sync python -m orca_agent --state-root <root> verify-report --run <run_id> --json
```

The reproducible end-to-end check is `scripts/verify_p3.ps1`. It uses real CLI
processes, validates approval gating, cross-process persistence, retry-safe
execution, report verification, and conversation isolation.

`verify-report --run <run_id>` verifies the accepted report and evidence chain.
Add `--report <exported.md or exported.json>` to compare the exported bytes
exactly with that run's artifact. `--report` alone is a parameter error.
Report generation and completion both verify the underlying artifact bytes.

Command retries return the original persisted public result. Use `inspect`
for the current workflow state. Expired approval requests persist the expiry
before returning `interrupt_expired`. A previously accepted submission can
recover the same execution after approval expires. An unknown execution blocks
cancellation; `execution_reconciliation_required` identifies retained execution
facts requiring investigation. Dead-letter work is not automatically resumed.

## P6 offline commands

P6 reads a completed P5 run and never invokes ORCA, the network, or an LLM:

```text
python -m orca_agent --state-root <root> assess --source-run-id <p5_run_id> --profile p6.nonlinear.r2scan3c.v1 --json
python -m orca_agent --state-root <root> worker --workflow p6 --drain --max-effects 20 --json
python -m orca_agent --state-root <root> inspect --workflow p6 --run <p6_run_id> --json
python -m orca_agent --state-root <root> report --run <p6_run_id> --format md --output report.md --json
python -m orca_agent --state-root <root> verify-report --run <p6_run_id> --json
python scripts/export_p6_evidence.py --state-root <root> --run-id <p6_run_id> --output <packet>
python scripts/verify_p6_evidence.py --state-root <root> --run-id <p6_run_id> --evidence-root <packet>
```

The final P6 status is not complete until the Owner accepts the implementation
and its evidence.

## Acceptance governance

This is a single-maintainer repository. The Owner may perform the final
acceptance; independent GitHub review is explicitly waived and no synthetic
Approval is claimed. Technical verification, CI, merge state, and Owner
acceptance remain separate records, and P3 is not marked final `PASS` until the
Owner accepts it.
