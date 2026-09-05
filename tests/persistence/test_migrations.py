import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.errors import (
    MigrationDriftError,
    MigrationVersionError,
    StateIntegrityError,
)
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.hashing import effect_spec_hash, sha256_hex
from orca_agent.domain.ids import EventId, new_id
from orca_agent.infrastructure.clock import FrozenClock, format_utc
from orca_agent.infrastructure.migrations import (
    DEFAULT_MIGRATIONS,
    Migration,
    apply_migrations,
    migration_checksum,
)
from orca_agent.infrastructure.outbox import OutboxRepository
from orca_agent.infrastructure.repositories import RunRepository, RunSnapshot, json_text
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
            == 5
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
        assert apply_migrations(reopened, clock=clock) == 5
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
            "VALUES (6, 'future', ?, '2026-09-04T00:00:00.000000Z')",
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


def test_historical_migration_checksums_are_frozen() -> None:
    assert tuple(migration.checksum for migration in DEFAULT_MIGRATIONS) == (
        "6f1ff16b97b9e45c286097b1b8de8adeef5ac483c3e08bb02793a156943c9463",
        "3df844dc355db2dc9e8c95e96678233397bf947387f2bc74d358a574fd161a42",
        "21353d3f1dd37b5237b588056457cde33bd6138849266328bd08033a391dc998",
        "36ad3bff16d21bdaca3f56fb6b545147e6deb008adb67cabea5a6e02e5ed8eab",
        "1cdafd5008f195a6ec6063edba9c86f5c974fb106a229511aa379b9b2dd85a11",
    )


def test_v4_sql_has_no_unexpanded_receipt_placeholders() -> None:
    migration = DEFAULT_MIGRATIONS[3]
    assert migration.post_apply_id == "p2-dispatch-permits-atomic-completion-v2"
    sql = "\n".join(migration.statements)
    assert "{_V4_LEGACY_EMPTY_RECEIPT_JSON}" not in sql
    assert "{_V4_LEGACY_EMPTY_RECEIPT_HASH}" not in sql


def test_post_apply_identity_is_part_of_new_migration_checksum() -> None:
    for migration in DEFAULT_MIGRATIONS[:2]:
        assert migration.checksum == migration_checksum(
            migration.version,
            migration.name,
            migration.statements,
        )
    assert (
        DEFAULT_MIGRATIONS[2].checksum
        == Migration(
            3,
            DEFAULT_MIGRATIONS[2].name,
            DEFAULT_MIGRATIONS[2].statements,
            post_apply_id=DEFAULT_MIGRATIONS[2].post_apply_id,
        ).checksum
    )
    assert (
        Migration(
            3,
            DEFAULT_MIGRATIONS[2].name,
            DEFAULT_MIGRATIONS[2].statements,
            post_apply_id="different-post-apply-v1",
        ).checksum
        != DEFAULT_MIGRATIONS[2].checksum
    )


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
        command_hash=command.command_hash(),
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
        connection.execute(
            "INSERT INTO events(event_id, command_id, command_type, command_hash, run_id, "
            "sequence_no, expected_revision, new_revision, event_type, schema_version, "
            "engine_version, payload_json, payload_hash, result_json, result_hash, "
            "occurred_at_utc, recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (
                str(event.event_id),
                str(event.command_id),
                event.command_type.value,
                event.command_hash,
                str(event.run_id),
                event.sequence_no,
                event.expected_revision,
                event.new_revision,
                event.event_type.value,
                event.schema_version,
                event.engine_version,
                json_text(event.payload),
                event.payload_hash,
                json_text(event.result),
                event.result_hash,
                format_utc(event.occurred_at_utc),
                format_utc(event.recorded_at_utc),
            ),
        )
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
        assert apply_migrations(connection, migrations=DEFAULT_MIGRATIONS[:3]) == 3
        assert (
            connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = 1"
            ).fetchone()[0]
            == v1_checksum
        )
        v3_upgraded = OutboxRepository(connection).get(effect_id)
        assert v3_upgraded is not None
        assert len(v3_upgraded.spec_hash) == 64
        assert v3_upgraded.completed_by_worker_id is None
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == [1, 2, 3]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_list(outbox)")]
        assert {row[2] for row in foreign_keys} >= {"events", "runs"}
        assert apply_migrations(connection) == 5
        upgraded = OutboxRepository(connection).get(effect_id)
        assert upgraded is not None
        assert upgraded.completion_protocol == 4
        assert upgraded.dispatch_run_revision is None
        assert upgraded.audit_event_id is None
        assert connection.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == [1, 2, 3, 4, 5]
        interrupt_foreign_keys = [
            tuple(row) for row in connection.execute("PRAGMA foreign_key_list(interrupts)")
        ]
        assert {row[2] for row in interrupt_foreign_keys} >= {"events", "runs"}
    finally:
        connection.close()


