"""Controlled Python processes: not real ORCA acceptance evidence."""

import hashlib
import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from orca_agent.application.p5_service import P5ApplicationService
from orca_agent.domain.hashing import sha256_hex
from orca_agent.execution.local_runner import run_supervisor
from orca_agent.execution.windows_job import process_start_marker
from tests.p5.test_p5_workflow import _source


def _controlled_run(
    tmp_path,
    monkeypatch,
    script,
    *,
    wall_time_seconds=None,
    before_launch=None,
    expected_outcome="starting",
    protocol_id="p5.sp_initial.r2scan3c.v1",
):
    from orca_agent.application import p5_service
    from orca_agent.execution import orca_config

    state_root, _, source = _source(tmp_path)
    executable = Path(sys.executable)
    if os.name != "nt":
        # uv's relocatable CPython cannot be copied away from its runtime tree.
        # This explicit fixture launcher preserves the production .exe allowlist
        # and invokes the installed interpreter in place (not real ORCA).
        executable = tmp_path / "controlled.exe"
        executable.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8"
        )
        executable.chmod(0o700)
    monkeypatch.setattr(orca_config, "probe_orca_version", lambda _path: "6.1.0")
    original_compile = p5_service.compile_orca_input

    def compile_fixture(*args, **kwargs):
        compiled = original_compile(*args, **kwargs)
        data = script.encode()
        digest = hashlib.sha256(data).hexdigest()
        manifest = {**compiled.manifest, "controlled_fixture": True, "input_sha256": digest}
        return compiled.model_copy(
            update={
                "input_bytes": data,
                "input_sha256": digest,
                "manifest": manifest,
                "manifest_hash": sha256_hex(manifest),
            }
        )

    monkeypatch.setattr(p5_service, "compile_orca_input", compile_fixture)
    service = P5ApplicationService(
        state_root, backend_kind="local_orca", orca_executable=executable, orca_version="6.1.0"
    )
    prepared = service.prepare_execution(
        source_run_id=source,
        protocol_id=protocol_id,
        wall_time_seconds=wall_time_seconds,
    )
    assert prepared.accepted, prepared
    view = service.inspect(prepared.run_id)
    approved = service.approve(
        run_id=view.run_id,
        conversation_id=view.conversation_id,
        action_id=view.action.action_id,
        action_hash=view.action.action_hash,
        binding_hash=view.binding.binding_hash,
        envelope_hash=view.action.envelope_hash,
        budget_hash=view.action.budget_hash,
        expected_revision=view.revision,
    )
    assert approved.accepted
    if before_launch is not None:
        original_launch = service.backend.start_or_reconcile

        def launch(request):
            before_launch(service, request)
            return original_launch(request)

        monkeypatch.setattr(service.backend, "start_or_reconcile", launch)
    reports = service.create_worker(allow_real_orca=True).run_once(run_id=view.run_id)
    assert reports and reports[0].outcome == expected_outcome, reports
    current = service.inspect(view.run_id)
    directory = state_root / "work" / str(current.job.execution_id)
    return service, current, directory


def _receipt(directory):
    until = time.monotonic() + 15
    while time.monotonic() < until:
        path = directory / "exit_receipt.json"
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.03)
    pytest.fail("controlled child did not produce a bounded terminal receipt")


def test_local_runner_starts_controlled_python_child_and_replays_receipt(tmp_path, monkeypatch):
    service, view, directory = _controlled_run(
        tmp_path, monkeypatch, 'from pathlib import Path\nPath("child.done").write_text("ok")\n'
    )
    receipt = _receipt(directory)
    assert receipt["status"] == "succeeded"
    before = (directory / "exit_receipt.json").read_bytes()
    (directory / "child.done").unlink()
    assert run_supervisor(service.state_root, str(view.job.execution_id)) == 0
    assert not (directory / "child.done").exists()
    assert (directory / "exit_receipt.json").read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="Windows physical-memory preflight")
