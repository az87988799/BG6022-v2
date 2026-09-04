import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.errors import MigrationDriftError, MigrationVersionError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import EventId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.migrations import (
    DEFAULT_MIGRATIONS,
    Migration,
    apply_migrations,
)
from orca_agent.infrastructure.outbox import OutboxRepository
from orca_agent.infrastructure.repositories import EventRepository, RunRepository, RunSnapshot
from orca_agent.infrastructure.sqlite import SQLiteConnectionFactory, resolve_database_path
from orca_agent.orchestration.commands import CreateRun
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.events import EventType, KernelEvent
from orca_agent.orchestration.reducer import reduce_event
from orca_agent.orchestration.replay import state_hash
from orca_agent.orchestration.state import RunStatus


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
            == 2
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
        assert apply_migrations(reopened, clock=clock) == 2
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
            "VALUES (3, 'future', ?, '2026-09-04T00:00:00.000000Z')",
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


def test_v2_upgrades_existing_v1_data_and_backfills_immutable_effect_metadata(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    connection = _connection(tmp_path / "upgrade")
    effect = EffectSpec(
        effect_index=0,
        effect_type="external.test",
        effect_class=EffectClass.EXTERNAL,
        payload={"source": "legacy"},
    )
    command = CreateRun.create(effects=(effect,), requested_at_utc=clock.now_utc())
    event_id = new_id(EventId)
    result = ApplicationResult.accepted_result(
        code="run_created",
        run_id=command.run_id,
        revision=1,
        status=RunStatus.CREATED,
        event_id=event_id,
    )
    event = KernelEvent.create(
        event_id=event_id,
        command_id=command.command_id,
        command_type=command.command_type,
        run_id=command.run_id,
        sequence_no=1,
        expected_revision=0,
        event_type=EventType.RUN_CREATED,
        payload=command.event_payload(),
        result=result,
        occurred_at_utc=clock.now_utc(),
    )
    try:
        v1_checksum = DEFAULT_MIGRATIONS[0].checksum
        assert apply_migrations(connection, migrations=(DEFAULT_MIGRATIONS[0],)) == 1
        state = reduce_event(None, event).next_state
        RunRepository(connection).insert(
            snapshot=RunSnapshot(
                run_id=command.run_id,
                schema_version=event.schema_version,
                engine_version=event.engine_version,
                revision=1,
                state=state,
                state_hash=state_hash(state),
                last_event_id=event.event_id,
                created_at_utc=clock.now_utc(),
                updated_at_utc=clock.now_utc(),
            )
        )
        EventRepository(connection).append(event, command_hash=command.command_hash())
        effect_id = effect.effect_id(event.event_id)
        connection.execute(
            "INSERT INTO outbox(effect_id, run_id, source_event_id, effect_index, effect_type, "
            "effect_class, schema_version, engine_version, payload_json, payload_hash, status, "
            "attempt_count, available_at_utc, lease_owner, lease_expires_at_utc, "
            "completed_at_utc, last_error_code, last_error_message, created_at_utc, "
            "updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, "
            "NULL, NULL, NULL, NULL, ?, ?)",
            (
                str(effect_id),
                str(command.run_id),
                str(event.event_id),
                effect.effect_index,
                effect.effect_type,
                effect.effect_class.value,
                event.schema_version,
                event.engine_version,
                '{"source":"legacy"}',
                sha256_hex(effect.payload),
                "2026-09-04T00:00:00.000000Z",
                "2026-09-04T00:00:00.000000Z",
                "2026-09-04T00:00:00.000000Z",
            ),
        )
        assert apply_migrations(connection) == 2
        assert (
            connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = 1"
            ).fetchone()[0]
            == v1_checksum
        )
        upgraded = OutboxRepository(connection).get(effect_id)
        assert upgraded is not None
        assert len(upgraded.spec_hash) == 64
        assert upgraded.completed_by_worker_id is None
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == [1, 2]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_list(outbox)")]
        assert {row[2] for row in foreign_keys} >= {"events", "runs"}
    finally:
        connection.close()
