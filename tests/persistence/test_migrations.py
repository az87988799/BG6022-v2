import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.errors import MigrationDriftError, MigrationVersionError
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.migrations import (
    DEFAULT_MIGRATIONS,
    Migration,
    apply_migrations,
)
from orca_agent.infrastructure.sqlite import SQLiteConnectionFactory, resolve_database_path


def _connection(tmp_path):
    return SQLiteConnectionFactory(tmp_path / "state").connect()


def test_connection_policy_and_fresh_schema(tmp_path) -> None:
    database_path = resolve_database_path(tmp_path / "state")
    connection = SQLiteConnectionFactory(database_path).connect()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

        assert (
            apply_migrations(
                connection,
                clock=FrozenClock(datetime(2026, 9, 4, tzinfo=UTC)),
            )
            == 1
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"schema_migrations", "runs", "events", "interrupts", "outbox"} <= tables
        assert not {"actions", "jobs", "evidence", "artifacts"} & tables
    finally:
        connection.close()


def test_migration_is_idempotent_across_close_and_reopen(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    connection = _connection(tmp_path)
    apply_migrations(connection, clock=clock)
    first = connection.execute(
        "SELECT version, name, checksum, applied_at_utc FROM schema_migrations"
    ).fetchall()
    connection.close()

    reopened = _connection(tmp_path)
    try:
        clock.advance(timedelta(seconds=5))
        assert apply_migrations(reopened, clock=clock) == 1
        second = reopened.execute(
            "SELECT version, name, checksum, applied_at_utc FROM schema_migrations"
        ).fetchall()
        assert second == first
    finally:
        reopened.close()


def test_checksum_drift_is_rejected(tmp_path) -> None:
    connection = _connection(tmp_path)
    try:
        apply_migrations(connection)
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 1", ("0" * 64,)
        )
        with pytest.raises(MigrationDriftError) as error:
            apply_migrations(connection)
        assert error.value.code == "migration_drift"
    finally:
        connection.close()


def test_registry_gaps_and_future_database_versions_fail_closed(tmp_path) -> None:
    gap_connection = _connection(tmp_path / "gap")
    try:
        with pytest.raises(MigrationVersionError):
            apply_migrations(
                gap_connection,
                migrations=(
                    Migration(1, "one", ("CREATE TABLE one (id INTEGER)",)),
                    Migration(3, "three", ("CREATE TABLE three (id INTEGER)",)),
                ),
            )
    finally:
        gap_connection.close()

    connection = _connection(tmp_path / "future")
    try:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at_utc) "
            "VALUES (2, 'future', ?, '2026-09-04T00:00:00.000000Z')",
            ("1" * 64,),
        )
        with pytest.raises(MigrationVersionError):
            apply_migrations(connection)
    finally:
        connection.close()


def test_failed_migration_rolls_back_schema_and_metadata(tmp_path) -> None:
    connection = _connection(tmp_path)
    broken = Migration(
        1,
        "broken",
        ("CREATE TABLE marker (id INTEGER)", "THIS IS NOT SQL"),
    )
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, migrations=(broken,))
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                "('schema_migrations', 'marker')"
            ).fetchall()
            == []
        )
    finally:
        connection.close()


def test_default_migration_checksum_is_stable() -> None:
    migration = DEFAULT_MIGRATIONS[0]
    assert migration.checksum
    assert len(migration.checksum) == 64
