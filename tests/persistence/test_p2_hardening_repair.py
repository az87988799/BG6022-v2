import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from orca_agent.application.effect_completion import EffectCompletionService
from orca_agent.application.errors import (
    DuplicateCommandConflictError,
    EffectInFlightError,
    StateIntegrityError,
)
from orca_agent.application.service import KernelApplicationService
from orca_agent.domain.hashing import GENESIS_EVENT_HASH, event_envelope_hash, sha256_hex
from orca_agent.domain.ids import EffectId, EventId, InterruptId, WorkerId, new_id
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.command_receipts import CommandBindingKind, CommandReceiptRepository
from orca_agent.infrastructure.outbox import OutboxStatus
from orca_agent.infrastructure.repositories import EventRepository, RunRepository, json_text
from orca_agent.infrastructure.sqlite import SQLiteConnectionFactory
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.commands import (
    CancelRun,
    CreateRun,
    RecordEffectSucceeded,
    RequestInterrupt,
)
from orca_agent.orchestration.effects import EffectClass, EffectSpec

BASE_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _effect(
    index: int = 0,
    *,
    effect_type: str = "external.test",
    effect_class: EffectClass = EffectClass.EXTERNAL,
) -> EffectSpec:
    return EffectSpec(
        effect_index=index,
        effect_type=effect_type,
        effect_class=effect_class,
        payload={"index": index},
    )


def _seed(tmp_path, *, effects: tuple[EffectSpec, ...] = (_effect(),)):
    clock = FrozenClock(BASE_TIME)
    database_path = tmp_path / "state.sqlite3"
    service = KernelApplicationService(database_path, clock=clock)
    created = service.execute(CreateRun.create(effects=effects, requested_at_utc=BASE_TIME))
    assert created.accepted
    with SQLiteUnitOfWork(database_path, clock=clock) as uow:
        effect_ids = tuple(
            EffectId(str(row[0]))
            for row in uow.connection.execute(
                "SELECT effect_id FROM outbox ORDER BY effect_index"
            ).fetchall()
        )
    return clock, database_path, service, created, effect_ids


def _claim(path, clock, worker_id, *, lease_duration=timedelta(seconds=30), limit=1):
    with SQLiteUnitOfWork(path, clock=clock) as uow:
        return uow.outbox.claim_due(
            worker_id=worker_id,
            now=clock.now_utc(),
            lease_duration=lease_duration,
            limit=limit,
        )


def _authorize_claimed(path, clock, claimed, worker_id):
    with SQLiteUnitOfWork(path, clock=clock) as uow:
        return uow.outbox.authorize_dispatch(
            runs=uow.runs,
            events=uow.events,
            interrupts=uow.interrupts,
            effect_id=claimed.effect_id,
            worker_id=worker_id,
            expected_generation=claimed.attempt_count,
            now=clock.now_utc(),
        )


def _authorize(path, clock, worker_id):
    claimed = _claim(path, clock, worker_id)[0]
    permit = _authorize_claimed(path, clock, claimed, worker_id)
    assert permit is not None
    return permit


def _cancel(service, run_id, clock, *, command_id=None):
    return service.execute(
        CancelRun.create(
            run_id=run_id,
            expected_revision=1,
            reason_code="user_cancelled",
            command_id=command_id,
            requested_at_utc=clock.now_utc(),
        )
    )


def test_cancel_before_claim_fences_handler(tmp_path) -> None:
    clock, database_path, service, created, effect_ids = _seed(tmp_path)
    cancelled = _cancel(service, created.run_id, clock)
    called: list[EffectId] = []

    report = OutboxWorker(
        database_path,
        lambda permit: (called.append(permit.effect.effect_id), HandlerResult(success=True))[1],
        clock=clock,
    ).run_once(limit=1)

    assert cancelled.accepted
    assert report == ()
    assert called == []
    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.outbox.get(effect_ids[0]).status is OutboxStatus.CANCELLED


