"""Small stdlib-only, checksum-verified SQLite migration runner."""

from __future__ import annotations

import hashlib
import json
import re
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
from orca_agent.domain.ids import CommandId, EffectId, EventId, RunId, effect_id_for
from orca_agent.domain.json_types import freeze_json_object, thaw_json
from orca_agent.domain.versions import CURRENT_SCHEMA_VERSION
from orca_agent.orchestration.commands import CommandType
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


def _backfill_effect_spec_hashes_v2_legacy(connection: sqlite3.Connection) -> None:
    """Backfill v2 using the historical callback semantics.

    IMMUTABLE MIGRATION CALLBACK: this function is part of the v2 migration
    identity.  Do not replace its permissive integer conversion with the
    stricter runtime storage parser; new validation belongs in later
    post-apply hooks and repository loaders.
    """

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
            effect_index = int(row[3])
            effect_type = str(row[4])
            effect_class = EffectClass(str(row[5]))
            schema_version = int(row[6])
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
_V4_LEGACY_EMPTY_RECEIPT_JSON = (
    '{"artifact_ids":[],"outcome_code":"completed","receipt_schema":"effect-success/v1"}'
)
_V4_LEGACY_EMPTY_RECEIPT_HASH = "b67c2b56e9c4474e273276f170fcc01e9aadf17926662e96dbc6aa5e696c1ddd"


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


