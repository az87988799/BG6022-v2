from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orca_agent.application.p5_service import P5ApplicationService
from orca_agent.domain.p5 import (
    P5_DEFAULT_MAXCORE_MB,
    P5_DEFAULT_NPROCS,
    P5_DEFAULT_TOTAL_MEMORY_MB,
    P5Budget,
    P5NodeKind,
)
from orca_agent.execution.local_backend import FakeExecutionBackend
from orca_agent.execution.orca_compiler import compile_orca_input
from orca_agent.execution.orca_config import execution_environment, runtime_config
from orca_agent.planning.p5_protocols import (
    P5_DEFAULT_PROTOCOL,
    P5_OPT_FREQ_SP,
    P5_OPT_FREQ_SP_4CORE,
    P5_OPT_FREQ_SP_4CORE_2048,
    P5_PROTOCOLS_BY_ID,
)
from orca_agent.planning.registry import METHOD_R2SCAN3C
from tests.p5.test_p5_workflow import _approve_and_run, _prepare, _run_all_actions, _source

OLD_PROTOCOL_HASHES = {
    "p5.sp_initial.r2scan3c.v1": "0a2c90f01e639b4bc1368c24fbd6a277b0dcfefab792afae6266b7aa65f1e2b1",
    "p5.opt_only.r2scan3c.v1": "005388ea1450d4646394f28ef5f824083fb8e84f9689f8828a1612c5ce996e5e",
    "p5.freq_from_opt.r2scan3c.v1": (
        "5ae819de59db49a9078e2e97de5f7f8739e55ec4d3fe9b39fc984ad620c5e7e3"
    ),
    "p5.opt_freq.r2scan3c.v1": "a4c6a33e86e7d3b3fb38a84ad11c8d4d8806422c27fd6912cb8d2e6bae065525",
    "p5.opt_freq_sp.r2scan3c.v1": (
        "785b6ff424c6c19af23646a9bf4ea8e83e4301ee1dce3c57efef1173c6c300e8"
    ),
}


def test_default_budget_is_four_core_2048mb_and_registered_protocol_is_v3():
    assert (P5_DEFAULT_NPROCS, P5_DEFAULT_TOTAL_MEMORY_MB, P5_DEFAULT_MAXCORE_MB) == (
        4,
        2048,
        384,
    )
    assert P5Budget.defaults_for(P5NodeKind.OPT).model_dump(mode="json") == {
        "nprocs": 4,
        "total_memory_mb": 2048,
        "maxcore_mb": 384,
        "wall_time_seconds": 900,
        "run_wall_time_seconds": 3600,
        "stdout_stderr_limit_bytes": 64 * 1024 * 1024,
        "workdir_limit_bytes": 512 * 1024 * 1024,
    }
    assert P5_DEFAULT_PROTOCOL is P5_OPT_FREQ_SP_4CORE_2048
    assert P5_DEFAULT_PROTOCOL.protocol_id == "p5.opt_freq_sp.r2scan3c.4core.v3"
    assert P5_DEFAULT_PROTOCOL.version == "3"
    assert P5_DEFAULT_PROTOCOL.protocol_hash == (
        "734393597e09398b0436dcde78152f1a102d209144bdb53764c831c8b7c5dc37"
    )
    assert P5_DEFAULT_PROTOCOL.content["resource_profile"] == {
        "nprocs": 4,
        "total_memory_mb": 2048,
        "maxcore_mb": 384,
        "parallel": True,
        "implicit_threads": 1,
        "run_wall_time_seconds": 3600,
        "wall_time_seconds": {"opt": 900, "freq": 1800, "sp": 300},
    }


class _CapturingFakeBackend(FakeExecutionBackend):
    def __init__(self, state_root: str | Path) -> None:
        super().__init__(state_root)
        self.requests = []

    def start_or_reconcile(self, launch_request):
        self.requests.append(launch_request)
        return super().start_or_reconcile(launch_request)