def test_cancel_after_claim_before_authorize_fences_handler(tmp_path) -> None:
    clock, database_path, service, created, effect_ids = _seed(tmp_path)
    worker_id = new_id(WorkerId)
    claimed = _claim(database_path, clock, worker_id)[0]
    cancelled = _cancel(service, created.run_id, clock)
    permit = _authorize_claimed(database_path, clock, claimed, worker_id)

    assert cancelled.accepted
    assert permit is None
    with SQLiteUnitOfWork(database_path) as uow:
        record = uow.outbox.get(effect_ids[0])
        assert record.status is OutboxStatus.CANCELLED


def test_authorize_commit_blocks_cancel_without_new_event(tmp_path) -> None:
    clock, database_path, service, created, _ = _seed(tmp_path)
    permit = _authorize(database_path, clock, new_id(WorkerId))
    result = _cancel(service, created.run_id, clock)

    assert result.accepted is False
    assert result.code == EffectInFlightError.code
    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.runs.require(created.run_id).revision == 1
        assert uow.events.count_for_run(created.run_id) == 1
        assert uow.outbox.get(permit.effect.effect_id).status is OutboxStatus.DISPATCHING


def test_cancel_and_authorize_race_has_one_linearization_winner(tmp_path) -> None:
    clock, database_path, service, created, _ = _seed(tmp_path)
    worker_id = new_id(WorkerId)
    claimed = _claim(database_path, clock, worker_id)[0]
    barrier = Barrier(2)

    def cancel_command():
        barrier.wait(timeout=5)
        return _cancel(service, created.run_id, clock)

    def authorize_command():
        barrier.wait(timeout=5)
        try:
            return _authorize_claimed(database_path, clock, claimed, worker_id)
        except Exception as error:  # pragma: no cover - a failed race is asserted below
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_result, authorize_result = tuple(
            future.result()
            for future in (
                pool.submit(cancel_command),
                pool.submit(authorize_command),
            )
        )

    assert not (
        cancel_result.accepted
        and authorize_result is not None
        and not isinstance(authorize_result, Exception)
    )
    if cancel_result.accepted:
        assert authorize_result is None
    else:
        assert cancel_result.code == EffectInFlightError.code
        assert not isinstance(authorize_result, Exception)
        assert authorize_result is not None


def test_waiting_external_effect_is_not_claimed(tmp_path) -> None:
    clock, database_path, service, created, effect_ids = _seed(tmp_path)
    requested = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="approval",
            payload={"question": "continue?"},
            expires_at_utc=BASE_TIME + timedelta(minutes=1),
        )
    )
    called: list[EffectId] = []
    reports = OutboxWorker(
        database_path,
        lambda permit: (called.append(permit.effect.effect_id), HandlerResult(success=True))[1],
        clock=clock,
    ).run_once(limit=1)

    assert requested.accepted
    assert reports == ()
    assert called == []
    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.outbox.get(effect_ids[0]).status is OutboxStatus.PENDING


def test_waiting_safe_internal_effect_is_allowed(tmp_path) -> None:
    clock, database_path, service, created, effect_ids = _seed(
        tmp_path,
        effects=(
            _effect(
                effect_type="internal.audit",
                effect_class=EffectClass.INTERNAL,
            ),
        ),
    )
    requested = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="approval",
            payload={"question": "continue?"},
            expires_at_utc=BASE_TIME + timedelta(minutes=1),
        )
    )
    report = OutboxWorker(
        database_path,
        lambda _permit: HandlerResult(success=True),
        clock=clock,
    ).run_once(limit=1)

    assert requested.accepted
    assert report[0].outcome == "succeeded"
    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.outbox.get(effect_ids[0]).status is OutboxStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("effect_type", "effect_class"),
    (
        ("external.not_registered", EffectClass.EXTERNAL),
        ("internal.test", EffectClass.EXTERNAL),
    ),
)
def test_unregistered_or_mismatched_effect_is_fail_closed(
    tmp_path,
    effect_type: str,
    effect_class: EffectClass,
) -> None:
    clock, database_path, _, _, effect_ids = _seed(
        tmp_path,
        effects=(_effect(effect_type=effect_type, effect_class=effect_class),),
    )
    called: list[EffectId] = []
    reports = OutboxWorker(
        database_path,
        lambda permit: (called.append(permit.effect.effect_id), HandlerResult(success=True))[1],
        clock=clock,
    ).run_once(limit=1)

    assert reports == ()
    assert called == []
    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.outbox.get(effect_ids[0]).status is OutboxStatus.PENDING