V4_DISPATCH_PERMIT_AND_COMMAND_RECEIPT_STATEMENTS = (
    "DROP TRIGGER IF EXISTS outbox_no_delete",
    "DROP TRIGGER IF EXISTS outbox_terminal_monotonic",
    "DROP TRIGGER IF EXISTS outbox_terminal_metadata_immutable",
    "DROP INDEX IF EXISTS one_dispatching_effect_per_run",
    "DROP INDEX IF EXISTS outbox_due",
    "DROP INDEX IF EXISTS outbox_lease_expiry",
    "ALTER TABLE outbox RENAME TO outbox_v3",
    """
    CREATE TABLE outbox (
        effect_id                 TEXT PRIMARY KEY,
        run_id                    TEXT NOT NULL REFERENCES runs(run_id),
        source_event_id           TEXT NOT NULL,
        effect_index              INTEGER NOT NULL CHECK (effect_index >= 0),
        effect_type               TEXT NOT NULL,
        effect_class              TEXT NOT NULL CHECK (effect_class IN ('internal', 'external')),
        schema_version            INTEGER NOT NULL,
        engine_version            TEXT NOT NULL,
        payload_json              TEXT NOT NULL,
        payload_hash              TEXT NOT NULL,
        spec_hash                 TEXT NOT NULL,
        status                    TEXT NOT NULL CHECK (
            status IN ('pending', 'leased', 'dispatching', 'succeeded', 'dead_letter', 'cancelled')
        ),
        attempt_count             INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        available_at_utc          TEXT NOT NULL,
        lease_owner               TEXT,
        lease_expires_at_utc      TEXT,
        dispatch_authorized_at_utc TEXT,
        dispatch_run_revision     INTEGER,
        dispatch_policy_version   INTEGER,
        completed_at_utc          TEXT,
        completed_by_worker_id    TEXT,
        terminal_generation       INTEGER,
        audit_event_id            TEXT,
        result_summary_json       TEXT,
        result_summary_hash       TEXT,
        completion_protocol       INTEGER NOT NULL CHECK (completion_protocol IN (3, 4)),
        last_error_code           TEXT,
        last_error_message        TEXT,
        created_at_utc            TEXT NOT NULL,
        updated_at_utc            TEXT NOT NULL,
        UNIQUE (source_event_id, effect_index),
        UNIQUE (audit_event_id),
        FOREIGN KEY (run_id, source_event_id)
            REFERENCES events(run_id, event_id),
        FOREIGN KEY (run_id, audit_event_id)
            REFERENCES events(run_id, event_id),
        CHECK (
            (status = 'pending'
             AND lease_owner IS NULL AND lease_expires_at_utc IS NULL
             AND dispatch_authorized_at_utc IS NULL
             AND dispatch_run_revision IS NULL AND dispatch_policy_version IS NULL)
            OR
            (status = 'leased'
             AND lease_owner IS NOT NULL AND lease_expires_at_utc IS NOT NULL
             AND dispatch_authorized_at_utc IS NULL
             AND dispatch_run_revision IS NULL AND dispatch_policy_version IS NULL)
            OR
            (status = 'dispatching'
             AND lease_owner IS NOT NULL AND lease_expires_at_utc IS NOT NULL
             AND dispatch_authorized_at_utc IS NOT NULL
             AND dispatch_run_revision IS NOT NULL AND dispatch_run_revision >= 1
             AND dispatch_policy_version IS NOT NULL AND dispatch_policy_version >= 1)
            OR
            (status IN ('succeeded', 'dead_letter', 'cancelled')
             AND lease_owner IS NULL AND lease_expires_at_utc IS NULL
             AND dispatch_authorized_at_utc IS NULL
             AND dispatch_run_revision IS NULL AND dispatch_policy_version IS NULL)
        ),
        CHECK (
            (status IN ('succeeded', 'dead_letter', 'cancelled')
             AND completed_at_utc IS NOT NULL
             AND completed_by_worker_id IS NOT NULL
             AND terminal_generation IS NOT NULL
             AND terminal_generation >= 0)
            OR
            (status IN ('pending', 'leased', 'dispatching')
             AND completed_at_utc IS NULL
             AND completed_by_worker_id IS NULL
             AND terminal_generation IS NULL)
        ),
        CHECK (
            (status = 'succeeded' AND result_summary_json IS NOT NULL
             AND result_summary_hash IS NOT NULL)
            OR
            (status <> 'succeeded' AND result_summary_json IS NULL
             AND result_summary_hash IS NULL)
        ),
        CHECK (
            (result_summary_json IS NULL AND result_summary_hash IS NULL)
            OR
            (result_summary_json IS NOT NULL AND result_summary_hash IS NOT NULL)
        ),
        CHECK (
            (status IN ('pending', 'leased', 'dispatching')
             AND audit_event_id IS NULL AND completion_protocol = 4)
            OR
            (status IN ('succeeded', 'dead_letter')
             AND completion_protocol = 4 AND audit_event_id IS NOT NULL)
            OR
            (status IN ('succeeded', 'dead_letter', 'cancelled')
             AND completion_protocol = 3 AND audit_event_id IS NULL)
            OR
            (status = 'cancelled' AND completion_protocol = 4 AND audit_event_id IS NULL)
        ),
        CHECK (
            (last_error_code IS NULL AND last_error_message IS NULL)
            OR
            (last_error_code IS NOT NULL AND last_error_message IS NOT NULL)
        )
    )
    """.strip(),
    f"""
    INSERT INTO outbox(
        effect_id, run_id, source_event_id, effect_index, effect_type, effect_class,
        schema_version, engine_version, payload_json, payload_hash, spec_hash, status,
        attempt_count, available_at_utc, lease_owner, lease_expires_at_utc,
        dispatch_authorized_at_utc, dispatch_run_revision, dispatch_policy_version,
        completed_at_utc, completed_by_worker_id, terminal_generation, audit_event_id,
        result_summary_json, result_summary_hash, completion_protocol, last_error_code,
        last_error_message, created_at_utc, updated_at_utc
    )
    SELECT
        effect_id, run_id, source_event_id, effect_index, effect_type, effect_class,
        schema_version, engine_version, payload_json, payload_hash, spec_hash, status,
        attempt_count, available_at_utc, lease_owner, lease_expires_at_utc,
        NULL, NULL, NULL, completed_at_utc, completed_by_worker_id, terminal_generation,
        audit_event_id,
        CASE
            WHEN status = 'succeeded' AND result_summary_json = '{{}}'
            THEN '{_V4_LEGACY_EMPTY_RECEIPT_JSON}'
            ELSE result_summary_json
        END,
        CASE
            WHEN status = 'succeeded' AND result_summary_json = '{{}}'
            THEN '{_V4_LEGACY_EMPTY_RECEIPT_HASH}'
            ELSE result_summary_hash
        END,
        CASE
            WHEN status IN ('succeeded', 'dead_letter', 'cancelled')
                 AND audit_event_id IS NULL THEN 3
            ELSE 4
        END,
        last_error_code, last_error_message, created_at_utc, updated_at_utc
    FROM outbox_v3
    """.strip(),
    "DROP TABLE outbox_v3",
    "CREATE INDEX outbox_due ON outbox(status, available_at_utc, created_at_utc, effect_id)",
    "CREATE INDEX outbox_lease_expiry ON outbox(status, lease_expires_at_utc)",
    "CREATE UNIQUE INDEX one_dispatching_effect_per_run ON outbox(run_id) "
    "WHERE status = 'dispatching'",
    """
    CREATE TRIGGER outbox_spec_immutable
    BEFORE UPDATE ON outbox
    WHEN NEW.effect_id IS NOT OLD.effect_id
         OR NEW.run_id IS NOT OLD.run_id
         OR NEW.source_event_id IS NOT OLD.source_event_id
         OR NEW.effect_index IS NOT OLD.effect_index
         OR NEW.effect_type IS NOT OLD.effect_type
         OR NEW.effect_class IS NOT OLD.effect_class
         OR NEW.schema_version IS NOT OLD.schema_version
         OR NEW.engine_version IS NOT OLD.engine_version
         OR NEW.payload_json IS NOT OLD.payload_json
         OR NEW.payload_hash IS NOT OLD.payload_hash
         OR NEW.spec_hash IS NOT OLD.spec_hash
    BEGIN
        SELECT RAISE(ABORT, 'outbox effect specification is immutable');
    END;
    """.strip(),
    """
    CREATE TRIGGER interrupt_identity_immutable
    BEFORE UPDATE ON interrupts
    WHEN NEW.interrupt_id IS NOT OLD.interrupt_id
         OR NEW.run_id IS NOT OLD.run_id
         OR NEW.kind IS NOT OLD.kind
         OR NEW.schema_version IS NOT OLD.schema_version
         OR NEW.engine_version IS NOT OLD.engine_version
         OR NEW.request_event_id IS NOT OLD.request_event_id
         OR NEW.created_at_utc IS NOT OLD.created_at_utc
         OR NEW.expires_at_utc IS NOT OLD.expires_at_utc
         OR NEW.payload_json IS NOT OLD.payload_json
         OR NEW.payload_hash IS NOT OLD.payload_hash
    BEGIN
        SELECT RAISE(ABORT, 'interrupt identity is immutable');
    END;
    """.strip(),
    """
    CREATE TABLE command_receipts (
        command_id       TEXT PRIMARY KEY,
        command_type     TEXT NOT NULL,
        command_hash     TEXT NOT NULL CHECK(length(command_hash) = 64),
        run_id           TEXT NOT NULL,
        binding_kind     TEXT NOT NULL CHECK(
            binding_kind IN ('event', 'effect_audit_alias')
        ),
        effect_id        TEXT,
        result_event_id  TEXT NOT NULL,
        recorded_at_utc  TEXT NOT NULL,
        FOREIGN KEY(run_id, result_event_id)
            REFERENCES events(run_id, event_id),
        CHECK(
            (binding_kind = 'event' AND effect_id IS NULL)
            OR
            (binding_kind = 'effect_audit_alias' AND effect_id IS NOT NULL)
        )
    )
    """.strip(),
    """
    INSERT INTO command_receipts(
        command_id, command_type, command_hash, run_id, binding_kind,
        effect_id, result_event_id, recorded_at_utc
    )
    SELECT command_id, command_type, command_hash, run_id, 'event', NULL,
        event_id, recorded_at_utc
    FROM events
    """.strip(),
    """
    CREATE TRIGGER outbox_no_delete
    BEFORE DELETE ON outbox
    BEGIN
        SELECT RAISE(ABORT, 'outbox rows are append-only terminal receipts');
    END;
    """.strip(),
    """
    CREATE TRIGGER outbox_terminal_monotonic
    BEFORE UPDATE ON outbox
    WHEN OLD.status IN ('succeeded', 'dead_letter', 'cancelled')
         AND NEW.status <> OLD.status
    BEGIN
        SELECT RAISE(ABORT, 'outbox terminal status is immutable');
    END;
    """.strip(),
    """
    CREATE TRIGGER outbox_terminal_metadata_immutable
    BEFORE UPDATE ON outbox
    WHEN OLD.status IN ('succeeded', 'dead_letter', 'cancelled')
         AND (
             NEW.completed_at_utc IS NOT OLD.completed_at_utc
             OR NEW.completed_by_worker_id IS NOT OLD.completed_by_worker_id
             OR NEW.terminal_generation IS NOT OLD.terminal_generation
             OR NEW.audit_event_id IS NOT OLD.audit_event_id
             OR NEW.result_summary_json IS NOT OLD.result_summary_json
             OR NEW.result_summary_hash IS NOT OLD.result_summary_hash
             OR NEW.completion_protocol IS NOT OLD.completion_protocol
             OR NEW.last_error_code IS NOT OLD.last_error_code
             OR NEW.last_error_message IS NOT OLD.last_error_message
         )
    BEGIN
        SELECT RAISE(ABORT, 'outbox terminal receipt is immutable');
    END;
    """.strip(),
    """
    CREATE TRIGGER outbox_dispatch_metadata_immutable
    BEFORE UPDATE ON outbox
    WHEN OLD.status = 'dispatching'
         AND NEW.status = 'dispatching'
         AND (
             NEW.dispatch_authorized_at_utc IS NOT OLD.dispatch_authorized_at_utc
             OR NEW.dispatch_run_revision IS NOT OLD.dispatch_run_revision
             OR NEW.dispatch_policy_version IS NOT OLD.dispatch_policy_version
             OR NEW.lease_owner IS NOT OLD.lease_owner
             OR NEW.attempt_count IS NOT OLD.attempt_count
         )
    BEGIN
        SELECT RAISE(ABORT, 'dispatch permit metadata is immutable');
    END;
    """.strip(),
    """
    CREATE TRIGGER command_receipts_no_update
    BEFORE UPDATE ON command_receipts
    BEGIN
        SELECT RAISE(ABORT, 'command receipts are append-only');
    END;
    """.strip(),
    """
    CREATE TRIGGER command_receipts_no_delete
    BEFORE DELETE ON command_receipts
    BEGIN
        SELECT RAISE(ABORT, 'command receipts are append-only');
    END;
    """.strip(),
)


