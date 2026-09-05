from datetime import UTC, datetime, timedelta

import pytest
from test_boundaries import _authorize_effect
from test_vertical_slice import _approval_command

from orca_agent.application.errors import StateIntegrityError
from orca_agent.application.p3_service import P3ApplicationService
from orca_agent.domain.p3 import LedgerState
from orca_agent.execution.commands import CancelWaterRun
from orca_agent.execution.gateway import FakeExecutionGateway
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.clock import FrozenClock
from orca_agent.infrastructure.p3_records import JobRepository


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/outside",
        "C:/outside",
        "C:outside",
        "C:\\outside",
        "\\\\server\\share\\file",
        "a\\..\\outside",
    ],
)
def test_nonportable_artifact_paths(tmp_path, path):
    with pytest.raises(StateIntegrityError):
        ArtifactStore(tmp_path).path_for(path)


def prepared(tmp_path, max_attempts=3):
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    service = P3ApplicationService(tmp_path / "state", clock=clock, max_attempts=max_attempts)
    started = service.start()
    assert service.approve(_approval_command(service, started)).accepted
    return service, clock, started


def cancel(service, started):
    return service.cancel(
        CancelWaterRun.create(
            run_id=started.run_id,
            conversation_id=started.conversation_id,
            expected_revision=service.inspect(started.run_id).revision,
            requested_at_utc=service.clock.now_utc(),
        )
    )


def test_old_generation_cannot_start_backend(tmp_path):
    service, clock, started = prepared(tmp_path)
    effect = service.inspect(started.run_id).state.dispatch_effect_id
    _, old = _authorize_effect(service, effect, clock, 1)
    clock.advance(timedelta(minutes=2))
    _, current = _authorize_effect(service, effect, clock, 2)
    gateway = FakeExecutionGateway(service.database_path, service.state_root, clock=clock)
    assert not gateway.execute(old).success
    assert service.backend.execution_count() == 0
    assert service.inspect(started.run_id).ledger_state is LedgerState.APPROVED
    assert gateway.execute(current).success


def test_backend_return_after_reclaim_cannot_persist_old_generation(tmp_path, monkeypatch):
    service, clock, started = prepared(tmp_path)
    effect = service.inspect(started.run_id).state.dispatch_effect_id
    _, old = _authorize_effect(service, effect, clock, 1)
    gateway = FakeExecutionGateway(service.database_path, service.state_root, clock=clock)
    original = gateway.backend.submit_or_get
    permits = []

    def reclaim(**kwargs):
        result = original(**kwargs)
        clock.advance(timedelta(minutes=2))
        permits.append(_authorize_effect(service, effect, clock, 2)[1])
        return result

    monkeypatch.setattr(gateway.backend, "submit_or_get", reclaim)
    assert not gateway.execute(old).success
    assert service.inspect(started.run_id).ledger_state is LedgerState.SUBMITTING
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

    with SQLiteUnitOfWork(service.database_path) as uow:
        assert uow.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    monkeypatch.setattr(gateway.backend, "submit_or_get", original)
    assert gateway.execute(permits[0]).success
    assert service.backend.execution_count() == 1


@pytest.mark.parametrize("exhausted", [False, True])
def test_unknown_execution_preserved_and_recoverable_after_grant_expiry(
    tmp_path, monkeypatch, exhausted
):
    service, clock, started = prepared(tmp_path, max_attempts=1 if exhausted else 3)
    original = JobRepository.insert

    def fail(*args, **kwargs):
        raise RuntimeError("before job persistence")

    monkeypatch.setattr(JobRepository, "insert", fail)
    worker = service.create_worker()
    assert worker.run_once(limit=1)[0].outcome == ("dead_letter" if exhausted else "retry")
    assert service.backend.execution_count() == 1
    assert service.inspect(started.run_id).ledger_state is LedgerState.SUBMITTING
    assert not cancel(service, started).accepted
    assert service.inspect(started.run_id).diagnostics == ("execution_reconciliation_required",)
    monkeypatch.setattr(JobRepository, "insert", original)
    clock.advance(timedelta(days=2))
    if exhausted:
        assert worker.run_once(limit=3) == ()
    else:
        assert [r.outcome for r in worker.run_once(limit=3)] == ["succeeded"] * 3
    assert service.backend.execution_count() == 1