def test_local_backend_replays_terminal_receipt_before_low_memory_preflight(tmp_path, monkeypatch):
    from orca_agent.execution import local_backend
    from orca_agent.planning.p5_protocols import P5_OPT_FREQ_SP_4CORE

    def seed_receipt(service, request):
        directory = service.backend._materialize(request)
        payload = {
            "status": "succeeded",
            "exit_code": 0,
            "physical_start_count": 1,
            "data_origin": "orca_local",
            "execution_id": str(request.job.execution_id),
            "job_id": str(request.job.job_id),
            "launch_token": request.job.launch_token,
            "host_identity": request.job.host_identity,
        }
        (directory / "exit_receipt.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(local_backend, "available_physical_memory_mb", lambda: 1024)

    service, view, directory = _controlled_run(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\nPath('unexpected-child').write_text('bad')\n",
        before_launch=seed_receipt,
        expected_outcome="succeeded",
        protocol_id=P5_OPT_FREQ_SP_4CORE.protocol_id,
    )
    assert view.state.phase.value == "collecting"
    assert view.job is not None and view.job.launch_consumed_at_utc is None
    assert (directory / "exit_receipt.json").exists()
    assert not (directory / "launch.json").exists()
    assert not (directory / "supervisor.json").exists()
    assert not (directory / "unexpected-child").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows physical-memory preflight")
@pytest.mark.parametrize("mutation", ["changed", "missing"])
def test_local_backend_rejects_low_memory_receipt_when_frozen_file_is_not_intact(
    tmp_path, monkeypatch, mutation
):
    from orca_agent.execution import local_backend
    from orca_agent.planning.p5_protocols import P5_OPT_FREQ_SP_4CORE

    def seed_receipt(service, request):
        directory = service.backend._materialize(request)
        payload = {
            "status": "succeeded",
            "exit_code": 0,
            "physical_start_count": 1,
            "data_origin": "orca_local",
            "execution_id": str(request.job.execution_id),
            "job_id": str(request.job.job_id),
            "launch_token": request.job.launch_token,
            "host_identity": request.job.host_identity,
        }
        (directory / "exit_receipt.json").write_text(json.dumps(payload), encoding="utf-8")
        target = directory / ("input.inp" if mutation == "changed" else "geometry.xyz")
        if mutation == "changed":
            target.write_bytes(b"changed frozen input")
        else:
            target.unlink()
        monkeypatch.setattr(local_backend, "available_physical_memory_mb", lambda: 1024)

    service, view, directory = _controlled_run(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\nPath('unexpected-child').write_text('bad')\n",
        before_launch=seed_receipt,
        expected_outcome="resource_limit_exceeded",
        protocol_id=P5_OPT_FREQ_SP_4CORE.protocol_id,
    )
    assert directory.exists()
    target = directory / ("input.inp" if mutation == "changed" else "geometry.xyz")
    if mutation == "changed":
        assert target.read_bytes() == b"changed frozen input"
    else:
        assert not target.exists()
    assert not (directory / "launch.json").exists()
    assert not (directory / "supervisor.json").exists()
    assert not (directory / "unexpected-child").exists()
    assert view.job is not None and view.job.launch_consumed_at_utc is None


def test_continuous_poll_is_non_destructive_and_pid_reuse_is_rejected(tmp_path, monkeypatch):
    service, view, directory = _controlled_run(
        tmp_path, monkeypatch, "import time\ntime.sleep(8)\n"
    )
    try:
        identity = json.loads((directory / "supervisor.json").read_text())
        for _ in range(10):
            assert service.backend.poll(str(view.job.execution_id)).status.value == "running"
            assert process_start_marker(identity["pid"]) == identity["created"]
        identity["created"] -= 1
        (directory / "supervisor.json").write_text(json.dumps(identity))
        assert (
            service.backend.poll(str(view.job.execution_id)).status.value == "needs_reconciliation"
        )
    finally:
        (directory / "cancel.requested").touch()
        assert _receipt(directory)["status"] == "cancelled"


def test_concurrent_runner_cannot_reuse_consumed_ticket(tmp_path, monkeypatch):
    service, view, directory = _controlled_run(
        tmp_path,
        monkeypatch,
        "import time\nfrom pathlib import Path\n"
        'with Path("starts").open("a") as f: f.write("start\\n")\ntime.sleep(4)\n',
    )
    try:
        until = time.monotonic() + 3
        while not (directory / "starts").exists() and time.monotonic() < until:
            time.sleep(0.02)
        assert run_supervisor(service.state_root, str(view.job.execution_id)) == 2
    finally:
        (directory / "cancel.requested").touch()
        _receipt(directory)
    assert (directory / "starts").read_text().splitlines() == ["start"]


def test_long_lived_job_outlasts_dispatch_lease_without_relaunch(tmp_path, monkeypatch):
    service, view, directory = _controlled_run(
        tmp_path, monkeypatch, "import time\ntime.sleep(50)\n"
    )
    try:
        deadline = time.monotonic() + 32
        while time.monotonic() < deadline:
            assert service.backend.poll(str(view.job.execution_id)).status.value == "running"
            time.sleep(1)
        restored = P5ApplicationService(service.state_root)
        assert restored.reconcile(view.run_id).accepted
        assert run_supervisor(service.state_root, str(view.job.execution_id)) == 2
        from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

        with SQLiteUnitOfWork(service.state_root) as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM local_jobs").fetchone()[0] == 1
            assert uow.outbox.list_for_run(view.run_id)[0].status.value == "succeeded"
    finally:
        (directory / "cancel.requested").touch()
        assert _receipt(directory)["status"] == "cancelled"


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_stop_terminates_descendant_tree_with_fresh_service(tmp_path, monkeypatch, stop):
    service, view, directory = _controlled_run(
        tmp_path,
        monkeypatch,
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "Path('descendant.pid').write_text(str(p.pid))\ntime.sleep(30)\n",
        wall_time_seconds=4 if stop == "timeout" else 15,
    )
    try:
        until = time.monotonic() + 3
        while not (directory / "descendant.pid").exists() and time.monotonic() < until:
            time.sleep(0.02)
        descendant = int((directory / "descendant.pid").read_text())
        assert process_start_marker(descendant) is not None
        restored = P5ApplicationService(service.state_root)
        if stop == "cancel":
            assert restored.cancel(
                run_id=view.run_id,
                conversation_id=view.conversation_id,
                expected_revision=view.revision,
            ).accepted
        receipt = _receipt(directory)
        assert receipt["status"] == ("cancelled" if stop == "cancel" else "timed_out")
        assert process_start_marker(descendant) is None
        assert process_start_marker(receipt["orca_pid"]) is None
        assert restored.reconcile(view.run_id).accepted
    finally:
        (directory / "cancel.requested").touch()
        _receipt(directory)


def test_deadline_stops_the_controlled_process_tree(tmp_path, monkeypatch):
    service, view, directory = _controlled_run(
        tmp_path, monkeypatch, "import time\ntime.sleep(10)\n", wall_time_seconds=2
    )
    receipt = _receipt(directory)
    assert receipt["status"] == "timed_out"
    assert process_start_marker(receipt["orca_pid"]) is None
    result = service.reconcile(view.run_id)
    assert result.accepted
    final = service.inspect(view.run_id)
    assert final.state.phase.value == "failed"
    assert final.job.status.value == "timed_out"
    assert final.results[-1].parse_status.value == "rejected"


def test_cancel_command_stops_and_archives_partial_output(tmp_path, monkeypatch):
    service, view, directory = _controlled_run(
        tmp_path, monkeypatch, 'import time\nprint("partial output", flush=True)\ntime.sleep(10)\n'
    )
    until = time.monotonic() + 3
    while (directory / "stdout.out").stat().st_size == 0 and time.monotonic() < until:
        time.sleep(0.02)
    result = service.cancel(
        run_id=view.run_id, conversation_id=view.conversation_id, expected_revision=view.revision
    )
    assert result.accepted, result
    assert _receipt(directory)["status"] == "cancelled"
    final = service.inspect(view.run_id)
    assert final.state.phase.value == "cancelled"
    assert final.results[-1].stdout_artifact_id is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_supervisor_crash_kills_descendants_without_relaunch(tmp_path, monkeypatch):
    import subprocess

    service, view, directory = _controlled_run(
        tmp_path,
        monkeypatch,
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
        'Path("descendant.pid").write_text(str(child.pid))\ntime.sleep(30)\n',
    )
    until = time.monotonic() + 5
    while not (directory / "descendant.pid").exists() and time.monotonic() < until:
        time.sleep(0.02)
    descendant = int((directory / "descendant.pid").read_text())
    supervisor = json.loads((directory / "supervisor.json").read_text())
    assert process_start_marker(descendant) is not None
    # Terminating a fixture PID is deliberate; this is not the poll probe.
    subprocess.run(
        ["taskkill", "/F", "/PID", str(supervisor["pid"])], check=True, capture_output=True
    )
    until = time.monotonic() + 5
    while process_start_marker(descendant) is not None and time.monotonic() < until:
        time.sleep(0.03)
    assert process_start_marker(descendant) is None
    assert service.reconcile(view.run_id).accepted
    final = service.inspect(view.run_id)
    assert final.state.phase.value == "needs_reconciliation"
    assert run_supervisor(service.state_root, str(view.job.execution_id)) == 2


@pytest.mark.parametrize("resource", ["output", "workdir"])
def test_runner_enforces_file_budgets(tmp_path, monkeypatch, resource):
    from orca_agent.domain.p5 import P5Budget

    original = P5Budget.defaults_for
    monkeypatch.setattr(
        P5Budget,
        "defaults_for",
        classmethod(
            lambda cls, kind: original(kind).model_copy(
                update={"stdout_stderr_limit_bytes": 4096, "workdir_limit_bytes": 128 * 1024}
            )
        ),
    )
    script = 'import time\nprint("x" * 16384, flush=True)\ntime.sleep(10)\n'
    if resource == "workdir":
        script = (
            "import time\nfrom pathlib import Path\n"
            'Path("large.tmp").write_bytes(b"x" * 262144)\ntime.sleep(10)\n'
        )
    service, view, directory = _controlled_run(tmp_path, monkeypatch, script)
    receipt = _receipt(directory)
    assert receipt["status"] == "failed"
    assert receipt["stop_reason"] == "resource_limit_exceeded"
    assert process_start_marker(receipt["orca_pid"]) is None
    assert service.reconcile(view.run_id).accepted


@pytest.mark.skipif(os.name != "nt", reason="Windows aggregate job memory limit")
def test_job_object_applies_aggregate_memory_limit(tmp_path):
    import subprocess

    from orca_agent.execution.windows_job import WindowsJobObject

    code = (
        "try:\n data = bytearray(512 * 1024 * 1024)\nexcept MemoryError:\n raise SystemExit(17)\n"
    )
    with WindowsJobObject(memory_limit_bytes=128 * 1024 * 1024) as job:
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            creationflags=0x4,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            job.assign_pid(process.pid)
            job.resume_pid(process.pid)
            assert process.wait(timeout=10) == 17
        finally:
            if process.poll() is None:
                job.terminate()
                process.wait(timeout=5)


@pytest.mark.parametrize("case", ["expired_permit", "cancel_before_consume"])
def test_invalid_launch_authority_never_starts_child(tmp_path, monkeypatch, case):
    from datetime import UTC, datetime, timedelta

    from orca_agent.infrastructure.clock import format_utc
    from orca_agent.infrastructure.p5_records import LocalJobRepository
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

    def invalidate(service, request):
        with SQLiteUnitOfWork(service.state_root) as uow:
            uow.begin()
            if case == "expired_permit":
                uow.connection.execute(
                    "UPDATE outbox SET lease_expires_at_utc = ? WHERE effect_id = ?",
                    (
                        format_utc(datetime.now(UTC) + timedelta(seconds=0.2)),
                        str(request.permit.effect.effect_id),
                    ),
                )
            else:
                LocalJobRepository(uow.connection).request_cancel(request.job.execution_id)
            uow.commit()
        if case == "expired_permit":
            time.sleep(0.25)

    service, view, directory = _controlled_run(
        tmp_path,
        monkeypatch,
        'from pathlib import Path\nPath("child.started").write_text("unexpected")\n',
        before_launch=invalidate,
        expected_outcome="launch_state_unknown",
    )
    assert not (directory / "child.started").exists()
    assert view.job.launch_consumed_at_utc is None
    assert run_supervisor(service.state_root, str(view.job.execution_id)) == 2