_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _post_apply_v4(connection: sqlite3.Connection) -> None:
    """Validate the expanded schema before recording migration version four."""

    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise StateIntegrityError("foreign key check failed during v4 migration")

    from .interrupts import InterruptRepository
    from .outbox import OutboxRepository
    from .repositories import EventRepository, RunRepository

    events = EventRepository(connection)
    _verify_v4_command_receipts(connection, events)
    runs = RunRepository(connection)
    interrupts = InterruptRepository(connection)
    outbox = OutboxRepository(connection)
    for run_id in runs.list_ids():
        runs.get_verified(
            run_id,
            events,
            interrupts=interrupts,
            outbox=outbox,
        )


def _verify_v4_command_receipts(connection: sqlite3.Connection, events: object) -> None:
    rows = connection.execute(
        "SELECT command_id, command_type, command_hash, run_id, binding_kind, effect_id, "
        "result_event_id, recorded_at_utc FROM command_receipts"
    ).fetchall()
    event_ids = {
        str(row[0]) for row in connection.execute("SELECT event_id FROM events").fetchall()
    }
    authoritative_event_ids: set[str] = set()
    for row in rows:
        try:
            command_id = CommandId(str(row[0]))
            command_type = CommandType(str(row[1]))
            command_hash = str(row[2])
            run_id = RunId(str(row[3]))
            result_event_id = EventId(str(row[6]))
            _migration_utc(str(row[7]))
        except (DomainError, TypeError, ValueError) as error:
            raise StateIntegrityError("v4 command receipt is invalid") from error
        if _HASH_PATTERN.fullmatch(command_hash) is None:
            raise StateIntegrityError("v4 command receipt hash is invalid")
        binding_kind = str(row[4])
        if binding_kind == "event":
            if row[5] is not None:
                raise StateIntegrityError("event command receipt contains an effect ID")
            if str(result_event_id) in authoritative_event_ids:
                raise StateIntegrityError("multiple command receipts bind one event")
        elif binding_kind == "effect_audit_alias":
            try:
                effect_id = EffectId(str(row[5]))
            except (DomainError, TypeError, ValueError) as error:
                raise StateIntegrityError("v4 command receipt effect ID is invalid") from error
            if command_type not in (
                CommandType.RECORD_EFFECT_SUCCEEDED,
                CommandType.RECORD_EFFECT_FAILED,
            ):
                raise StateIntegrityError("effect audit alias has an invalid command type")
            effect_row = connection.execute(
                "SELECT run_id, status, audit_event_id FROM outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            expected_status = (
                "succeeded"
                if command_type is CommandType.RECORD_EFFECT_SUCCEEDED
                else "dead_letter"
            )
            if (
                effect_row is None
                or str(effect_row[0]) != str(run_id)
                or str(effect_row[1]) != expected_status
                or str(effect_row[2]) != str(result_event_id)
            ):
                raise StateIntegrityError("effect audit alias is not authoritative")
        else:
            raise StateIntegrityError("v4 command receipt binding kind is invalid")
        event = events.get(result_event_id)  # type: ignore[union-attr]
        if event is None or event.run_id != run_id:
            raise StateIntegrityError("command receipt result event is invalid")
        if binding_kind == "event" and (
            event.command_id != command_id
            or event.command_type is not command_type
            or event.command_hash != command_hash
        ):
            raise StateIntegrityError("event command receipt does not match its event")
        if binding_kind == "event":
            authoritative_event_ids.add(str(result_event_id))
        elif event.event_type not in (
            EventType.EFFECT_SUCCEEDED,
            EventType.EFFECT_DEAD_LETTERED,
        ):
            raise StateIntegrityError("effect audit alias references a non-audit event")
    if authoritative_event_ids != event_ids:
        raise StateIntegrityError("command receipts do not cover the event history")


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


