"""Minimal C1–C3 regressions from the c7cd1ea completion review."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.application.p5_errors import GeometryBindingMismatch, P5Error
from orca_agent.application.p5_service import P5ApplicationService
from orca_agent.execution.control_evidence import control_gate_status
from orca_agent.execution.local_backend import FakeExecutionBackend
from orca_agent.execution.local_runner import CONTROL_EXIT_CODE, _controlled_stop
from orca_agent.execution.orca_parser import parse_orca_output
from orca_agent.identity.optimized_compatibility import validate_optimized_identity
from tests.p5.test_p5_output_contract import _stdout
from tests.p5.test_p5_workflow import _approve_and_run, _prepare, _source


@pytest.mark.parametrize(
    "tail",
    [
        b"FINAL SINGLE POINT ENERGY -999.123\n",
        b"SCF CONVERGED\n",
        b"ORCA finished by error termination\n",
        b"Program Version 6.1.1\n",
    ],
)
def test_c1_real_opt_rejects_calculation_after_termination(tmp_path, tail):
    _, _, view = _prepare(tmp_path, "p5.opt_only.r2scan3c.v1")
    fixture = Path(__file__).parent / "fixtures/orca_6_1_1_water_opt"
    with pytest.raises(P5Error):
        parse_orca_output(
            (fixture / "stdout.out").read_bytes() + tail,
            primitive="opt",
            geometry=view.geometry[0],
            input_manifest_hash="0" * 64,
            exit_code=0,
            optimized_xyz_bytes=(fixture / "input.xyz").read_bytes(),
        )


def test_c1_scf_heading_and_previous_cycle_do_not_prove_success(tmp_path):
    _, _, view = _prepare(tmp_path, "p5.sp_initial.r2scan3c.v1")
    for output in (
        _stdout("sp").replace(b"SCF CONVERGED", b"SCF CONVERGENCE"),
        _stdout("sp").replace(b"****ORCA", b"FINAL SINGLE POINT ENERGY -74.0\n****ORCA"),
    ):
        with pytest.raises(P5Error):
            parse_orca_output(
                output,
                primitive="opt",
                geometry=view.geometry[0],
                input_manifest_hash="0" * 64,
                exit_code=0,
            )


@pytest.mark.parametrize("smiles", ["O", "CCO", "c1ccccc1", "C[C@H](O)F", "F/C=C/F"])
def test_c2_supported_geometry_identity_passes_and_broken_connections_fail(tmp_path, smiles):
    root, clock, source = _source(tmp_path, smiles)
    service = P5ApplicationService(root, clock=clock)
    prepared = service.prepare_execution(
        source_run_id=source, protocol_id="p5.opt_only.r2scan3c.v1"
    )
    geometry = service.inspect(prepared.run_id).geometry[0]
    validate_optimized_identity(geometry.xyz_bytes(), geometry)
    lines = geometry.xyz_bytes().decode().splitlines()
    lines[-1] = lines[-1].split()[0] + " 100 100 100"
    with pytest.raises(GeometryBindingMismatch):
        validate_optimized_identity(("\n".join(lines) + "\n").encode(), geometry)


def test_c2_mirrored_explicit_stereochemistry_rejected(tmp_path):
    root, clock, source = _source(tmp_path, "C[C@H](O)F")
    service = P5ApplicationService(root, clock=clock)
    prepared = service.prepare_execution(
        source_run_id=source, protocol_id="p5.opt_only.r2scan3c.v1"
    )
    geometry = service.inspect(prepared.run_id).geometry[0]
    reflected = geometry.model_copy(
        update={"coordinates": tuple((-x, y, z) for x, y, z in geometry.coordinates)}
    )
    with pytest.raises(GeometryBindingMismatch):
        validate_optimized_identity(reflected.xyz_bytes(), geometry)


def test_c2_changed_ez_geometry_rejected(tmp_path):
    from rdkit import Chem
    from rdkit.Chem import AllChem

    root, clock, source = _source(tmp_path, "F/C=C/F")
    service = P5ApplicationService(root, clock=clock)
    prepared = service.prepare_execution(
        source_run_id=source, protocol_id="p5.opt_only.r2scan3c.v1"
    )
    geometry = service.inspect(prepared.run_id).geometry[0]
    changed = Chem.AddHs(Chem.MolFromSmiles("F/C=C\\F"))
    assert AllChem.EmbedMolecule(changed, randomSeed=6022) == 0
    with pytest.raises(GeometryBindingMismatch):
        validate_optimized_identity(Chem.MolToXYZBlock(changed).encode(), geometry)


def test_c3_real_exited_child_is_not_a_cancelled_process(tmp_path):
    import subprocess
    import sys

    from orca_agent.execution.windows_job import process_start_marker

    with subprocess.Popen([sys.executable, "-c", "pass"], cwd=tmp_path) as child:
        marker = process_start_marker(child.pid)
        assert child.wait(timeout=10) == 0
        facts = _controlled_stop(child, None, marker, "cancel_requested")
    assert facts["exit_code"] == 0
    assert not facts["request_sent"] and not facts["stop_confirmed"]


def test_c3_identity_unavailable_never_sends_stop(monkeypatch):
    from orca_agent.execution import local_runner

    monkeypatch.setattr(local_runner, "process_start_marker", lambda pid: None)
    process = SimpleNamespace(pid=123, poll=lambda: None)
    job = SimpleNamespace(terminate=lambda code: pytest.fail("unverified process was stopped"))
    facts = _controlled_stop(process, job, 10.0, "wall_time_deadline")
    assert not facts["identity_matched"] and not facts["request_sent"]


def test_c2_incompatible_opt_archived_without_downstream_action(tmp_path):
    class BrokenOpt(FakeExecutionBackend):
        def start_or_reconcile(self, request):
            result = super().start_or_reconcile(request)
            directory = self._workdir(str(request.job.execution_id))
            xyz = b"3\nbroken identity fixture\nO 0 0 0\nH 100 0 0\nH 0 100 0\n"
            (directory / "input.xyz").write_bytes(xyz)
            (directory / "stdout.out").write_bytes(
                _stdout("opt", coords="O 0 0 0\nH 100 0 0\nH 0 100 0")
            )
            return result

    service, _, view = _prepare(tmp_path, "p5.opt_freq_sp.r2scan3c.v1")
    service.backend = BrokenOpt(service.state_root)
    final = _approve_and_run(service, view)
    assert final.state.phase.value == "failed"
    assert len(final.results) == 1 and final.results[0].parse_status.value == "rejected"
    assert final.results[0].stdout_artifact_id
    assert "identity compatibility" in final.state.last_error_message
    assert final.state.current_action_id is None


@pytest.mark.parametrize("phase", ["already_exited", "before_request", "during_request"])
def test_c3_natural_exit_never_claims_control(monkeypatch, phase):
    from orca_agent.execution import local_runner

    polls = iter(
        [0]
        if phase == "already_exited"
        else [None, 0]
        if phase == "before_request"
        else [None, None]
    )
    requests = []
    process = SimpleNamespace(pid=123, poll=lambda: next(polls), wait=lambda timeout: 0)
    job = SimpleNamespace(
        terminate=lambda code: requests.append(code), active_process_count=lambda: 0
    )
    monkeypatch.setattr(local_runner, "process_start_marker", lambda pid: 10.0)
    facts = _controlled_stop(process, job, 10.0, "cancel_requested")
    assert facts["stop_confirmed"] is False and facts["exit_code"] == 0
    assert bool(requests) == (phase == "during_request")


@pytest.mark.parametrize("status", ["cancelled", "timed_out"])
def test_c3_gate_requires_control_facts_not_exit_code(status):
    reason = "cancel_requested" if status == "cancelled" else "wall_time_deadline"
    receipt = dict(status=status, exit_code=0, physical_start_count=1)
    assert control_gate_status(receipt, status) == "NOT_EXERCISED"
    receipt.update(
        exit_code=CONTROL_EXIT_CODE, orca_pid=123, orca_created_at=10.0, stop_reason=reason
    )
    facts = dict(
        schema="p5-stop/v1",
        reason=reason,
        pid=123,
        expected_created=10.0,
        observed_created=10.0,
        identity_matched=True,
        alive_before_request=True,
        request_sent=True,
        stop_confirmed=True,
        tree_stopped=True,
        exit_code=CONTROL_EXIT_CODE,
        requested_at_utc="2026-09-07T00:00:00+00:00",
        confirmed_at_utc="2026-09-07T00:00:01+00:00",
    )
    receipt["stop_facts"] = facts
    assert control_gate_status(receipt, status) == "PASS"
    for field in ("identity_matched", "alive_before_request", "stop_confirmed", "tree_stopped"):
        receipt["stop_facts"] = {**facts, field: False}
        assert control_gate_status(receipt, status) == "FAIL"