def test_one_run_has_at_most_one_dispatching_effect(tmp_path) -> None:
    clock, database_path, _, created, effect_ids = _seed(
        tmp_path,
        effects=(
            _effect(0, effect_type="internal.test", effect_class=EffectClass.INTERNAL),
            _effect(1, effect_type="internal.test", effect_class=EffectClass.INTERNAL),
        ),
    )
    worker_id = new_id(WorkerId)
    claimed = _claim(database_path, clock, worker_id, limit=2)
    assert len(claimed) == 2
    first = _authorize_claimed(database_path, clock, claimed[0], worker_id)
    assert first is not None
    with pytest.raises(EffectInFlightError):
        _authorize_claimed(database_path, clock, claimed[1], worker_id)
    with SQLiteUnitOfWork(database_path) as uow:
        dispatching = [
            record
            for record in uow.outbox.list_for_run(created.run_id)
            if record.status is OutboxStatus.DISPATCHING
        ]
        assert len(dispatching) == 1
        assert effect_ids[0] != effect_ids[1]


def test_interrupt_transition_cannot_make_dispatching_internal_effect_illegal(tmp_path) -> None:
    clock, database_path, service, created, _ = _seed(
        tmp_path,
        effects=(_effect(effect_type="internal.test", effect_class=EffectClass.INTERNAL),),
    )
    _authorize(database_path, clock, new_id(WorkerId))
    result = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="approval",
            payload={"question": "continue?"},
            expires_at_utc=BASE_TIME + timedelta(minutes=1),
        )
    )

    assert result.accepted is False
    assert result.code == EffectInFlightError.code
    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.runs.require(created.run_id).revision == 1
        assert uow.events.count_for_run(created.run_id) == 1


class _FailNthCommitConnection:
    def __init__(self, connection, fail_on: int) -> None:
        self._connection = connection
        self._fail_on = fail_on
        self._commits = 0

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self) -> None:
        self._commits += 1
        if self._commits == self._fail_on:
            raise RuntimeError("simulated crash before commit")
        self._connection.commit()


class _FailNthCommitFactory:
    def __init__(self, database_path, fail_on: int) -> None:
        self._factory = SQLiteConnectionFactory(database_path)
        self._fail_on = fail_on

    def connect(self):
        return _FailNthCommitConnection(self._factory.connect(), self._fail_on)


def _assert_completion_rolled_back(path, run_id, effect_id) -> None:
    with SQLiteUnitOfWork(path) as uow:
        effect = uow.outbox.get(effect_id)
        assert effect is not None
        assert effect.status is OutboxStatus.DISPATCHING
        assert effect.audit_event_id is None
        assert uow.runs.require(run_id).revision == 1
        assert uow.events.count_for_run(run_id) == 1
        assert len(uow.command_receipts.list_for_run(run_id)) == 1