V5_TERMINAL_METADATA_STATEMENTS = (
    "DROP TRIGGER outbox_terminal_metadata_immutable",
    """
    CREATE TRIGGER outbox_terminal_metadata_immutable
    BEFORE UPDATE ON outbox
    WHEN OLD.status IN ('succeeded', 'dead_letter', 'cancelled')
         AND (
             NEW.completed_at_utc IS NOT OLD.completed_at_utc
             OR NEW.completed_by_worker_id IS NOT OLD.completed_by_worker_id
             OR NEW.terminal_generation IS NOT OLD.terminal_generation
             OR NEW.audit_event_id IS NOT OLD.audit_event_id
             OR NEW.result_summary_json IS NOT OLD.result_summary_json
             OR NEW.result_summary_hash IS NOT OLD.result_summary_hash
             OR NEW.completion_protocol IS NOT OLD.completion_protocol
             OR NEW.last_error_code IS NOT OLD.last_error_code
             OR NEW.last_error_message IS NOT OLD.last_error_message
             OR NEW.attempt_count IS NOT OLD.attempt_count
             OR NEW.available_at_utc IS NOT OLD.available_at_utc
             OR NEW.created_at_utc IS NOT OLD.created_at_utc
             OR NEW.updated_at_utc IS NOT OLD.updated_at_utc
         )
    BEGIN
        SELECT RAISE(ABORT, 'outbox terminal receipt is immutable');
    END;
    """.strip(),
)


