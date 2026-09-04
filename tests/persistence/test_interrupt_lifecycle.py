from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.service import KernelApplicationService
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import (
    CancelRun,
    CreateRun,
    ExpireInterrupt,
    ReplaceInterrupt,
    RequestInterrupt,
    ResolveInterrupt,
)
from orca_agent.orchestration.state import RunStatus

BASE_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _service(tmp_path):
    return KernelApplicationService(tmp_path / "state.sqlite3", clock=FrozenClock(BASE_TIME))


def _created(service: KernelApplicationService):
    result = service.execute(CreateRun.create())
    assert result.accepted
    return result


def _request(service: KernelApplicationService, run_id, revision: int, expiry_offset: int = 60):
    return RequestInterrupt.create(
        run_id=run_id,
        expected_revision=revision,
        kind="approval",
        payload={"question": "continue?"},
        expires_at_utc=BASE_TIME + timedelta(seconds=expiry_offset),
        requested_at_utc=BASE_TIME,
    )


def test_request_replace_and_resolve_use_exact_projection_lifecycle(tmp_path) -> None:
    service = _service(tmp_path)
    created = _created(service)
    requested = service.execute(_request(service, created.run_id, 1))
    assert requested.accepted
    assert requested.status is RunStatus.WAITING_FOR_INPUT

    replacement = ReplaceInterrupt.create(
        run_id=created.run_id,
        expected_revision=2,
        old_interrupt_id=requested.interrupt_id,
        kind="approval",
        payload={"question": "continue with replacement?"},
        expires_at_utc=BASE_TIME + timedelta(minutes=2),
        requested_at_utc=BASE_TIME,
    )
    replaced = service.execute(replacement)
    assert replaced.accepted
    assert replaced.interrupt_id == replacement.new_interrupt_id

    resolved = service.execute(
        ResolveInterrupt.create(
            run_id=created.run_id,
            expected_revision=3,
            interrupt_id=replacement.new_interrupt_id,
            response={"approved": True},
            requested_at_utc=BASE_TIME,
        )
    )
    assert resolved.accepted
    assert resolved.status is RunStatus.READY

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        old = uow.interrupts.get(requested.interrupt_id)
        new = uow.interrupts.get(replacement.new_interrupt_id)
        assert old.status.value == "superseded"
        assert old.superseded_by == replacement.new_interrupt_id
        assert new.status.value == "resolved"
        assert new.response["approved"] is True
        assert uow.interrupts.get_pending_for_run(created.run_id) is None
        assert uow.runs.require(created.run_id).state.status is RunStatus.READY


def test_second_plain_request_is_rejected_by_service_and_database_guard(tmp_path) -> None:
    service = _service(tmp_path)
    created = _created(service)
    first = service.execute(_request(service, created.run_id, 1))
    second = service.execute(_request(service, created.run_id, 2))

    assert first.accepted
    assert second.accepted is False
    assert second.code == "interrupt_already_pending"
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.interrupts.count_for_run(created.run_id) == 1


def test_resolve_at_deadline_persists_expiry_and_returns_typed_rejection(tmp_path) -> None:
    service = _service(tmp_path)
    created = _created(service)
    requested = service.execute(_request(service, created.run_id, 1, expiry_offset=5))
    assert requested.accepted
    service.clock.advance(timedelta(seconds=5))

    result = service.execute(
        ResolveInterrupt.create(
            run_id=created.run_id,
            expected_revision=2,
            interrupt_id=requested.interrupt_id,
            response={"approved": True},
            requested_at_utc=BASE_TIME,
        )
    )

    assert result.accepted is False
    assert result.code == "interrupt_expired"
    assert result.revision == 3
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        record = uow.interrupts.get(requested.interrupt_id)
        assert record.status.value == "expired"
        assert record.response is None
        assert uow.interrupts.get_pending_for_run(created.run_id) is None
        assert uow.runs.require(created.run_id).state.status is RunStatus.READY


def test_explicit_expiry_requires_deadline_and_due_sweep_is_deterministic(tmp_path) -> None:
    service = _service(tmp_path)
    created = _created(service)
    requested = service.execute(_request(service, created.run_id, 1, expiry_offset=5))
    assert requested.accepted

    too_early = service.execute(
        ExpireInterrupt.create(
            run_id=created.run_id,
            expected_revision=2,
            interrupt_id=requested.interrupt_id,
            requested_at_utc=BASE_TIME,
        )
    )
    assert too_early.accepted is False
    assert too_early.code == "interrupt_not_expired"
    service.clock.advance(timedelta(seconds=5))
    summary = service.expire_due(limit=10)
    assert summary == (requested.interrupt_id,)

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.interrupts.get(requested.interrupt_id).status.value == "expired"


def test_cancel_atomically_clears_pending_interrupt(tmp_path) -> None:
    service = _service(tmp_path)
    created = _created(service)
    requested = service.execute(_request(service, created.run_id, 1))
    cancelled = service.execute(
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=2,
            reason_code="user_cancelled",
            requested_at_utc=BASE_TIME,
        )
    )

    assert cancelled.accepted
    assert cancelled.status is RunStatus.CANCELLED
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.interrupts.get(requested.interrupt_id).status.value == "cancelled"
        assert uow.interrupts.get_pending_for_run(created.run_id) is None


def test_stale_revision_is_safe_typed_result_and_restart_preserves_state(tmp_path) -> None:
    service = _service(tmp_path)
    created = _created(service)
    requested = service.execute(_request(service, created.run_id, 1))
    stale = service.execute(_request(service, created.run_id, 1))
    assert requested.accepted
    assert stale.accepted is False
    assert stale.code == "revision_conflict"
    assert stale.revision == 2

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        snapshot = uow.runs.get_verified(created.run_id, uow.events)
        assert snapshot.state.pending_interrupt_id == requested.interrupt_id


def test_projection_hash_tamper_fails_closed(tmp_path) -> None:
    service = _service(tmp_path)
    created = _created(service)
    requested = service.execute(_request(service, created.run_id, 1))
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        uow.connection.execute(
            "UPDATE interrupts SET payload_hash = ? WHERE interrupt_id = ?",
            ("0" * 64, str(requested.interrupt_id)),
        )
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        with pytest.raises(StateIntegrityError):
            uow.interrupts.get(requested.interrupt_id)