@pytest.mark.parametrize("success", (True, False))
@pytest.mark.parametrize(
    "fault",
    ("event_append", "terminal_receipt_update", "audit_binding", "run_cas", "commit"),
)
def test_terminal_completion_is_atomic_at_every_crash_point(
    tmp_path,
    monkeypatch,
    success: bool,
    fault: str,
) -> None:
    clock, database_path, _, created, effect_ids = _seed(tmp_path)
    permit = _authorize(database_path, clock, new_id(WorkerId))

    if fault == "event_append":
        original = EventRepository.append

        def append_then_fail(repository, event, *, command_hash):
            original(repository, event, command_hash=command_hash)
            raise RuntimeError("crash after event append")

        monkeypatch.setattr(EventRepository, "append", append_then_fail)
    elif fault in {"terminal_receipt_update", "audit_binding"}:
        from orca_agent.infrastructure.outbox import OutboxRepository

        original = OutboxRepository.complete_terminal_in_transaction

        def receipt_then_fail(repository, *args, **kwargs):
            original(repository, *args, **kwargs)
            raise RuntimeError("crash after terminal receipt and audit binding")

        monkeypatch.setattr(OutboxRepository, "complete_terminal_in_transaction", receipt_then_fail)
    elif fault == "run_cas":
        original = RunRepository.compare_and_swap

        def cas_then_fail(repository, *args, **kwargs):
            original(repository, *args, **kwargs)
            raise RuntimeError("crash after run CAS")

        monkeypatch.setattr(RunRepository, "compare_and_swap", cas_then_fail)

    completion = EffectCompletionService(
        database_path,
        clock=clock,
        max_attempts=1,
        connection_factory=(
            _FailNthCommitFactory(database_path, fail_on=2) if fault == "commit" else None
        ),
    )
    with pytest.raises(RuntimeError):
        completion.complete(permit, HandlerResult(success=success))

    _assert_completion_rolled_back(database_path, created.run_id, effect_ids[0])


def test_command_receipt_insert_rolls_back_event_and_projections(tmp_path, monkeypatch) -> None:
    clock = FrozenClock(BASE_TIME)
    database_path = tmp_path / "state.sqlite3"
    service = KernelApplicationService(database_path, clock=clock)
    original = CommandReceiptRepository.append_event

    def receipt_then_fail(repository, *, event, recorded_at_utc):
        original(repository, event=event, recorded_at_utc=recorded_at_utc)
        raise RuntimeError("receipt crash")

    monkeypatch.setattr(CommandReceiptRepository, "append_event", receipt_then_fail)
    command = CreateRun.create(
        requested_at_utc=BASE_TIME,
        effects=(_effect(effect_type="internal.audit", effect_class=EffectClass.INTERNAL),),
    )
    with pytest.raises(RuntimeError):
        service.execute(command)

    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.runs.get(command.run_id) is None
        assert uow.events.count_for_run(command.run_id) == 0
        assert uow.outbox.count() == 0
        assert uow.command_receipts.list_for_run(command.run_id) == ()


def test_two_new_audit_commands_concurrently_create_alias_receipts_only(tmp_path) -> None:
    clock, database_path, service, created, effect_ids = _seed(tmp_path)
    worker = OutboxWorker(database_path, lambda _permit: HandlerResult(success=True), clock=clock)
    assert worker.run_once(limit=1)[0].outcome == "succeeded"
    commands = tuple(
        RecordEffectSucceeded.create(
            run_id=created.run_id,
            expected_revision=1,
            effect_id=effect_ids[0],
            result_summary={
                "receipt_schema": "effect-success/v1",
                "outcome_code": "completed",
                "artifact_ids": [],
            },
            requested_at_utc=clock.now_utc(),
        )
        for _ in range(2)
    )
    barrier = Barrier(2)

    def execute(command):
        barrier.wait(timeout=5)
        return KernelApplicationService(database_path, clock=clock).execute(command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.submit(execute, command) for command in commands)
        results = tuple(future.result() for future in results)

    assert all(result.accepted for result in results)
    assert results[0] == results[1]
    with SQLiteUnitOfWork(database_path) as uow:
        assert uow.events.count_for_run(created.run_id) == 2
        receipts = uow.command_receipts.list_for_run(created.run_id)
        assert len(receipts) == 4
        assert (
            sum(
                receipt.binding_kind is CommandBindingKind.EFFECT_AUDIT_ALIAS
                for receipt in receipts
            )
            == 2
        )