def _post_apply_v5(connection: sqlite3.Connection) -> None:
    """Validate existing projections and identify historical completion collisions."""
    _post_apply_v4(connection)
    from .outbox import OutboxRepository
    from .repositories import RunRepository

    outbox = OutboxRepository(connection)
    for run_id in RunRepository(connection).list_ids():
        for record in outbox.list_for_run(run_id):
            outbox.verify_completion_namespace(record)


# P3 adds only new append-oriented tables.  Migrations 1-5 and their checksums
# are intentionally left untouched; the P2 kernel remains the durable event
# and outbox owner while P3 records carry the version-2 workflow contracts.
P3_WORKFLOW_STATEMENTS = (
    """
    CREATE TABLE workflow_records (
        record_id       TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL REFERENCES runs(run_id),
        record_type     TEXT NOT NULL,
        schema_version  INTEGER NOT NULL CHECK (schema_version >= 1),
        engine_version   TEXT NOT NULL,
        record_json     TEXT NOT NULL,
        record_hash     TEXT NOT NULL CHECK (length(record_hash) = 64),
        source_event_id TEXT,
        created_at_utc  TEXT NOT NULL,
        FOREIGN KEY (run_id, source_event_id)
            REFERENCES events(run_id, event_id)
    )
    """.strip(),
    "CREATE INDEX workflow_records_run_type ON workflow_records("
    "run_id, record_type, created_at_utc)",
    """
    CREATE TRIGGER workflow_records_no_update
    BEFORE UPDATE ON workflow_records
    BEGIN
        SELECT RAISE(ABORT, 'workflow records are append-only');
    END;
    """.strip(),
    """
    CREATE TRIGGER workflow_records_no_delete
    BEFORE DELETE ON workflow_records
    BEGIN
        SELECT RAISE(ABORT, 'workflow records are append-only');
    END;
    """.strip(),
    """
    CREATE TABLE actions (
        action_id       TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL REFERENCES runs(run_id),
        conversation_id TEXT NOT NULL,
        action_json     TEXT NOT NULL,
        action_hash     TEXT NOT NULL CHECK (length(action_hash) = 64),
        envelope_hash   TEXT NOT NULL CHECK (length(envelope_hash) = 64),
        budget_hash     TEXT NOT NULL CHECK (length(budget_hash) = 64),
        idempotency_key TEXT NOT NULL UNIQUE,
        approval_grant_id TEXT,
        execution_id    TEXT UNIQUE,
        ledger_state    TEXT NOT NULL CHECK (
            ledger_state IN ('planned', 'approved', 'submitting', 'submitted',
                             'succeeded', 'failed', 'cancelled')
        ),
        created_at_utc  TEXT NOT NULL,
        updated_at_utc  TEXT NOT NULL
    )
    """.strip(),
    "CREATE UNIQUE INDEX actions_run_action_unique ON actions(run_id, action_id)",
    """
    CREATE TRIGGER actions_identity_immutable
    BEFORE UPDATE ON actions
    WHEN NEW.action_id IS NOT OLD.action_id
      OR NEW.run_id IS NOT OLD.run_id
      OR NEW.conversation_id IS NOT OLD.conversation_id
      OR NEW.action_json IS NOT OLD.action_json
      OR NEW.action_hash IS NOT OLD.action_hash
      OR NEW.envelope_hash IS NOT OLD.envelope_hash
      OR NEW.budget_hash IS NOT OLD.budget_hash
      OR NEW.idempotency_key IS NOT OLD.idempotency_key
      OR NEW.created_at_utc IS NOT OLD.created_at_utc
    BEGIN
        SELECT RAISE(ABORT, 'action identity is immutable');
    END;
    """.strip(),
    """
    CREATE TABLE jobs (
        job_id          TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL REFERENCES runs(run_id),
        action_id       TEXT NOT NULL REFERENCES actions(action_id),
        execution_id    TEXT NOT NULL UNIQUE,
        input_hash      TEXT NOT NULL CHECK (length(input_hash) = 64),
        fixture_id      TEXT NOT NULL,
        fixture_version TEXT NOT NULL,
        fixture_hash    TEXT NOT NULL CHECK (length(fixture_hash) = 64),
        status          TEXT NOT NULL CHECK (status IN ('submitted', 'succeeded', 'failed')),
        raw_result_artifact_id TEXT,
        result_hash     TEXT,
        created_at_utc  TEXT NOT NULL,
        updated_at_utc  TEXT NOT NULL,
        FOREIGN KEY (run_id, action_id) REFERENCES actions(run_id, action_id)
    )
    """.strip(),
    """
    CREATE TRIGGER jobs_identity_immutable
    BEFORE UPDATE ON jobs
    WHEN NEW.job_id IS NOT OLD.job_id
      OR NEW.run_id IS NOT OLD.run_id
      OR NEW.action_id IS NOT OLD.action_id
      OR NEW.execution_id IS NOT OLD.execution_id
      OR NEW.input_hash IS NOT OLD.input_hash
      OR NEW.fixture_id IS NOT OLD.fixture_id
      OR NEW.fixture_version IS NOT OLD.fixture_version
      OR NEW.fixture_hash IS NOT OLD.fixture_hash
      OR NEW.created_at_utc IS NOT OLD.created_at_utc
    BEGIN
        SELECT RAISE(ABORT, 'job identity is immutable');
    END;
    """.strip(),
    """
    CREATE TABLE artifacts (
        artifact_id     TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL REFERENCES runs(run_id),
        action_id       TEXT,
        execution_id    TEXT,
        content_hash    TEXT NOT NULL CHECK (length(content_hash) = 64),
        size_bytes      INTEGER NOT NULL CHECK (size_bytes >= 0),
        media_type      TEXT NOT NULL,
        relative_path   TEXT NOT NULL,
        created_at_utc  TEXT NOT NULL,
        UNIQUE (content_hash, relative_path)
    )
    """.strip(),
    """
    CREATE TRIGGER artifacts_no_update
    BEFORE UPDATE ON artifacts
    BEGIN
        SELECT RAISE(ABORT, 'artifacts are immutable');
    END;
    """.strip(),
    """
    CREATE TRIGGER artifacts_no_delete
    BEFORE DELETE ON artifacts
    BEGIN
        SELECT RAISE(ABORT, 'artifacts are immutable');
    END;
    """.strip(),
    """
    CREATE TABLE evidence (
        evidence_id     TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL REFERENCES runs(run_id),
        action_id       TEXT NOT NULL REFERENCES actions(action_id),
        execution_id    TEXT NOT NULL,
        artifact_id     TEXT NOT NULL REFERENCES artifacts(artifact_id),
        evidence_json   TEXT NOT NULL,
        evidence_hash   TEXT NOT NULL CHECK (length(evidence_hash) = 64),
        created_at_utc  TEXT NOT NULL,
        FOREIGN KEY (run_id, action_id) REFERENCES actions(run_id, action_id)
    )
    """.strip(),
    """
    CREATE TRIGGER evidence_no_update
    BEFORE UPDATE ON evidence
    BEGIN
        SELECT RAISE(ABORT, 'evidence is immutable');
    END;
    """.strip(),
    """
    CREATE TRIGGER evidence_no_delete
    BEFORE DELETE ON evidence
    BEGIN
        SELECT RAISE(ABORT, 'evidence is immutable');
    END;
    """.strip(),
)


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
        post_apply=_backfill_effect_spec_hashes_v2_legacy,
    ),
    Migration(
        version=3,
        name="p2_event_chain_outbox_receipts_and_cancellation",
        statements=V3_RECEIPT_AND_EVENT_CHAIN_STATEMENTS,
        post_apply=_post_apply_v3,
        post_apply_id="p2-event-chain-outbox-receipts-v1",
    ),
    Migration(
        version=4,
        name="p2_dispatch_permits_atomic_completion_and_command_receipts",
        statements=V4_DISPATCH_PERMIT_AND_COMMAND_RECEIPT_STATEMENTS,
        post_apply=_post_apply_v4,
        post_apply_id="p2-dispatch-permits-atomic-completion-v2",
    ),
    Migration(
        version=5,
        name="p2_freeze_all_terminal_metadata",
        statements=V5_TERMINAL_METADATA_STATEMENTS,
        post_apply=_post_apply_v5,
        post_apply_id="p2-terminal-metadata-and-namespace-v1",
    ),
    Migration(
        version=6,
        name="p3_water_workflow_records_and_artifacts",
        statements=P3_WORKFLOW_STATEMENTS,
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
    "V4_DISPATCH_PERMIT_AND_COMMAND_RECEIPT_STATEMENTS",
    "P3_WORKFLOW_STATEMENTS",
    "apply_migrations",
    "migrate_database",
    "migration_checksum",
]
