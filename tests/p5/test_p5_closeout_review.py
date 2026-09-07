"""Frozen Freq replay and end-to-end unconfirmed Windows control regressions."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.application.p5_errors import GeometryBindingMismatch
from orca_agent.domain.p5 import GeometryRecord
from orca_agent.execution import local_runner
from orca_agent.execution.orca_parser import parse_orca_output
from tests.p5.test_p5_local_runner import _controlled_run
from tests.p5.test_p5_output_contract import _hessian, _parse
from tests.p5.test_p5_workflow import _prepare


def test_frozen_real_freq_replays_without_changing_original_bytes():
    packet = Path(__file__).resolve().parents[2] / "docs/evidence/p5-freq-blocker"
    evidence = json.loads((packet / "evidence.json").read_text())
    geometry = GeometryRecord.model_validate_json(
        json.dumps(
            [r["record"] for r in evidence["records"] if r["record_type"] == "p5.geometry"][-1]
        )
    )
    binding = [
        r["record"] for r in evidence["records"] if r["record_type"] == "p5.execution_binding"
    ][-1]
    raw = {
        name: (packet / name).read_bytes() for name in ("stdout.out", "hessian.txt", "geometry.xyz")
    }
    before = {name: hashlib.sha256(data).hexdigest() for name, data in raw.items()}
    assert hashlib.sha256(raw["geometry.xyz"]).hexdigest() == binding["xyz_bytes_sha256"]
    parsed = parse_orca_output(
        raw["stdout.out"],
        primitive="freq",
        geometry=geometry,
        input_manifest_hash=binding["input_manifest_hash"],
        exit_code=0,
        hessian_bytes=raw["hessian.txt"],
    )
    assert parsed.hessian_dimension == 9 and len(parsed.frequencies) == 9
    assert parsed.energy == pytest.approx(-76.418938721035, abs=1e-12)
    assert before == {
        name: hashlib.sha256((packet / name).read_bytes()).hexdigest() for name in raw
    }


@pytest.mark.parametrize(
    "case", ["translation", "single_atom", "units", "correspondence", "rotation"]
)
def test_hessian_binding_allows_only_translation(tmp_path, case):
    _, _, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    geometry = view.geometry[0]
    coordinates = [
        tuple(v + shift for v, shift in zip(xyz, (1.3, -2.7, 0.5), strict=True))
        for xyz in geometry.coordinates
    ]
    if case == "single_atom":
        coordinates[-1] = tuple(v + 1e-3 for v in coordinates[-1])
    elif case == "units":
        coordinates = [tuple(v * 0.529177210903 for v in xyz) for xyz in coordinates]
    elif case == "correspondence":
        coordinates[1], coordinates[2] = coordinates[2], coordinates[1]
    elif case == "rotation":
        coordinates = [(-y, x, z) for x, y, z in coordinates]
    hessian = _hessian(geometry.model_copy(update={"coordinates": tuple(coordinates)}))
    if case == "translation":
        assert _parse(geometry, hessian_bytes=hessian).hessian_dimension == 9
    else:
        with pytest.raises(GeometryBindingMismatch, match="center-of-mass translation"):
            _parse(geometry, hessian_bytes=hessian)


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
@pytest.mark.parametrize("tree", ["nonempty", "query_error"])
def test_unconfirmed_tree_remains_pending_through_runner_backend_service(
    tmp_path, monkeypatch, stop, tree
):
    """Inject OS observations, execute real supervisor/receipt/backend/service code.

    No actual process is spawned; existing Windows tests exercise real OS control.
    """
    child = SimpleNamespace(pid=12345, code=None)
    child.poll = lambda: child.code

    def wait(timeout):
        child.code = local_runner.CONTROL_EXIT_CODE
        return child.code

    child.wait = wait
    requests = []
    starts = []

    def count():
        if tree == "query_error":
            raise OSError("injected Job query failure")
        return 1

    job = SimpleNamespace(
        assign_pid=lambda pid: None,
        resume_pid=lambda pid: None,
        terminate=lambda code: requests.append(code),
        active_process_count=count,
        close=lambda: None,
    )
    real_os = local_runner.os

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(real_os, name)

    def popen(args, **kwargs):
        if "orca_agent.execution.local_runner" not in args:
            starts.append(args)
            return child
        root = args[args.index("--state-root") + 1]
        execution = args[args.index("--execution-id") + 1]
        ticks = iter(range(0, 10000, 100))
        checks = iter([False, stop == "cancel"])
        with monkeypatch.context() as patch:
            patch.setattr(local_runner, "os", WindowsOS())
            patch.setattr(local_runner, "WindowsJobObject", lambda **kw: job)
            patch.setattr(local_runner, "process_start_marker", lambda pid: 10.0)
            patch.setattr(local_runner, "_cancel_pending", lambda *a: next(checks))
            patch.setattr(local_runner.time, "monotonic", lambda: next(ticks))
            patch.setattr(local_runner.time, "sleep", lambda seconds: None)
            assert local_runner.run_supervisor(root, execution) == 1
        return SimpleNamespace(pid=os.getpid(), poll=lambda: 1)

    monkeypatch.setattr(local_runner.subprocess, "Popen", popen)
    service, view, directory = _controlled_run(tmp_path, monkeypatch, "pass", wall_time_seconds=30)
    receipt = json.loads((directory / "exit_receipt.json").read_text())
    assert requests == [local_runner.CONTROL_EXIT_CODE]
    assert receipt["stop_facts"]["stop_confirmed"] is True
    assert receipt["stop_facts"]["tree_stopped"] is False
    assert receipt["status"] == "needs_reconciliation"
    execution = str(view.job.execution_id)
    # Even a contradictory status label in a trusted, correctly bound receipt
    # cannot override missing tree-stop evidence at the backend boundary.
    for status in ("cancelled", "timed_out", "failed", "succeeded", "needs_reconciliation"):
        (directory / "exit_receipt.json").write_text(json.dumps({**receipt, "status": status}))
        assert service.backend.poll(execution).status.value == "needs_reconciliation"
        with pytest.raises(RuntimeError, match="not terminal"):
            service.backend.collect(execution)
        cancelled = service.backend.cancel(execution, "injected-cancel")
        assert cancelled.status.value == "needs_reconciliation" and not cancelled.stopped
    assert service.reconcile(view.run_id).accepted
    final = service.inspect(view.run_id)
    assert final.state.phase.value == final.job.status.value == "needs_reconciliation"
    assert final.state.current_execution_id == view.job.execution_id
    assert not final.results
    reports = service.create_worker(allow_real_orca=True).run_once(run_id=view.run_id)
    assert all(report.status == "needs_reconciliation" for report in reports)
    assert local_runner.run_supervisor(service.state_root, execution) == 1
    assert requests == [local_runner.CONTROL_EXIT_CODE]
    assert len(starts) == 1