def test_alias_command_id_cannot_be_reused_for_cancel(tmp_path) -> None:
    clock, database_path, service, created, effect_ids = _seed(tmp_path)
    assert (
        OutboxWorker(
            database_path,
            lambda _permit: HandlerResult(success=True),
            clock=clock,
        )
        .run_once(limit=1)[0]
        .outcome
        == "succeeded"
    )
    alias = RecordEffectSucceeded.create(
        run_id=created.run_id,
        expected_revision=1,
        effect_id=effect_ids[0],
        result_summary={
            "receipt_schema": "effect-success/v1",
            "outcome_code": "completed",
            "artifact_ids": [],
        },
        requested_at_utc=clock.now_utc(),
    )
    assert service.execute(alias).accepted
    reused = _cancel(service, created.run_id, clock, command_id=alias.command_id)
    assert reused.accepted is False
    assert reused.code == DuplicateCommandConflictError.code


def test_known_event_with_missing_run_is_state_integrity_error(tmp_path) -> None:
    clock, database_path, service, created, _ = _seed(tmp_path)
    with SQLiteUnitOfWork(database_path) as uow:
        stored = uow.events.list_for_run(created.run_id)[0].event
    command = CreateRun.create(
        run_id=created.run_id,
        command_id=stored.command_id,
        requested_at_utc=BASE_TIME,
        effects=(_effect(),),
    )
    with SQLiteUnitOfWork(database_path) as uow:
        uow.connection.execute("PRAGMA foreign_keys = OFF")
        uow.connection.execute("DELETE FROM runs WHERE run_id = ?", (str(created.run_id),))

    result = service.execute(command)
    assert result.accepted is False
    assert result.code == StateIntegrityError.code


def _rewrite_result_chain(connection, run_id, target_sequence: int, mutate) -> None:
    rows = connection.execute(
        "SELECT event_id, command_id, command_type, command_hash, run_id, sequence_no, "
        "expected_revision, new_revision, event_type, schema_version, engine_version, "
        "payload_json, payload_hash, result_json, result_hash, occurred_at_utc, "
        "recorded_at_utc FROM events WHERE run_id = ? ORDER BY sequence_no",
        (str(run_id),),
    ).fetchall()
    previous_hash = GENESIS_EVENT_HASH
    for row in rows:
        result = json.loads(str(row[13]))
        if int(row[5]) == target_sequence:
            mutate(result)
        payload = json.loads(str(row[11]))
        result_hash = sha256_hex(result)
        event_hash = event_envelope_hash(
            event_id=str(row[0]),
            previous_event_hash=previous_hash,
            command_id=str(row[1]),
            command_type=str(row[2]),
            command_hash=str(row[3]),
            run_id=str(row[4]),
            sequence_no=int(row[5]),
            expected_revision=int(row[6]),
            new_revision=int(row[7]),
            event_type=str(row[8]),
            schema_version=int(row[9]),
            engine_version=str(row[10]),
            payload=payload,
            payload_hash=str(row[12]),
            result=result,
            result_hash=result_hash,
            occurred_at_utc=str(row[15]),
            recorded_at_utc=str(row[16]),
        )
        connection.execute(
            "UPDATE events SET result_json = ?, result_hash = ?, previous_event_hash = ?, "
            "event_hash = ? WHERE event_id = ?",
            (json_text(result), result_hash, previous_hash, event_hash, str(row[0])),
        )
        previous_hash = event_hash


