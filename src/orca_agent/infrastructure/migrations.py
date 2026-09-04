"""Small stdlib-only, checksum-verified SQLite migration runner."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from orca_agent.application.errors import (
    MigrationDriftError,
    MigrationVersionError,
    StateIntegrityError,
    StorageBusyError,
)
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.errors import DomainError
from orca_agent.domain.hashing import (
    GENESIS_EVENT_HASH,
    effect_spec_hash,
    event_envelope_hash,
    sha256_hex,
    verify_sha256,
)
from orca_agent.domain.ids import EffectId, EventId, RunId, effect_id_for
from orca_agent.domain.json_types import freeze_json_object, thaw_json
from orca_agent.domain.versions import CURRENT_SCHEMA_VERSION
from orca_agent.orchestration.effects import EffectClass
from orca_agent.orchestration.events import EventType, KernelEvent
from orca_agent.orchestration.versions import ENGINE_VERSION

from .clock import Clock, SystemClock, format_utc
from .sqlite import begin_immediate

SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version             INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    checksum            TEXT NOT NULL,
    applied_at_utc      TEXT NOT NULL
)
""".strip()


def migration_checksum(
    version: int,
    name: str,
    statements: Sequence[str],
    *,
    post_apply_id: str = "",
) -> str:
    """Calculate a stable checksum over migration identity and exact SQL text."""

    value = {"version": version, "name": name, "statements": list(statements)}
    if post_apply_id:
        value["post_apply_id"] = post_apply_id
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    checksum: str = ""
    post_apply: Callable[[sqlite3.Connection], None] | None = None
    post_apply_id: str = ""

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("migration version must be positive")
        if not self.name.strip():
            raise ValueError("migration name must not be blank")
        if not self.statements:
            raise ValueError("migration must contain at least one statement")
        if self.post_apply is not None and self.version >= 3 and not self.post_apply_id.strip():
            raise ValueError("post_apply_id is required for a post-apply migration")
        if not self.checksum:
            object.__setattr__(
                self,
                "checksum",
                migration_checksum(
                    self.version,
                    self.name,
                    self.statements,
                    post_apply_id=self.post_apply_id,
                ),
            )