def test_v3_semantic_failure_rolls_back_schema_and_metadata(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    connection = _connection(tmp_path / "v3-rollback")
    effect = EffectSpec(
        effect_index=0,
        effect_type="internal.audit",
        effect_class=EffectClass.INTERNAL,
        payload={"source": "rollback"},
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
        command_hash=command.command_hash(),
    )
    try:
        assert apply_migrations(connection, migrations=DEFAULT_MIGRATIONS[:2], clock=clock) == 2
        state = reduce_event(None, event).next_state
        RunRepository(connection).insert(
            RunSnapshot(
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
        connection.execute(
            "INSERT INTO events(event_id, command_id, command_type, command_hash, run_id, "
            "sequence_no, expected_revision, new_revision, event_type, schema_version, "
            "engine_version, payload_json, payload_hash, result_json, result_hash, "
            "occurred_at_utc, recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (
                str(event.event_id),
                str(event.command_id),
                event.command_type.value,
                event.command_hash,
                str(event.run_id),
                event.sequence_no,
                event.expected_revision,
                event.new_revision,
                event.event_type.value,
                event.schema_version,
                event.engine_version,
                json_text(event.payload),
                event.payload_hash,
                json_text(event.result),
                event.result_hash,
                format_utc(event.occurred_at_utc),
                format_utc(event.recorded_at_utc),
            ),
        )
        effect_id = effect.effect_id(event.event_id)
        payload_hash = sha256_hex(effect.payload)
        spec_hash = effect_spec_hash(
            effect_id=str(effect_id),
            run_id=str(command.run_id),
            source_event_id=str(event.event_id),
            effect_index=effect.effect_index,
            effect_type=effect.effect_type,
            effect_class=effect.effect_class.value,
            schema_version=event.schema_version,
            engine_version=event.engine_version,
            payload=effect.payload,
            payload_hash=payload_hash,
        )
        connection.execute(
            "INSERT INTO outbox(effect_id, run_id, source_event_id, effect_index, effect_type, "
            "effect_class, schema_version, engine_version, payload_json, payload_hash, spec_hash, "
            "status, attempt_count, available_at_utc, lease_owner, lease_expires_at_utc, "
            "completed_at_utc, completed_by_worker_id, last_error_code, last_error_message, "
            "created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', "
            "0, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
            (
                str(effect_id),
                str(command.run_id),
                str(event.event_id),
                effect.effect_index,
                effect.effect_type,
                effect.effect_class.value,
                event.schema_version,
                event.engine_version,
                json_text(effect.payload),
                payload_hash,
                spec_hash,
                format_utc(clock.now_utc()),
                format_utc(clock.now_utc()),
                format_utc(clock.now_utc()),
            ),
        )
        connection.commit()
        connection.execute("DELETE FROM outbox WHERE effect_id = ?", (str(effect_id),))
        connection.commit()
        with pytest.raises(StateIntegrityError):
            apply_migrations(connection, clock=clock)
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outbox)").fetchall()}
        assert "terminal_generation" not in columns
    finally:
        connection.close()