def test_cancel_before_dispatch_and_expired_unsubmitted_grant(tmp_path):
    service, clock, started = prepared(tmp_path)
    assert cancel(service, started).accepted
    assert service.create_worker().run_once(limit=1) == ()
    assert service.backend.execution_count() == 0
    other = service.start()
    assert service.approve(_approval_command(service, other)).accepted
    clock.advance(timedelta(days=2))
    assert service.create_worker().run_once(limit=1)[0].outcome == "retry"
    assert service.backend.execution_count() == 0


@pytest.mark.parametrize("damage", ["before_render", "raw", "md", "json"])
def test_report_damage_cannot_commit_and_recovery_does_not_execute_again(
    tmp_path, monkeypatch, damage
):
    from orca_agent.infrastructure.p3_records import ArtifactRecordRepository
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
    from orca_agent.reporting.renderer import P3ReportRenderer

    service, clock, started = prepared(tmp_path)
    worker = service.create_worker()
    assert [r.outcome for r in worker.run_once(limit=2)] == ["succeeded"] * 2
    before = service.inspect(started.run_id)
    with SQLiteUnitOfWork(service.database_path) as uow:
        raw = JobRepository(uow.connection).get_by_execution(before.state.execution_id)
        raw_record = ArtifactRecordRepository(uow.connection).get(raw.raw_result_artifact_id)
    damaged = []

    def corrupt(artifact_id):
        with SQLiteUnitOfWork(service.database_path) as uow:
            record = ArtifactRecordRepository(uow.connection).get(artifact_id)
        path = ArtifactStore(service.state_root).path_for(record.relative_path)
        damaged.append((path, path.read_bytes()))
        path.write_bytes(b"corrupted")

    original = P3ReportRenderer.render

    def render(renderer, permit):
        result = original(renderer, permit)
        corrupt(
            raw_record.artifact_id
            if damage == "raw"
            else result.result_summary.artifact_ids[0 if damage == "md" else 1]
        )
        return result

    if damage == "before_render":
        corrupt(raw_record.artifact_id)
    else:
        monkeypatch.setattr(P3ReportRenderer, "render", render)
    if damage == "before_render":
        assert worker.run_once(limit=1)[0].outcome == "retry"
    else:
        with pytest.raises(StateIntegrityError):
            worker.run_once(limit=1)
    assert service.inspect(started.run_id).revision == before.revision
    for path, content in damaged:
        path.write_bytes(content)
    monkeypatch.setattr(P3ReportRenderer, "render", original)
    clock.advance(timedelta(minutes=2))
    assert worker.run_once(limit=1)[0].outcome == "succeeded"
    assert service.backend.execution_count() == 1


