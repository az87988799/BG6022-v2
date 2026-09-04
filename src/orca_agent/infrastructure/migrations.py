"""Small stdlib-only, checksum-verified SQLite migration runner."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from orca_agent.application.errors import (
    MigrationDriftError,
    MigrationVersionError,
    StorageBusyError,
)
from orca_agent.domain.canonical import canonical_json_bytes

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

DEFAULT_MIGRATIONS = (
    Migration(
        version=1,
        name="initial_durable_kernel",
        statements=INITIAL_SCHEMA_STATEMENTS,
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


__all__ = [
    "DEFAULT_MIGRATIONS",
    "INITIAL_SCHEMA_STATEMENTS",
    "Migration",
    "SCHEMA_MIGRATIONS_SQL",
    "apply_migrations",
    "migrate_database",
    "migration_checksum",
]