INITIAL_SCHEMA_STATEMENTS = (
    SCHEMA_MIGRATIONS_SQL,
    """
    CREATE TABLE runs (
        run_id              TEXT PRIMARY KEY,
        schema_version      INTEGER NOT NULL,
        engine_version      TEXT NOT NULL,
        revision            INTEGER NOT NULL CHECK (revision >= 1),
        status              TEXT NOT NULL CHECK (
            status IN ('created', 'waiting_for_input', 'ready', 'cancelled', 'failed')
        ),
        state_json          TEXT NOT NULL,
        state_hash          TEXT NOT NULL,
        last_event_id       TEXT NOT NULL,
        created_at_utc      TEXT NOT NULL,
        updated_at_utc      TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE events (
        event_id            TEXT PRIMARY KEY,
        command_id          TEXT NOT NULL UNIQUE,
        command_type        TEXT NOT NULL,
        command_hash        TEXT NOT NULL,
        run_id              TEXT NOT NULL REFERENCES runs(run_id),
        sequence_no         INTEGER NOT NULL CHECK (sequence_no >= 1),
        expected_revision   INTEGER NOT NULL CHECK (expected_revision >= 0),
        new_revision        INTEGER NOT NULL,
        event_type          TEXT NOT NULL,
        schema_version      INTEGER NOT NULL,
        engine_version      TEXT NOT NULL,
        payload_json        TEXT NOT NULL,
        payload_hash        TEXT NOT NULL,
        result_json         TEXT NOT NULL,
        result_hash         TEXT NOT NULL,
        occurred_at_utc     TEXT NOT NULL,
        recorded_at_utc     TEXT NOT NULL,
        UNIQUE (run_id, sequence_no),
        CHECK (new_revision = expected_revision + 1)
    )
    """.strip(),
    "CREATE INDEX events_run_sequence ON events(run_id, sequence_no)",
    """
    CREATE TABLE interrupts (
        interrupt_id        TEXT PRIMARY KEY,
        run_id              TEXT NOT NULL REFERENCES runs(run_id),
        kind                TEXT NOT NULL,
        status              TEXT NOT NULL CHECK (
            status IN ('pending', 'resolved', 'expired', 'superseded', 'cancelled')
        ),
        schema_version      INTEGER NOT NULL,
        engine_version      TEXT NOT NULL,
        request_event_id    TEXT NOT NULL REFERENCES events(event_id),
        terminal_event_id   TEXT REFERENCES events(event_id),
        payload_json        TEXT NOT NULL,
        payload_hash        TEXT NOT NULL,
        response_json       TEXT,
        response_hash       TEXT,
        created_at_utc      TEXT NOT NULL,
        expires_at_utc      TEXT NOT NULL,
        terminal_at_utc     TEXT,
        superseded_by       TEXT REFERENCES interrupts(interrupt_id)
                            DEFERRABLE INITIALLY DEFERRED,
        CHECK (
            (status = 'pending' AND terminal_event_id IS NULL AND terminal_at_utc IS NULL)
            OR
            (status <> 'pending' AND terminal_event_id IS NOT NULL AND terminal_at_utc IS NOT NULL)
        ),
        CHECK (
            (response_json IS NULL AND response_hash IS NULL)
            OR
            (response_json IS NOT NULL AND response_hash IS NOT NULL)
        )
    )
    """.strip(),
    (
        "CREATE UNIQUE INDEX one_pending_interrupt_per_run ON interrupts(run_id) "
        "WHERE status = 'pending'"
    ),
    "CREATE INDEX interrupts_due ON interrupts(status, expires_at_utc)",
    """
    CREATE TABLE outbox (
        effect_id            TEXT PRIMARY KEY,
        run_id               TEXT NOT NULL REFERENCES runs(run_id),
        source_event_id      TEXT NOT NULL REFERENCES events(event_id),
        effect_index         INTEGER NOT NULL CHECK (effect_index >= 0),
        effect_type          TEXT NOT NULL,
        effect_class         TEXT NOT NULL CHECK (effect_class IN ('internal', 'external')),
        schema_version       INTEGER NOT NULL,
        engine_version       TEXT NOT NULL,
        payload_json         TEXT NOT NULL,
        payload_hash         TEXT NOT NULL,
        status                TEXT NOT NULL CHECK (
            status IN ('pending', 'leased', 'succeeded', 'dead_letter')
        ),
        attempt_count        INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        available_at_utc     TEXT NOT NULL,
        lease_owner          TEXT,
        lease_expires_at_utc TEXT,
        completed_at_utc     TEXT,
        last_error_code      TEXT,
        last_error_message   TEXT,
        created_at_utc       TEXT NOT NULL,
        updated_at_utc       TEXT NOT NULL,
        UNIQUE (source_event_id, effect_index),
        CHECK (
            (status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at_utc IS NOT NULL)
            OR
            (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at_utc IS NULL)
        ),
        CHECK (
            (status IN ('succeeded', 'dead_letter') AND completed_at_utc IS NOT NULL)
            OR
            (status IN ('pending', 'leased') AND completed_at_utc IS NULL)
        )
    )
    """.strip(),
    "CREATE INDEX outbox_due ON outbox(status, available_at_utc, created_at_utc, effect_id)",
    "CREATE INDEX outbox_lease_expiry ON outbox(status, lease_expires_at_utc)",
)


LEGACY_COMPLETION_WORKER_ID = "worker_ffffffffffffffffffffffffffffffff"


