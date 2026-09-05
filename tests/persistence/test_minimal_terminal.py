import sqlite3

import pytest
from test_p2_hardening_repair import _cancel, _seed

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.ids import completion_command_id
from orca_agent.infrastructure.migrations import DEFAULT_MIGRATIONS, apply_migrations
from orca_agent.infrastructure.sqlite import SQLiteConnectionFactory
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult
from orca_agent.orchestration.commands import CreateRun


@pytest.mark.parametrize("status", ["succeeded", "dead_letter", "cancelled"])
@pytest.mark.parametrize(
    "column",
    [
        "attempt_count",
        "available_at_utc",
        "created_at_utc",
        "updated_at_utc",
    ],
)
def test_all_terminal_metadata_frozen_by_database(tmp_path, status, column):
    clock, path, service, created, _ = _seed(tmp_path)
    if status == "cancelled":
        assert _cancel(service, created.run_id, clock).accepted
    else:
        worker = service.create_worker(
            lambda _: HandlerResult(success=status == "succeeded"),
            max_attempts=1,
        )
        assert worker.run_once()[0].outcome == status
    with SQLiteUnitOfWork(path) as u:
        before = tuple(u.connection.iterdump())
        expression = (
            "attempt_count + 1" if column == "attempt_count" else "'2099-01-01T00:00:00.000000Z'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            u.connection.execute(f"UPDATE outbox SET {column} = {expression}")
        assert tuple(u.connection.iterdump()) == before


def test_v5_collision_validation_rolls_back_trigger_replacement(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(
            "orca_agent.infrastructure.unit_of_work.apply_migrations",
            lambda connection, **kwargs: apply_migrations(
                connection, migrations=DEFAULT_MIGRATIONS[:4], **kwargs
            ),
        )
        _, path, service, _, effects = _seed(tmp_path)
        patch.setattr("orca_agent.application.service.is_new_external_command_id", lambda _: True)
        assert service.execute(
            CreateRun.create(command_id=completion_command_id(effects[0], 1, "succeeded"))
        ).accepted
    connection = SQLiteConnectionFactory(path).connect()
    try:
        before = tuple(connection.iterdump())
        with pytest.raises(StateIntegrityError) as error:
            apply_migrations(connection)
        assert error.value.details["effect_id"] == effects[0]
        assert tuple(connection.iterdump()) == before
    finally:
        connection.close()
