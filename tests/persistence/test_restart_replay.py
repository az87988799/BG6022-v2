from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import EventId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CancelRun, CreateRun, RequestInterrupt


def test_close_reopen_preserves_replayable_run_and_interrupt(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create())
    requested = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="input",
            payload={"question": "continue?"},
            expires_at_utc=clock.now_utc() + timedelta(minutes=1),
        )
    )

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        snapshot = uow.runs.get_verified(created.run_id, uow.events)
        assert snapshot.revision == 2
        assert snapshot.state.pending_interrupt_id == requested.interrupt_id
        assert len(uow.events.list_for_run(created.run_id)) == 2


def test_state_change_refuses_deleted_event_history(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create())
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        uow.connection.execute("DELETE FROM events WHERE run_id = ?", (str(created.run_id),))

    result = service.execute(
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=1,
            reason_code="user_cancelled",
        )
    )

    assert result.accepted is False
    assert result.code == "state_integrity_error"
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.events.count_for_run(created.run_id) == 0
        assert uow.runs.require(created.run_id).revision == 1


def test_sequence_gap_fails_closed(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create())
    cancelled = service.execute(
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=1,
            reason_code="user_cancelled",
        )
    )
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        uow.connection.execute(
            "UPDATE events SET sequence_no = 3 WHERE event_id = ?", (str(cancelled.event_id),)
        )
        with pytest.raises(StateIntegrityError):
            uow.runs.get_verified(created.run_id, uow.events)


@pytest.mark.parametrize(
    "column, value",
    (
        ("event_id", "event_invalid"),
        ("command_id", "command_invalid"),
        ("schema_version", 99),
    ),
)
def test_invalid_persisted_event_fields_return_typed_storage_error(tmp_path, column, value) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create())
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        uow.connection.execute(
            f"UPDATE events SET {column} = ? WHERE run_id = ?",  # noqa: S608 - test column allowlist
            (value, str(created.run_id)),
        )

    result = service.execute(
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=1,
            reason_code="user_cancelled",
        )
    )

    assert result.accepted is False
    assert result.code == "state_integrity_error"


@pytest.mark.parametrize(
    "column, value",
    (("last_event_id", "event_invalid"), ("schema_version", 99)),
)
def test_invalid_persisted_run_fields_return_typed_storage_error(tmp_path, column, value) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create())
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        uow.connection.execute(
            f"UPDATE runs SET {column} = ? WHERE run_id = ?",  # noqa: S608 - test column allowlist
            (value, str(created.run_id)),
        )

    result = service.execute(
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=1,
            reason_code="user_cancelled",
        )
    )

    assert result.accepted is False
    assert result.code == "state_integrity_error"


@pytest.mark.parametrize("tamper", ["last_event", "revision", "result_hash", "snapshot_hash"])
def test_snapshot_and_result_metadata_tamper_fails_closed(tmp_path, tamper: str) -> None:
    clock = FrozenClock(datetime(2026, 9, 4, tzinfo=UTC))
    service = KernelApplicationService(tmp_path / "state.sqlite3", clock=clock)
    created = service.execute(CreateRun.create())
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        if tamper == "last_event":
            uow.connection.execute(
                "UPDATE runs SET last_event_id = ? WHERE run_id = ?",
                (str(new_id(EventId)), str(created.run_id)),
            )
        elif tamper == "revision":
            uow.connection.execute(
                "UPDATE runs SET revision = 2 WHERE run_id = ?", (str(created.run_id),)
            )
        elif tamper == "result_hash":
            uow.connection.execute(
                "UPDATE events SET result_hash = ? WHERE event_id = ?",
                ("0" * 64, str(created.event_id)),
            )
        else:
            uow.connection.execute(
                "UPDATE runs SET state_hash = ? WHERE run_id = ?",
                ("0" * 64, str(created.run_id)),
            )
        with pytest.raises(StateIntegrityError):
            uow.runs.get_verified(created.run_id, uow.events)