def _backfill_effect_spec_hashes(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT effect_id, run_id, source_event_id, effect_index, effect_type, "
        "effect_class, schema_version, engine_version, payload_json, payload_hash "
        "FROM outbox"
    ).fetchall()
    for row in rows:
        try:
            effect_id = EffectId(str(row[0]))
            run_id = RunId(str(row[1]))
            source_event_id = EventId(str(row[2]))
            effect_index = _migration_int(row[3], what="outbox effect_index", minimum=0)
            effect_type = str(row[4])
            effect_class = EffectClass(str(row[5]))
            schema_version = _migration_int(row[6], what="outbox schema_version", minimum=1)
            engine_version = str(row[7])
            payload = freeze_json_object(json.loads(str(row[8])))
            payload_hash = str(row[9])
            if schema_version != CURRENT_SCHEMA_VERSION or engine_version != ENGINE_VERSION:
                raise StateIntegrityError("legacy outbox version is unsupported")
            if effect_id != effect_id_for(source_event_id, effect_index):
                raise StateIntegrityError("legacy outbox effect ID is not deterministic")
            verify_sha256(payload, payload_hash)
            spec_hash = effect_spec_hash(
                effect_id=str(effect_id),
                run_id=str(run_id),
                source_event_id=str(source_event_id),
                effect_index=effect_index,
                effect_type=effect_type,
                effect_class=effect_class.value,
                schema_version=schema_version,
                engine_version=engine_version,
                payload=payload,
                payload_hash=payload_hash,
            )
        except (DomainError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise StateIntegrityError("legacy outbox payload is invalid") from error
        connection.execute(
            "UPDATE outbox SET spec_hash = ? WHERE effect_id = ?",
            (
                spec_hash,
                str(effect_id),
            ),
        )


V2_HARDENING_STATEMENTS = (
    "CREATE UNIQUE INDEX events_run_event_unique ON events(run_id, event_id)",
    "DROP INDEX IF EXISTS one_pending_interrupt_per_run",
    "DROP INDEX IF EXISTS interrupts_due",
    "ALTER TABLE interrupts RENAME TO interrupts_v1",
    """
    CREATE TABLE interrupts (
        interrupt_id        TEXT PRIMARY KEY,
        run_id              TEXT NOT NULL REFERENCES runs(run_id),
        kind                TEXT NOT NULL,
        status              TEXT NOT NULL CHECK (
            status IN ('pending', 'resolved', 'expired', 'superseded', 'cancelled')
        ),
        schema_version      INTEGER NOT NULL,
        engine_version      TEXT NOT NULL,
        request_event_id    TEXT NOT NULL,
        terminal_event_id   TEXT,
        payload_json        TEXT NOT NULL,
        payload_hash        TEXT NOT NULL,
        response_json       TEXT,
        response_hash       TEXT,
        created_at_utc      TEXT NOT NULL,
        expires_at_utc      TEXT NOT NULL,
        terminal_at_utc     TEXT,
        superseded_by       TEXT,
        UNIQUE (run_id, interrupt_id),
        FOREIGN KEY (run_id, request_event_id)
            REFERENCES events(run_id, event_id),
        FOREIGN KEY (run_id, terminal_event_id)
            REFERENCES events(run_id, event_id),
        FOREIGN KEY (run_id, superseded_by)
            REFERENCES interrupts(run_id, interrupt_id)
            DEFERRABLE INITIALLY DEFERRED,
        CHECK (
            (status = 'pending' AND terminal_event_id IS NULL AND terminal_at_utc IS NULL)
            OR
            (status <> 'pending' AND terminal_event_id IS NOT NULL AND terminal_at_utc IS NOT NULL)
        ),
        CHECK (
            (response_json IS NULL AND response_hash IS NULL)
            OR
            (response_json IS NOT NULL AND response_hash IS NOT NULL)
        )
    )
    """.strip(),
    """
    INSERT INTO interrupts(
        interrupt_id, run_id, kind, status, schema_version, engine_version,
        request_event_id, terminal_event_id, payload_json, payload_hash,
        response_json, response_hash, created_at_utc, expires_at_utc,
        terminal_at_utc, superseded_by
    )
    SELECT
        interrupt_id, run_id, kind, status, schema_version, engine_version,
        request_event_id, terminal_event_id, payload_json, payload_hash,
        response_json, response_hash, created_at_utc, expires_at_utc,
        terminal_at_utc, superseded_by
    FROM interrupts_v1
    """.strip(),
    "DROP TABLE interrupts_v1",
    (
        "CREATE UNIQUE INDEX one_pending_interrupt_per_run ON interrupts(run_id) "
        "WHERE status = 'pending'"
    ),
    "CREATE INDEX interrupts_due ON interrupts(status, expires_at_utc)",
    "DROP INDEX IF EXISTS outbox_due",
    "DROP INDEX IF EXISTS outbox_lease_expiry",
    "ALTER TABLE outbox RENAME TO outbox_v1",
    """
    CREATE TABLE outbox (
        effect_id            TEXT PRIMARY KEY,
        run_id               TEXT NOT NULL REFERENCES runs(run_id),
        source_event_id      TEXT NOT NULL,
        effect_index         INTEGER NOT NULL CHECK (effect_index >= 0),
        effect_type          TEXT NOT NULL,
        effect_class         TEXT NOT NULL CHECK (effect_class IN ('internal', 'external')),
        schema_version       INTEGER NOT NULL,
        engine_version       TEXT NOT NULL,
        payload_json         TEXT NOT NULL,
        payload_hash         TEXT NOT NULL,
        spec_hash            TEXT NOT NULL,
        status                TEXT NOT NULL CHECK (
            status IN ('pending', 'leased', 'succeeded', 'dead_letter')
        ),
        attempt_count        INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        available_at_utc     TEXT NOT NULL,
        lease_owner          TEXT,
        lease_expires_at_utc TEXT,
        completed_at_utc     TEXT,
        completed_by_worker_id TEXT,
        last_error_code      TEXT,
        last_error_message   TEXT,
        created_at_utc       TEXT NOT NULL,
        updated_at_utc       TEXT NOT NULL,
        UNIQUE (source_event_id, effect_index),
        FOREIGN KEY (run_id, source_event_id)
            REFERENCES events(run_id, event_id),
        CHECK (
            (status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at_utc IS NOT NULL)
            OR
            (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at_utc IS NULL)
        ),
        CHECK (
            (status IN ('succeeded', 'dead_letter')
             AND completed_at_utc IS NOT NULL
             AND completed_by_worker_id IS NOT NULL)
            OR
            (status IN ('pending', 'leased')
             AND completed_at_utc IS NULL
             AND completed_by_worker_id IS NULL)
        )
    )
    """.strip(),
    """
    INSERT INTO outbox(
        effect_id, run_id, source_event_id, effect_index, effect_type, effect_class,
        schema_version, engine_version, payload_json, payload_hash, spec_hash, status,
        attempt_count, available_at_utc, lease_owner, lease_expires_at_utc,
        completed_at_utc, completed_by_worker_id, last_error_code, last_error_message,
        created_at_utc, updated_at_utc
    )
    SELECT
        effect_id, run_id, source_event_id, effect_index, effect_type, effect_class,
        schema_version, engine_version, payload_json, payload_hash, '', status,
        attempt_count, available_at_utc, lease_owner, lease_expires_at_utc,
        completed_at_utc,
        CASE
            WHEN status IN ('succeeded', 'dead_letter')
            THEN 'worker_ffffffffffffffffffffffffffffffff'
            ELSE NULL
        END,
        last_error_code, last_error_message, created_at_utc, updated_at_utc
    FROM outbox_v1
    """.strip(),
    "DROP TABLE outbox_v1",
    "CREATE INDEX outbox_due ON outbox(status, available_at_utc, created_at_utc, effect_id)",
    "CREATE INDEX outbox_lease_expiry ON outbox(status, lease_expires_at_utc)",
)


_V3_EMPTY_RESULT_HASH = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"


V3_RECEIPT_AND_EVENT_CHAIN_STATEMENTS = (
    "ALTER TABLE events ADD COLUMN previous_event_hash TEXT",
    "ALTER TABLE events ADD COLUMN event_hash TEXT",
    "DROP INDEX IF EXISTS outbox_due",
    "DROP INDEX IF EXISTS outbox_lease_expiry",
    "ALTER TABLE outbox RENAME TO outbox_v2",
    """
    CREATE TABLE outbox (
        effect_id              TEXT PRIMARY KEY,
        run_id                 TEXT NOT NULL REFERENCES runs(run_id),
        source_event_id        TEXT NOT NULL,
        effect_index           INTEGER NOT NULL CHECK (effect_index >= 0),
        effect_type            TEXT NOT NULL,
        effect_class           TEXT NOT NULL CHECK (effect_class IN ('internal', 'external')),
        schema_version         INTEGER NOT NULL,
        engine_version         TEXT NOT NULL,
        payload_json           TEXT NOT NULL,
        payload_hash           TEXT NOT NULL,
        spec_hash              TEXT NOT NULL,
        status                 TEXT NOT NULL CHECK (
            status IN ('pending', 'leased', 'succeeded', 'dead_letter', 'cancelled')
        ),
        attempt_count          INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        available_at_utc       TEXT NOT NULL,
        lease_owner            TEXT,
        lease_expires_at_utc   TEXT,
        completed_at_utc       TEXT,
        completed_by_worker_id TEXT,
        terminal_generation    INTEGER,
        audit_event_id         TEXT,
        result_summary_json    TEXT,
        result_summary_hash    TEXT,
        last_error_code        TEXT,
        last_error_message     TEXT,
        created_at_utc         TEXT NOT NULL,
        updated_at_utc         TEXT NOT NULL,
        UNIQUE (source_event_id, effect_index),
        UNIQUE (audit_event_id),
        FOREIGN KEY (run_id, source_event_id)
            REFERENCES events(run_id, event_id),
        FOREIGN KEY (run_id, audit_event_id)
            REFERENCES events(run_id, event_id),
        CHECK (
            (status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at_utc IS NOT NULL)
            OR
            (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at_utc IS NULL)
        ),
        CHECK (
            (status IN ('succeeded', 'dead_letter', 'cancelled')
             AND completed_at_utc IS NOT NULL
             AND completed_by_worker_id IS NOT NULL
             AND terminal_generation IS NOT NULL)
            OR
            (status IN ('pending', 'leased')
             AND completed_at_utc IS NULL
             AND completed_by_worker_id IS NULL
             AND terminal_generation IS NULL)
        ),
        CHECK (
            (result_summary_json IS NULL AND result_summary_hash IS NULL)
            OR
            (result_summary_json IS NOT NULL AND result_summary_hash IS NOT NULL)
        ),
        CHECK (audit_event_id IS NULL OR status IN ('succeeded', 'dead_letter'))
    )
    """.strip(),
    f"""
    INSERT INTO outbox(
        effect_id, run_id, source_event_id, effect_index, effect_type, effect_class,
        schema_version, engine_version, payload_json, payload_hash, spec_hash, status,
        attempt_count, available_at_utc, lease_owner, lease_expires_at_utc,
        completed_at_utc, completed_by_worker_id, terminal_generation, audit_event_id,
        result_summary_json, result_summary_hash, last_error_code, last_error_message,
        created_at_utc, updated_at_utc
    )
    SELECT
        effect_id, run_id, source_event_id, effect_index, effect_type, effect_class,
        schema_version, engine_version, payload_json, payload_hash, spec_hash, status,
        attempt_count, available_at_utc, lease_owner, lease_expires_at_utc,
        completed_at_utc, completed_by_worker_id,
        CASE WHEN status IN ('succeeded', 'dead_letter') THEN attempt_count ELSE NULL END,
        NULL,
        CASE WHEN status = 'succeeded' THEN '{{}}' ELSE NULL END,
        CASE WHEN status = 'succeeded' THEN '{_V3_EMPTY_RESULT_HASH}' ELSE NULL END,
        last_error_code, last_error_message, created_at_utc, updated_at_utc
    FROM outbox_v2
    """.strip(),
    "DROP TABLE outbox_v2",
    """
    UPDATE outbox SET status = 'cancelled', lease_owner = NULL,
        lease_expires_at_utc = NULL,
        completed_at_utc = updated_at_utc,
        completed_by_worker_id = 'worker_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
        terminal_generation = attempt_count,
        last_error_code = 'run_cancelled',
        last_error_message = 'The run is terminal; the effect was not dispatched.'
    WHERE run_id IN (
        SELECT run_id FROM runs WHERE status IN ('cancelled', 'failed')
    ) AND status IN ('pending', 'leased')
    """.strip(),
    "CREATE INDEX outbox_due ON outbox(status, available_at_utc, created_at_utc, effect_id)",
    "CREATE INDEX outbox_lease_expiry ON outbox(status, lease_expires_at_utc)",
)


def _migration_int(value: object, *, what: str, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise StateIntegrityError(f"stored {what} is not an integer")
    if minimum is not None and value < minimum:
        raise StateIntegrityError(f"stored {what} is below its minimum")
    return value


def _backfill_event_hashes(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT event_id, command_id, command_type, command_hash, run_id, sequence_no, "
        "expected_revision, new_revision, event_type, schema_version, engine_version, "
        "payload_json, payload_hash, result_json, result_hash, occurred_at_utc, "
        "recorded_at_utc FROM events ORDER BY run_id, sequence_no"
    ).fetchall()
    previous_by_run: dict[str, str] = {}
    next_sequence_by_run: dict[str, int] = {}
    for row in rows:
        run_text = str(row[4])
        previous = previous_by_run.get(run_text, GENESIS_EVENT_HASH)
        expected_sequence = next_sequence_by_run.get(run_text, 1)
        sequence_no = _migration_int(row[5], what="event sequence_no", minimum=1)
        expected_revision = _migration_int(row[6], what="event expected_revision", minimum=0)
        new_revision = _migration_int(row[7], what="event new_revision", minimum=1)
        schema_version = _migration_int(row[9], what="event schema_version", minimum=1)
        if sequence_no != expected_sequence or expected_revision != sequence_no - 1:
            raise StateIntegrityError("legacy event sequence is not contiguous")
        try:
            payload = freeze_json_object(json.loads(str(row[11])))
            result = freeze_json_object(json.loads(str(row[13])))
            occurred_at = _migration_utc(str(row[15]))
            recorded_at = _migration_utc(str(row[16]))
            payload_hash = str(row[12])
            result_hash = str(row[14])
            verify_sha256(payload, payload_hash)
            verify_sha256(result, result_hash)
            event_hash = event_envelope_hash(
                event_id=str(row[0]),
                previous_event_hash=previous,
                command_id=str(row[1]),
                command_type=str(row[2]),
                command_hash=str(row[3]),
                run_id=run_text,
                sequence_no=sequence_no,
                expected_revision=expected_revision,
                new_revision=new_revision,
                event_type=str(row[8]),
                schema_version=schema_version,
                engine_version=str(row[10]),
                payload=payload,
                payload_hash=payload_hash,
                result=result,
                result_hash=result_hash,
                occurred_at_utc=_event_utc_text(occurred_at),
                recorded_at_utc=_event_utc_text(recorded_at),
            )
            KernelEvent.model_validate_json(
                json.dumps(
                    {
                        "event_id": str(row[0]),
                        "command_id": str(row[1]),
                        "command_type": str(row[2]),
                        "command_hash": str(row[3]),
                        "run_id": run_text,
                        "sequence_no": sequence_no,
                        "expected_revision": expected_revision,
                        "new_revision": new_revision,
                        "event_type": str(row[8]),
                        "schema_version": schema_version,
                        "engine_version": str(row[10]),
                        "payload": thaw_json(payload),
                        "payload_hash": payload_hash,
                        "result": thaw_json(result),
                        "result_hash": result_hash,
                        "occurred_at_utc": format_utc(occurred_at),
                        "recorded_at_utc": format_utc(recorded_at),
                        "previous_event_hash": previous,
                        "event_hash": event_hash,
                    },
                    ensure_ascii=False,
                )
            )
        except StateIntegrityError:
            raise
        except (DomainError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise StateIntegrityError("legacy event cannot be upgraded safely") from error
        connection.execute(
            "UPDATE events SET previous_event_hash = ?, event_hash = ? WHERE event_id = ?",
            (previous, event_hash, str(row[0])),
        )
        previous_by_run[run_text] = event_hash
        next_sequence_by_run[run_text] = sequence_no + 1


def _migration_utc(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except (TypeError, ValueError) as error:
        raise StateIntegrityError("stored event timestamp is invalid") from error
    return parsed


def _event_utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _backfill_terminal_receipts(connection: sqlite3.Connection) -> None:
    audit_by_effect: dict[str, list[tuple[str, str, dict[str, object]]]] = {}
    event_rows = connection.execute(
        "SELECT event_id, run_id, event_type, payload_json FROM events"
    ).fetchall()
    for row in event_rows:
        event_type = str(row[2])
        if event_type not in {
            EventType.EFFECT_SUCCEEDED.value,
            EventType.EFFECT_DEAD_LETTERED.value,
        }:
            continue
        try:
            payload = json.loads(str(row[3]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StateIntegrityError("effect audit event payload is invalid") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("effect_id"), str):
            raise StateIntegrityError("effect audit event is missing effect ID")
        audit_by_effect.setdefault(payload["effect_id"], []).append(
            (str(row[0]), str(row[1]), payload)
        )

    rows = connection.execute(
        "SELECT effect_id, run_id, status, attempt_count, completed_by_worker_id, "
        "audit_event_id, result_summary_json, result_summary_hash, last_error_code, "
        "last_error_message FROM outbox"
    ).fetchall()
    for row in rows:
        effect_id = str(row[0])
        run_id = str(row[1])
        status = str(row[2])
        attempt_count = _migration_int(row[3], what="outbox attempt_count", minimum=0)
        candidates = [
            candidate for candidate in audit_by_effect.get(effect_id, ()) if candidate[1] == run_id
        ]
        if len(candidates) > 1:
            raise StateIntegrityError("effect has more than one terminal audit event")
        if status in {"pending", "leased"}:
            if candidates:
                raise StateIntegrityError("active effect already has a terminal audit event")
            continue
        if status == "cancelled":
            if candidates:
                raise StateIntegrityError("cancelled effect has a terminal audit event")
            continue
        if attempt_count < 1:
            raise StateIntegrityError("terminal effect has no lease generation")
        if not candidates:
            # Pre-v3 workers durably completed the outbox row before the
            # optional effect-audit command existed. Preserve that terminal
            # receipt; a later audit command may bind its event exactly once.
            continue
        audit_event_id, _, payload = candidates[0]
        expected_type = (
            EventType.EFFECT_SUCCEEDED.value
            if status == "succeeded"
            else EventType.EFFECT_DEAD_LETTERED.value
        )
        actual_type = connection.execute(
            "SELECT event_type FROM events WHERE event_id = ?", (audit_event_id,)
        ).fetchone()[0]
        if actual_type != expected_type:
            raise StateIntegrityError("effect audit event type does not match terminal status")
        if status == "succeeded":
            summary = payload.get("result_summary")
            if not isinstance(summary, dict):
                raise StateIntegrityError("success audit event is missing result summary")
            summary_json = canonical_json_bytes(summary).decode("utf-8")
            summary_hash = sha256_hex(summary)
            connection.execute(
                "UPDATE outbox SET audit_event_id = ?, result_summary_json = ?, "
                "result_summary_hash = ? WHERE effect_id = ?",
                (audit_event_id, summary_json, summary_hash, effect_id),
            )
        else:
            if payload.get("error_code") != row[8] or payload.get("error_message") != row[9]:
                raise StateIntegrityError("failure audit event is not bound to persisted error")
            connection.execute(
                "UPDATE outbox SET audit_event_id = ? WHERE effect_id = ?",
                (audit_event_id, effect_id),
            )


def _install_v3_triggers(connection: sqlite3.Connection) -> None:
    for statement in (
        """
        CREATE TRIGGER outbox_no_delete
        BEFORE DELETE ON outbox
        BEGIN
            SELECT RAISE(ABORT, 'outbox rows are append-only terminal receipts');
        END;
        """,
        """
        CREATE TRIGGER outbox_terminal_monotonic
        BEFORE UPDATE ON outbox
        WHEN OLD.status IN ('succeeded', 'dead_letter', 'cancelled')
             AND NEW.status <> OLD.status
        BEGIN
            SELECT RAISE(ABORT, 'outbox terminal status is immutable');
        END;
        """,
        """
        CREATE TRIGGER outbox_terminal_metadata_immutable
        BEFORE UPDATE ON outbox
        WHEN OLD.status IN ('succeeded', 'dead_letter', 'cancelled')
             AND (
                 NEW.completed_at_utc IS NOT OLD.completed_at_utc
                 OR NEW.completed_by_worker_id IS NOT OLD.completed_by_worker_id
                 OR NEW.terminal_generation IS NOT OLD.terminal_generation
                 OR (OLD.audit_event_id IS NOT NULL
                     AND NEW.audit_event_id IS NOT OLD.audit_event_id)
                 OR NEW.last_error_code IS NOT OLD.last_error_code
                 OR NEW.last_error_message IS NOT OLD.last_error_message
                 OR NEW.result_summary_json IS NOT OLD.result_summary_json
                 OR (OLD.result_summary_hash IS NOT NULL
                     AND NEW.result_summary_hash IS NOT OLD.result_summary_hash)
             )
        BEGIN
            SELECT RAISE(ABORT, 'outbox terminal receipt is immutable');
        END;
        """,
    ):
        connection.execute(statement)


def _post_apply_v3(connection: sqlite3.Connection) -> None:
    _backfill_event_hashes(connection)
    _backfill_terminal_receipts(connection)
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise StateIntegrityError("foreign key check failed during v3 migration")
    from .interrupts import InterruptRepository
    from .outbox import OutboxRepository
    from .repositories import EventRepository, RunRepository

    runs = RunRepository(connection)
    events = EventRepository(connection)
    interrupts = InterruptRepository(connection)
    outbox = OutboxRepository(connection)
    for run_id in runs.list_ids():
        runs.get_verified(
            run_id,
            events,
            interrupts=interrupts,
            outbox=outbox,
        )
    _install_v3_triggers(connection)


DEFAULT_MIGRATIONS = (
    Migration(
        version=1,
        name="initial_durable_kernel",
        statements=INITIAL_SCHEMA_STATEMENTS,
    ),
    Migration(
        version=2,
        name="p2_projection_ownership_and_outbox_completion",
        statements=V2_HARDENING_STATEMENTS,
        post_apply=_backfill_effect_spec_hashes,
    ),
    Migration(
        version=3,
        name="p2_event_chain_outbox_receipts_and_cancellation",
        statements=V3_RECEIPT_AND_EVENT_CHAIN_STATEMENTS,
        post_apply=_post_apply_v3,
        post_apply_id="p2-event-chain-outbox-receipts-v1",
    ),
)


def _validate_registry(migrations: Iterable[Migration]) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    if not ordered or tuple(item.version for item in ordered) != tuple(range(1, len(ordered) + 1)):
        raise MigrationVersionError("migration registry versions must be continuous from one")
    if len({item.name for item in ordered}) != len(ordered):
        raise MigrationVersionError("migration names must be unique")
    return ordered


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    migrations: Iterable[Migration] = DEFAULT_MIGRATIONS,
    clock: Clock | None = None,
) -> int:
    """Apply all migrations atomically and return the resulting schema version."""

    ordered = _validate_registry(migrations)
    time_source = clock or SystemClock()
    try:
        begin_immediate(connection)
        connection.execute(SCHEMA_MIGRATIONS_SQL)
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = {
            _migration_int(row[0], what="migration version", minimum=1): (str(row[1]), str(row[2]))
            for row in rows
        }
        if tuple(sorted(applied)) != tuple(range(1, len(applied) + 1)):
            raise MigrationVersionError("applied migration versions contain a gap")
        latest = ordered[-1].version
        if applied and max(applied) > latest:
            raise MigrationVersionError("database schema is newer than this code")

        for migration in ordered:
            existing = applied.get(migration.version)
            if existing is not None:
                if existing != (migration.name, migration.checksum):
                    raise MigrationDriftError(
                        "applied migration checksum or name differs",
                        details={
                            "version": migration.version,
                            "expected_name": migration.name,
                            "actual_name": existing[0],
                            "expected_checksum": migration.checksum,
                            "actual_checksum": existing[1],
                        },
                    )
                continue

            for statement in migration.statements:
                connection.execute(statement)
            if migration.post_apply is not None:
                migration.post_apply(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at_utc) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    format_utc(time_source.now_utc()),
                ),
            )

        connection.commit()
        return latest
    except (MigrationDriftError, MigrationVersionError, StorageBusyError):
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.OperationalError as error:
        if connection.in_transaction:
            connection.rollback()
        if "locked" in str(error).casefold() or "busy" in str(error).casefold():
            raise StorageBusyError("database is busy") from error
        raise
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def migrate_database(
    connection: sqlite3.Connection,
    *,
    migrations: Iterable[Migration] = DEFAULT_MIGRATIONS,
    clock: Clock | None = None,
) -> int:
    """Alias with a verb matching common application startup code."""

    return apply_migrations(connection, migrations=migrations, clock=clock)


class MigrationRunner:
    """Reusable migration runner object for application startup and tests."""

    def __init__(
        self,
        migrations: Iterable[Migration] = DEFAULT_MIGRATIONS,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.migrations = tuple(migrations)
        self.clock = clock

    def run(self, connection: sqlite3.Connection) -> int:
        return apply_migrations(connection, migrations=self.migrations, clock=self.clock)


__all__ = [
    "DEFAULT_MIGRATIONS",
    "INITIAL_SCHEMA_STATEMENTS",
    "Migration",
    "MigrationRunner",
    "SCHEMA_MIGRATIONS_SQL",
    "V2_HARDENING_STATEMENTS",
    "V3_RECEIPT_AND_EVENT_CHAIN_STATEMENTS",
    "apply_migrations",
    "migrate_database",
    "migration_checksum",
]
