import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.ids import EffectId, InterruptId, WorkerId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.outbox import OutboxStatus
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import (
    CancelRun,
    CreateRun,
    ExpireInterrupt,
    RecordEffectSucceeded,
    ReplaceInterrupt,
    RequestInterrupt,
    ResolveInterrupt,
)
from orca_agent.orchestration.effects import EffectClass, EffectSpec
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


def test_partial_unique_pending_index_rejects_direct_duplicate_insert(tmp_path) -> None:
    service = _service(tmp_path)
    created = _created(service)
    requested = service.execute(_request(service, created.run_id, 1))

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        index_sql = uow.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'one_pending_interrupt_per_run'"
        ).fetchone()[0]
        assert "UNIQUE INDEX" in index_sql.upper()
        assert "WHERE status = 'pending'" in index_sql
        row = uow.connection.execute(
            "SELECT run_id, kind, schema_version, engine_version, request_event_id, "
            "payload_json, payload_hash, created_at_utc, expires_at_utc "
            "FROM interrupts WHERE interrupt_id = ?",
            (str(requested.interrupt_id),),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            uow.connection.execute(
                "INSERT INTO interrupts(interrupt_id, run_id, kind, status, schema_version, "
                "engine_version, request_event_id, terminal_event_id, payload_json, "
                "payload_hash, response_json, response_hash, created_at_utc, expires_at_utc, "
                "terminal_at_utc, superseded_by) VALUES (?, ?, ?, 'pending', ?, ?, ?, NULL, "
                "?, ?, NULL, NULL, ?, ?, NULL, NULL)",
                (
                    str(new_id(InterruptId)),
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                ),
            )


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


def test_deleted_pending_interrupt_blocks_effect_progress(tmp_path) -> None:
    service = _service(tmp_path)
    created = service.execute(
        CreateRun.create(
            effects=(
                EffectSpec(
                    effect_index=0,
                    effect_type="internal.audit",
                    effect_class=EffectClass.INTERNAL,
                    payload={"source": "test"},
                ),
            )
        )
    )
    requested = service.execute(_request(service, created.run_id, 1))
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3", clock=service.clock) as uow:
        effect_row = uow.connection.execute("SELECT effect_id FROM outbox").fetchone()
        effect_id = EffectId(str(effect_row[0]))
        worker_id = WorkerId("worker_00000000000000000000000000000000")
        claimed = uow.outbox.claim_due(
            worker_id=worker_id,
            now=service.clock.now_utc(),
            lease_duration=timedelta(seconds=30),
            limit=1,
        )[0]
        assert claimed.effect_id == effect_id
        uow.outbox.mark_succeeded(
            effect_id=effect_id,
            worker_id=worker_id,
            now=service.clock.now_utc(),
        )
        uow.connection.execute(
            "DELETE FROM interrupts WHERE interrupt_id = ?", (str(requested.interrupt_id),)
        )

    result = service.execute(
        RecordEffectSucceeded.create(
            run_id=created.run_id,
            expected_revision=2,
            effect_id=effect_id,
            result_summary={"accepted": True},
        )
    )

    assert result.accepted is False
    assert result.code == "state_integrity_error"
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.runs.require(created.run_id).revision == 2
        assert uow.outbox.get(effect_id).status is OutboxStatus.SUCCEEDED


def test_expiry_tamper_is_rejected_by_due_sweep(tmp_path) -> None:
    service = _service(tmp_path)
    created = _created(service)
    requested = service.execute(_request(service, created.run_id, 1, expiry_offset=5))
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        uow.connection.execute(
            "UPDATE interrupts SET expires_at_utc = ? WHERE interrupt_id = ?",
            ("2026-09-04T01:00:00.000000Z", str(requested.interrupt_id)),
        )

    with pytest.raises(StateIntegrityError):
        service.expire_due(limit=10)


def test_interrupt_request_event_cross_run_tamper_fails_closed(tmp_path) -> None:
    service = _service(tmp_path)
    first = _created(service)
    first_interrupt = service.execute(_request(service, first.run_id, 1))
    second = _created(service)
    second_interrupt = service.execute(_request(service, second.run_id, 1))
    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        second_event_id = uow.connection.execute(
            "SELECT request_event_id FROM interrupts WHERE interrupt_id = ?",
            (str(second_interrupt.interrupt_id),),
        ).fetchone()[0]
        uow.connection.execute("PRAGMA foreign_keys = OFF")
        uow.connection.execute(
            "UPDATE interrupts SET request_event_id = ? WHERE interrupt_id = ?",
            (second_event_id, str(first_interrupt.interrupt_id)),
        )

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        with pytest.raises(StateIntegrityError):
            uow.interrupts.get(first_interrupt.interrupt_id)


def test_composite_run_ownership_foreign_keys_reject_cross_run_links(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.execute(
        CreateRun.create(
            effects=(
                EffectSpec(
                    effect_index=0,
                    effect_type="internal.audit",
                    effect_class=EffectClass.INTERNAL,
                    payload={"source": "first"},
                ),
            )
        )
    )
    second = _created(service)
    first_interrupt = service.execute(_request(service, first.run_id, 1))
    second_interrupt = service.execute(_request(service, second.run_id, 1))

    with SQLiteUnitOfWork(tmp_path / "state.sqlite3") as uow:
        assert uow.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        effect_id = uow.connection.execute("SELECT effect_id FROM outbox").fetchone()[0]
        second_event_id = uow.connection.execute(
            "SELECT request_event_id FROM interrupts WHERE interrupt_id = ?",
            (str(second_interrupt.interrupt_id),),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            uow.connection.execute(
                "UPDATE outbox SET run_id = ? WHERE effect_id = ?",
                (str(second.run_id), effect_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            uow.connection.execute(
                "UPDATE interrupts SET request_event_id = ? WHERE interrupt_id = ?",
                (second_event_id, str(first_interrupt.interrupt_id)),
            )