@pytest.mark.parametrize(
    "field",
    ("accepted", "code", "status", "details", "revision", "event_id", "interrupt_id"),
)
def test_coherent_result_tamper_is_rejected_by_replay(tmp_path, field: str) -> None:
    clock, database_path, service, created, _ = _seed(tmp_path, effects=())
    requested = service.execute(
        RequestInterrupt.create(
            run_id=created.run_id,
            expected_revision=1,
            kind="approval",
            payload={"question": "continue?"},
            expires_at_utc=BASE_TIME + timedelta(minutes=1),
        )
    )

    def mutate(result):
        if field == "accepted":
            result[field] = False
        elif field == "code":
            result[field] = "tampered"
        elif field == "status":
            result[field] = "ready"
        elif field == "details":
            result[field] = {"tampered": True}
        elif field == "revision":
            result[field] = 1
        elif field == "event_id":
            result[field] = str(new_id(EventId))
        else:
            result[field] = str(new_id(InterruptId))

    with SQLiteUnitOfWork(database_path) as uow:
        _rewrite_result_chain(uow.connection, created.run_id, 2, mutate)

    rejected = service.execute(
        CancelRun.create(
            run_id=created.run_id,
            expected_revision=2,
            reason_code="user_cancelled",
            requested_at_utc=clock.now_utc(),
        )
    )
    assert requested.accepted
    assert rejected.accepted is False
    assert rejected.code == StateIntegrityError.code


def test_coherent_cross_run_interrupt_id_swap_is_rejected(tmp_path) -> None:
    clock = FrozenClock(BASE_TIME)
    database_path = tmp_path / "state.sqlite3"
    service = KernelApplicationService(database_path, clock=clock)
    first = service.execute(CreateRun.create(requested_at_utc=BASE_TIME))
    first_request = service.execute(
        RequestInterrupt.create(
            run_id=first.run_id,
            expected_revision=1,
            kind="input",
            payload={"question": "first"},
            expires_at_utc=BASE_TIME + timedelta(minutes=1),
        )
    )
    second = service.execute(CreateRun.create(requested_at_utc=BASE_TIME))
    second_request = service.execute(
        RequestInterrupt.create(
            run_id=second.run_id,
            expected_revision=1,
            kind="input",
            payload={"question": "second"},
            expires_at_utc=BASE_TIME + timedelta(minutes=1),
        )
    )

    with SQLiteUnitOfWork(database_path) as uow:
        _rewrite_result_chain(
            uow.connection,
            first.run_id,
            2,
            lambda result: result.update(interrupt_id=str(second_request.interrupt_id)),
        )
    rejected = service.execute(
        CancelRun.create(
            run_id=first.run_id,
            expected_revision=2,
            reason_code="user_cancelled",
            requested_at_utc=clock.now_utc(),
        )
    )

    assert first_request.accepted and second_request.accepted
    assert rejected.code == StateIntegrityError.code


def test_v4_migration_failure_rolls_back_schema_and_metadata(tmp_path) -> None:
    from orca_agent.infrastructure.migrations import (
        DEFAULT_MIGRATIONS,
        Migration,
        apply_migrations,
    )

    path = tmp_path / "state.sqlite3"
    connection = SQLiteConnectionFactory(path).connect()

    def fail_post_apply(_connection):
        raise RuntimeError("v4 validation crash")

    broken_v4 = Migration(
        version=DEFAULT_MIGRATIONS[3].version,
        name=DEFAULT_MIGRATIONS[3].name,
        statements=DEFAULT_MIGRATIONS[3].statements,
        post_apply=fail_post_apply,
        post_apply_id=DEFAULT_MIGRATIONS[3].post_apply_id,
    )
    try:
        assert apply_migrations(connection, migrations=DEFAULT_MIGRATIONS[:3]) == 3
        with pytest.raises(RuntimeError):
            apply_migrations(connection, migrations=(*DEFAULT_MIGRATIONS[:3], broken_v4))
        assert [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ] == [1, 2, 3]
        assert "dispatch_run_revision" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(outbox)").fetchall()
        }
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'command_receipts'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()
