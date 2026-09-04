"""Small stdlib-only, checksum-verified SQLite migration runner."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from orca_agent.application.errors import (
    MigrationDriftError,
    MigrationVersionError,
    StateIntegrityError,
    StorageBusyError,
)
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.errors import DomainError
from orca_agent.domain.hashing import effect_spec_hash, verify_sha256
from orca_agent.domain.ids import EffectId, EventId, RunId, effect_id_for
from orca_agent.domain.json_types import freeze_json_object
from orca_agent.domain.versions import CURRENT_SCHEMA_VERSION
from orca_agent.orchestration.effects import EffectClass
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


def migration_checksum(version: int, name: str, statements: Sequence[str]) -> str:
    """Calculate a stable checksum over migration identity and exact SQL text."""

    value = {"version": version, "name": name, "statements": list(statements)}
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    checksum: str = ""
    post_apply: Callable[[sqlite3.Connection], None] | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("migration version must be positive")
        if not self.name.strip():
            raise ValueError("migration name must not be blank")
        if not self.statements:
            raise ValueError("migration must contain at least one statement")
        if not self.checksum:
            object.__setattr__(
                self,
                "checksum",
                migration_checksum(self.version, self.name, self.statements),
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
        applied = {int(row[0]): (str(row[1]), str(row[2])) for row in rows}
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
    "apply_migrations",
    "migrate_database",
    "migration_checksum",
]