def cli_process(tmp_path, state_root, *args):
    import os
    import subprocess
    import sys

    guard = tmp_path / "child_guard"
    guard.mkdir(exist_ok=True)
    (guard / "sitecustomize.py").write_text(
        "import pytest_socket; pytest_socket.disable_socket()\n", encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(guard) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "orca_agent", "--state-root", str(state_root), *map(str, args)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def test_cli_export_requires_run_and_exact_bytes(tmp_path):
    import json

    service, _, started = prepared(tmp_path)
    assert [r.outcome for r in service.create_worker().run_once(limit=3)] == ["succeeded"] * 3
    root = tmp_path / "must_not_exist"
    rejected = cli_process(tmp_path, root, "verify-report", "--report", tmp_path / "marker.md")
    assert rejected.returncode == 2 and not root.exists()
    for extension in ("md", "json"):
        exported = tmp_path / f"report.{extension}"
        assert (
            cli_process(
                tmp_path,
                service.state_root,
                "report",
                "--run",
                started.run_id,
                "--format",
                extension,
                "--output",
                exported,
            ).returncode
            == 0
        )
        good = cli_process(
            tmp_path,
            service.state_root,
            "verify-report",
            "--run",
            started.run_id,
            "--report",
            exported,
        )
        assert good.returncode == 0 and json.loads(good.stdout)["valid"]
        exported.write_text(
            "FAKE FIXTURE ONLY"
            if extension == "md"
            else json.dumps(
                {
                    "fake_marker": "fake_fixture_only",
                    "data_origin": "fake_fixture",
                    "backend": "fake",
                    "real_scientific_result": False,
                }
            ),
            encoding="utf-8",
        )
        bad = cli_process(
            tmp_path,
            service.state_root,
            "verify-report",
            "--run",
            started.run_id,
            "--report",
            exported,
        )
        assert bad.returncode != 0 and json.loads(bad.stdout)["valid"] is False


@pytest.mark.parametrize("expire", [False, True])
def test_approval_and_expiry_write_failure_rolls_back_all_projections(
    tmp_path, monkeypatch, expire
):
    from orca_agent.infrastructure.p3_records import P3RecordRepository
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

    service = P3ApplicationService(
        tmp_path / "state", clock=FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    )
    started = service.start()
    command = _approval_command(service, started)
    if expire:
        service.clock.advance(timedelta(hours=24))

    def counts():
        with SQLiteUnitOfWork(service.database_path) as uow:
            return tuple(
                uow.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("events", "outbox", "command_receipts", "workflow_records")
            )

    before = counts()
    original = P3RecordRepository.append

    def fail_after_write(repository, **kwargs):
        result = original(repository, **kwargs)
        if kwargs["record_type"] == "workflow_state":
            raise RuntimeError("after workflow record write, before commit")
        return result

    monkeypatch.setattr(P3RecordRepository, "append", fail_after_write)
    assert not service.approve(command).accepted
    assert counts() == before
    assert service.inspect(started.run_id).ledger_state is LedgerState.PLANNED
    monkeypatch.setattr(P3RecordRepository, "append", original)
    first = service.approve(command)
    assert first.accepted is not expire
    assert service.approve(command) == first
    if expire:
        assert first.code == "interrupt_expired" and first.revision == 3
        assert dict(first.details) == {}
        changed = command.model_copy(update={"action_hash": "0" * 64})
        assert service.approve(changed).code == "duplicate_command_conflict"
        assert service.inspect(started.run_id).outbox == ()


def test_command_public_result_replay_after_completion_in_new_process(tmp_path):
    import json

    from orca_agent.execution.commands import StartWaterRun

    service = P3ApplicationService(tmp_path / "state")
    start = StartWaterRun.create()
    first_start = service.start(start)
    approve = _approval_command(service, first_start)
    first_approve = service.approve(approve)
    assert [r.outcome for r in service.create_worker().run_once(limit=3)] == ["succeeded"] * 3
    for name, command, expected in (
        ("start", start, first_start),
        ("approve", approve, first_approve),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(command.model_dump_json(), encoding="utf-8")
        result = cli_process(tmp_path, service.state_root, "replay-request", "--file", path)
        assert result.returncode == 0
        assert json.loads(result.stdout) == expected.model_dump(mode="json")
    assert service.backend.execution_count() == 1


def test_cancel_replays_the_whole_original_result(tmp_path):
    service, _, started = prepared(tmp_path)
    command = CancelWaterRun.create(
        run_id=started.run_id,
        conversation_id=started.conversation_id,
        expected_revision=service.inspect(started.run_id).revision,
    )
    result = service.cancel(command)
    assert result.accepted
    assert service.cancel(command) == result


def test_submission_transaction_wins_concurrent_cancel(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from orca_agent.infrastructure.p3_records import ActionRepository
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

    service, clock, started = prepared(tmp_path)
    effect = service.inspect(started.run_id).state.dispatch_effect_id
    _, permit = _authorize_effect(service, effect, clock, 1)
    gateway = FakeExecutionGateway(service.database_path, service.state_root, clock=clock)
    written, cancel_attempted = Event(), Event()
    original_update, original_begin = ActionRepository.update_ledger, SQLiteUnitOfWork.__enter__

    def update(repository, **kwargs):
        result = original_update(repository, **kwargs)
        if kwargs["state"] is LedgerState.SUBMITTING:
            written.set()
            assert cancel_attempted.wait(5)
        return result

    def begin(uow):
        if current_thread().name.startswith("cancel"):
            cancel_attempted.set()
        return original_begin(uow)

    monkeypatch.setattr(ActionRepository, "update_ledger", update)
    monkeypatch.setattr(SQLiteUnitOfWork, "__enter__", begin)
    command = CancelWaterRun.create(
        run_id=started.run_id, conversation_id=started.conversation_id, expected_revision=3
    )
    with (
        ThreadPoolExecutor(1, thread_name_prefix="gateway") as dispatch_pool,
        ThreadPoolExecutor(1, thread_name_prefix="cancel") as cancel_pool,
    ):
        running = dispatch_pool.submit(gateway.execute, permit)
        assert written.wait(5)
        cancelling = cancel_pool.submit(service.cancel, command)
        assert running.result(timeout=10).success
        assert cancelling.result(timeout=10).code == "effect_in_flight"
    assert service.backend.execution_count() == 1


def test_report_completion_receipt_write_failure_rolls_back(tmp_path, monkeypatch):
    from orca_agent.infrastructure.command_receipts import CommandReceiptRepository
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

    service, clock, started = prepared(tmp_path)
    worker = service.create_worker()
    assert [r.outcome for r in worker.run_once(limit=2)] == ["succeeded"] * 2
    before = service.inspect(started.run_id)
    original = CommandReceiptRepository.append_event

    def fail(repository, **kwargs):
        original(repository, **kwargs)
        raise RuntimeError("after completion receipt write")

    monkeypatch.setattr(CommandReceiptRepository, "append_event", fail)
    with pytest.raises(RuntimeError):
        worker.run_once(limit=1)
    assert service.inspect(started.run_id).revision == before.revision
    assert service.inspect(started.run_id).ledger_state is LedgerState.SUBMITTED
    with SQLiteUnitOfWork(service.database_path) as uow:
        assert (
            uow.connection.execute("SELECT count(*) FROM command_receipts").fetchone()[0]
            == before.revision
        )
    monkeypatch.setattr(CommandReceiptRepository, "append_event", original)
    clock.advance(timedelta(minutes=2))
    assert worker.run_once(limit=1)[0].outcome == "succeeded"
    assert service.backend.execution_count() == 1


def test_cancel_transaction_wins_concurrent_worker(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from orca_agent.infrastructure.p3_records import ActionRepository
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

    service, _, started = prepared(tmp_path)
    worker = service.create_worker()
    written, worker_attempted = Event(), Event()
    original_update, original_enter = ActionRepository.update_ledger, SQLiteUnitOfWork.__enter__

    def update(repository, **kwargs):
        result = original_update(repository, **kwargs)
        if kwargs["state"] is LedgerState.CANCELLED:
            written.set()
            assert worker_attempted.wait(5)
        return result

    def enter(uow):
        if current_thread().name.startswith("worker"):
            worker_attempted.set()
        return original_enter(uow)

    monkeypatch.setattr(ActionRepository, "update_ledger", update)
    monkeypatch.setattr(SQLiteUnitOfWork, "__enter__", enter)
    command = CancelWaterRun.create(
        run_id=started.run_id, conversation_id=started.conversation_id, expected_revision=3
    )
    with (
        ThreadPoolExecutor(1, thread_name_prefix="cancel") as cancel_pool,
        ThreadPoolExecutor(1, thread_name_prefix="worker") as worker_pool,
    ):
        cancelling = cancel_pool.submit(service.cancel, command)
        assert written.wait(5)
        running = worker_pool.submit(worker.run_once, limit=1)
        assert cancelling.result(timeout=10).accepted
        assert running.result(timeout=10) == ()
    assert service.backend.execution_count() == 0


def test_cli_rejects_other_run_unknown_format_and_missing_file(tmp_path):
    import json

    service, _, first = prepared(tmp_path)
    assert len(service.create_worker().run_once(limit=3)) == 3
    second = service.start()
    assert service.approve(_approval_command(service, second)).accepted
    assert len(service.create_worker().run_once(limit=3)) == 3
    exported = tmp_path / "first.md"
    assert (
        cli_process(
            tmp_path,
            service.state_root,
            "report",
            "--run",
            first.run_id,
            "--format",
            "md",
            "--output",
            exported,
        ).returncode
        == 0
    )
    unknown = tmp_path / "first.txt"
    unknown.write_bytes(exported.read_bytes())
    for path in (exported, unknown, tmp_path / "missing.json"):
        result = cli_process(
            tmp_path, service.state_root, "verify-report", "--run", second.run_id, "--report", path
        )
        assert result.returncode == 2 and json.loads(result.stdout)["valid"] is False


def test_invalid_expired_approval_does_not_expire_someone_elses_binding(tmp_path):
    service = P3ApplicationService(
        tmp_path / "state", clock=FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    )
    started = service.start()
    command = _approval_command(service, started)
    service.clock.advance(timedelta(hours=24))
    assert not service.approve(command.model_copy(update={"action_hash": "0" * 64})).accepted
    assert service.inspect(started.run_id).revision == 2
    assert service.inspect(started.run_id).ledger_state is LedgerState.PLANNED
    expired = service.approve(command)
    assert expired.code == "interrupt_expired" and expired.revision == 3