def test_four_core_protocol_preserves_old_hashes_and_registers_fixed_resources():
    assert {
        protocol_id: P5_PROTOCOLS_BY_ID[protocol_id].protocol_hash
        for protocol_id in OLD_PROTOCOL_HASHES
    } == OLD_PROTOCOL_HASHES
    assert all(
        "resource_profile" not in P5_PROTOCOLS_BY_ID[item].content for item in OLD_PROTOCOL_HASHES
    )
    assert "p5.opt_freq_sp.r2scan3c.4core.v1" not in P5_PROTOCOLS_BY_ID
    assert P5_OPT_FREQ_SP_4CORE.protocol_id == "p5.opt_freq_sp.r2scan3c.4core.v2"
    assert P5_OPT_FREQ_SP_4CORE.version == "2"
    assert P5_OPT_FREQ_SP_4CORE.protocol_hash == (
        "090eccb882b3c7ba96f1068bce9db622405a0c04b5b760471a1bcef147391245"
    )
    assert P5_OPT_FREQ_SP_4CORE.content["resource_profile"] == {
        "nprocs": 4,
        "total_memory_mb": 4096,
        "maxcore_mb": 768,
        "parallel": True,
        "implicit_threads": 1,
        "run_wall_time_seconds": 3600,
        "wall_time_seconds": {"opt": 900, "freq": 1800, "sp": 300},
    }


def test_four_core_plan_compiler_and_runtime_binding_are_consistent(tmp_path):
    service, _clock, view = _prepare(tmp_path, P5_OPT_FREQ_SP_4CORE.protocol_id)
    assert view.plan.protocol_id == P5_OPT_FREQ_SP_4CORE.protocol_id
    assert [
        (
            node.kind,
            node.budget.nprocs,
            node.budget.total_memory_mb,
            node.budget.maxcore_mb,
            node.budget.wall_time_seconds,
            node.budget.run_wall_time_seconds,
        )
        for node in view.plan.nodes
    ] == [
        (P5NodeKind.OPT, 4, 4096, 768, 900, 3600),
        (P5NodeKind.FREQ, 4, 4096, 768, 1800, 3600),
        (P5NodeKind.SP, 4, 4096, 768, 300, 3600),
    ]
    node = view.plan.nodes[0]
    compiled = compile_orca_input(
        node,
        METHOD_R2SCAN3C,
        view.geometry[0],
        node.budget,
        P5_OPT_FREQ_SP_4CORE.feature_profile(),
    )
    text = compiled.input_bytes.decode("utf-8")
    assert text.count("%pal nprocs 4 end") == 1
    assert "mpirun" not in text.lower()
    assert compiled.feature_profile == {
        "parallel": True,
        "nprocs": 4,
        "implicit_threads": 1,
    }
    assert view.binding is not None
    expected_runtime = runtime_config(
        state_root=service.state_root,
        executable=None,
        orca_version=None,
        profile_hash=compiled.feature_profile_hash,
        nprocs=4,
        implicit_threads=1,
        parallel=True,
    )
    assert view.binding.feature_profile_hash == compiled.feature_profile_hash
    assert view.binding.runtime_config_hash == expected_runtime["runtime_config_hash"]
    assert expected_runtime["thread_environment"] == {
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }


def test_compiler_uses_four_core_default_when_budget_and_profile_are_omitted(tmp_path):
    _service, _clock, view = _prepare(tmp_path, P5_OPT_FREQ_SP.protocol_id)
    node = view.plan.nodes[0]
    compiled = compile_orca_input(
        node,
        METHOD_R2SCAN3C,
        view.geometry[0],
        None,
        None,
    )
    assert b"%maxcore 384" in compiled.input_bytes
    assert b"%pal nprocs 4 end" in compiled.input_bytes
    assert compiled.feature_profile == {
        "parallel": True,
        "nprocs": 4,
        "implicit_threads": 1,
    }


def test_four_core_fake_chain_uses_three_sequential_actions(tmp_path):
    service, _clock, view = _prepare(tmp_path, P5_OPT_FREQ_SP_4CORE.protocol_id)
    final = _run_all_actions(service, view)
    assert final.state.phase.value == "completed"
    assert [result.primitive for result in final.results] == [
        P5NodeKind.OPT,
        P5NodeKind.FREQ,
        P5NodeKind.SP,
    ]
    assert all(result.data_origin.value == "fake_fixture" for result in final.results)


def test_four_core_fixed_node_budget_rejects_wall_time_override(tmp_path):
    state_root, clock, source_run_id = _source(tmp_path)
    service = P5ApplicationService(state_root, clock=clock)
    result = service.prepare_execution(
        source_run_id=source_run_id,
        protocol_id=P5_OPT_FREQ_SP_4CORE.protocol_id,
        wall_time_seconds=1,
    )
    assert not result.accepted


