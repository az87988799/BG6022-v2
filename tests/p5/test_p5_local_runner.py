from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.p5.test_p5_workflow import _source

from orca_agent.application.p5_service import P5ApplicationService
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import ExecutionId, JobId, WorkflowRecordId, new_id
from orca_agent.domain.p5 import P5ExecutionBinding, P5JobRecord, P5JobStatus
from orca_agent.execution.local_runner import run_supervisor
from orca_agent.execution.windows_job import host_identity
from orca_agent.infrastructure.clock import format_utc
from orca_agent.infrastructure.p5_records import LocalJobRepository, P5RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork


def test_local_runner_starts_controlled_python_child_and_writes_receipt(tmp_path):
    state_root, clock, source_run_id = _source(tmp_path)
    service = P5ApplicationService(
        state_root,
        clock=clock,
        backend_kind="local_orca",
        orca_executable=sys.executable,
        orca_version="6.1.0",
    )
    prepared = service.prepare_execution(
        source_run_id=source_run_id,
        protocol_id="p5.sp_initial.r2scan3c.v1",
    )
    assert prepared.accepted
    view = service.inspect(prepared.run_id)
    assert view.action is not None and view.binding is not None and view.geometry

    child_script = (
        b'from pathlib import Path\n'
        b'Path("child.done").write_text("ok", encoding="utf-8")\n'
    )
    child_input_hash = hashlib.sha256(child_script).hexdigest()
    binding_payload = view.binding.model_dump(mode="json")
    binding_payload["record_id"] = str(new_id(WorkflowRecordId))
    binding_payload["input_sha256"] = child_input_hash
    binding_payload["input_manifest_hash"] = sha256_hex(
        {"input_sha256": child_input_hash, "fixture": "runner-smoke"}
    )
    binding_payload.pop("binding_hash", None)
    binding = P5ExecutionBinding.model_validate(
        {**binding_payload, "binding_hash": sha256_hex(binding_payload)}, strict=True
    )
    execution_id = new_id(ExecutionId)
    job = P5JobRecord(
        job_id=new_id(JobId),
        run_id=view.run_id,
        action_id=view.action.action_id,
        execution_id=execution_id,
        idempotency_key=f"runner-smoke:{execution_id}",
        binding_id=binding.record_id,
        binding_hash=binding.binding_hash,
        input_manifest_hash=binding.input_manifest_hash,
        geometry_hash=binding.geometry_hash,
        backend_kind="local_orca",
        launch_token="runner-smoke-token",
        launch_generation=1,
        launch_reserved_at_utc=clock.now_utc(),
        launch_consumed_at_utc=clock.now_utc(),
        status=P5JobStatus.STARTING,
        host_identity=host_identity(),
        executable_sha256=binding.executable_sha256,
        job_directory_id=str(execution_id),
        deadline_utc=datetime.now(UTC) + timedelta(minutes=5),
    )
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        uow.begin()
        event = uow.events.list_for_run(view.run_id)[-1].event
        P5RecordRepository(uow.connection).append_p5(
            run_id=view.run_id,
            record_type="p5.execution_binding",
            record=binding,
            created_at_utc=clock.now_utc(),
            source_event_id=event.event_id,
            record_id=binding.record_id,
        )
        LocalJobRepository(uow.connection).insert(job)
        uow.commit()

    directory = state_root / "work" / str(execution_id)
    directory.mkdir(parents=True, exist_ok=True)
    geometry_bytes = view.geometry[0].xyz_bytes()
    (directory / "input.inp").write_bytes(child_script)
    (directory / "geometry.xyz").write_bytes(geometry_bytes)
    launch = {
        "executable": str(Path(sys.executable).resolve()),
        "input": "input.inp",
        "deadline_utc": format_utc(job.deadline_utc),
        "execution_id": str(execution_id),
        "job_id": str(job.job_id),
        "launch_token": job.launch_token,
        "launch_generation": job.launch_generation,
        "binding_hash": binding.binding_hash,
        "input_sha256": child_input_hash,
        "geometry_hash": binding.geometry_hash,
        "xyz_bytes_sha256": binding.xyz_bytes_sha256,
        "host_identity": host_identity(),
    }
    (directory / "launch.json").write_text(
        json.dumps(launch, sort_keys=True), encoding="utf-8", newline="\n"
    )

    assert run_supervisor(state_root, str(execution_id)) == 0
    assert (directory / "child.done").read_text(encoding="utf-8") == "ok"
    receipt = json.loads((directory / "exit_receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "succeeded"
    assert receipt["execution_id"] == str(execution_id)
