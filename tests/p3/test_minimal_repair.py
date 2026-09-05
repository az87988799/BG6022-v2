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