def test_worker_passes_four_core_runtime_to_backend(tmp_path):
    service, _clock, view = _prepare(tmp_path, P5_OPT_FREQ_SP_4CORE.protocol_id)
    backend = _CapturingFakeBackend(service.state_root)
    service.backend = backend
    final = _approve_and_run(service, view)
    assert final.state.phase.value == "awaiting_execution_approval"
    assert len(backend.requests) == 1
    request = backend.requests[0]
    assert request.node.budget.nprocs == 4
    assert request.node.budget.total_memory_mb == 4096
    assert request.runtime_config is not None
    assert request.runtime_config["nprocs"] == 4
    assert request.runtime_config["parallel"] is True
    assert request.runtime_config["thread_environment"]["OMP_NUM_THREADS"] == "1"


def test_single_core_runtime_shape_stays_legacy_and_does_not_mutate_process_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OMP_NUM_THREADS", "17")
    before = os.environ["OMP_NUM_THREADS"]
    legacy = runtime_config(
        state_root=tmp_path,
        executable=None,
        orca_version=None,
        profile_hash="a" * 64,
    )
    assert "parallel" not in legacy
    assert "thread_environment" not in legacy
    assert execution_environment(legacy)["OMP_NUM_THREADS"] == before
    assert os.environ["OMP_NUM_THREADS"] == before


def test_four_core_runtime_environment_rejects_tampering(tmp_path):
    configured = runtime_config(
        state_root=tmp_path,
        executable=None,
        orca_version=None,
        profile_hash="b" * 64,
        nprocs=4,
        parallel=True,
    )
    tampered = {
        **configured,
        "thread_environment": {**configured["thread_environment"], "MKL_NUM_THREADS": "2"},
    }
    with pytest.raises(ValueError):
        execution_environment(tampered)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_four_core_controlled_child_gets_fixed_threads_and_memory_limit(tmp_path, monkeypatch):
    from orca_agent.execution import local_backend
    from tests.p5.test_p5_local_runner import _controlled_run, _receipt

    monkeypatch.setattr(local_backend, "available_physical_memory_mb", lambda: 16 * 1024)
    script = (
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "Path('thread-env.json').write_text(json.dumps({k: os.environ.get(k) for k in "
        "['OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS']}))\n"
        "children = [subprocess.Popen([sys.executable, '-c', 'pass']) for _ in range(4)]\n"
        "[child.wait() for child in children]\n"
    )
    _service, _view, directory = _controlled_run(
        tmp_path,
        monkeypatch,
        script,
        protocol_id=P5_OPT_FREQ_SP_4CORE.protocol_id,
    )
    receipt = _receipt(directory)
    assert receipt["status"] == "succeeded"
    assert receipt["resource_enforcement"]["job_memory_limit_bytes"] == 4096 * 1024 * 1024
    assert json.loads((directory / "thread-env.json").read_text()) == {
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows physical-memory preflight")
def test_four_core_memory_preflight_blocks_launch_without_child_start(tmp_path, monkeypatch):
    from orca_agent.execution import local_backend
    from tests.p5.test_p5_local_runner import _controlled_run

    monkeypatch.setattr(local_backend, "available_physical_memory_mb", lambda: 1024)
    _service, _view, directory = _controlled_run(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\nPath('unexpected-child').write_text('bad')\n",
        expected_outcome="resource_limit_exceeded",
        protocol_id=P5_OPT_FREQ_SP_4CORE.protocol_id,
    )
    assert not directory.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows physical-memory preflight")
@pytest.mark.parametrize(
    ("available_mb", "expected_outcome"), [(4095, "resource_limit_exceeded"), (4096, "starting")]
)
def test_four_core_memory_preflight_uses_inclusive_budget_boundary(
    tmp_path, monkeypatch, available_mb, expected_outcome
):
    from orca_agent.execution import local_backend
    from tests.p5.test_p5_local_runner import _controlled_run

    monkeypatch.setattr(local_backend, "available_physical_memory_mb", lambda: available_mb)
    _service, _view, directory = _controlled_run(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\nPath('boundary-child').write_text('ok')\n",
        expected_outcome=expected_outcome,
        protocol_id=P5_OPT_FREQ_SP_4CORE.protocol_id,
    )
    assert directory.exists() is (available_mb == 4096)
