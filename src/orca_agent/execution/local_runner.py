"""Per-job supervisor used by the local ORCA backend.

The supervisor owns only one physical process group. It does not plan, approve,
write business state, or consume the application outbox.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from orca_agent.domain.ids import ExecutionId
from orca_agent.domain.p5 import P5ExecutionBinding
from orca_agent.infrastructure.p5_records import LocalJobRepository, P5RecordRepository
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

from .launch_ticket import consume_ticket
from .output_contract import INPUT_NAME
from .windows_job import WindowsJobObject, host_identity, process_start_marker
from .work_paths import execution_directory


def run_supervisor(state_root: str | Path, execution_id: str) -> int:
    root = Path(state_root).resolve()
    try:
        directory = execution_directory(root, execution_id)
    except (OSError, ValueError):
        return 2
    try:
        spec = json.loads((directory / "launch.json").read_text(encoding="utf-8"))
        trusted_job = _validate_launch_ticket(root, spec, execution_id)
        receipt_path = directory / "exit_receipt.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if all(
                receipt.get(key) == spec.get(key)
                for key in ("execution_id", "job_id", "launch_token", "host_identity")
            ):
                return 0 if receipt.get("status") == "succeeded" else 1
            return 2
        if trusted_job.launch_consumed_at_utc is not None:
            return 2
        executable = Path(str(spec["executable"])).resolve()
        if executable.suffix.casefold() != ".exe" or not executable.is_file():
            return _write_receipt(
                directory,
                "failed",
                126,
                "executable_version_mismatch",
                execution_id=execution_id,
                job_id=str(spec.get("job_id", "")),
                launch_token=str(spec.get("launch_token", "")),
            )
        with SQLiteUnitOfWork(root) as uow:
            uow.begin()
            job = LocalJobRepository(uow.connection).get_by_execution(ExecutionId(execution_id))
            binding = (
                None
                if job is None
                else P5RecordRepository(uow.connection).get_exact_p5(
                    run_id=job.run_id,
                    record_id=job.binding_id,
                    record_type="p5.execution_binding",
                    model_type=P5ExecutionBinding,
                )
            )
            if (
                binding is None
                or binding.executable_sha256 != hashlib.sha256(executable.read_bytes()).hexdigest()
            ):
                raise ValueError("launch executable hash does not match the trusted binding")
            uow.commit()
        input_name = str(spec["input"])
        if input_name != INPUT_NAME:
            return _write_receipt(
                directory,
                "failed",
                126,
                "input_name_invalid",
                execution_id=execution_id,
                job_id=str(spec.get("job_id", "")),
                launch_token=str(spec.get("launch_token", "")),
            )
        deadline = job.deadline_utc
        input_path = directory / input_name
        if not input_path.is_file() or input_path.is_symlink():
            return _write_receipt(
                directory,
                "failed",
                126,
                "input_missing",
                execution_id=execution_id,
                job_id=str(spec.get("job_id", "")),
                launch_token=str(spec.get("launch_token", "")),
            )
        if hashlib.sha256(input_path.read_bytes()).hexdigest() != spec.get("input_sha256"):
            return _write_receipt(
                directory,
                "failed",
                126,
                "input_hash_mismatch",
                execution_id=execution_id,
                job_id=str(spec.get("job_id", "")),
                launch_token=str(spec.get("launch_token", "")),
            )
        geometry_path = directory / "geometry.xyz"
        if (
            not geometry_path.is_file()
            or geometry_path.is_symlink()
            or hashlib.sha256(geometry_path.read_bytes()).hexdigest()
            != spec.get("xyz_bytes_sha256")
        ):
            return _write_receipt(
                directory,
                "failed",
                126,
                "geometry_hash_mismatch",
                execution_id=execution_id,
                job_id=str(spec.get("job_id", "")),
                launch_token=str(spec.get("launch_token", "")),
            )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 2

    if deadline <= datetime.now(UTC):
        return _write_receipt(
            directory,
            "timed_out",
            None,
            "wall_time_deadline_before_spawn",
            execution_id=execution_id,
            job_id=str(spec.get("job_id", "")),
            launch_token=str(spec.get("launch_token", "")),
        )

    try:
        consume_ticket(root, spec)
    except Exception:
        return 2

    if _cancel_pending(root, directory, execution_id):
        return _write_receipt(
            directory,
            "cancelled",
            None,
            "cancel_before_spawn",
            execution_id=execution_id,
            job_id=str(spec["job_id"]),
            launch_token=str(spec["launch_token"]),
        )

    stdout_handle = (directory / "stdout.out").open("wb")
    stderr_handle = (directory / "stderr.err").open("wb")
    job: WindowsJobObject | None = None
    process: subprocess.Popen[bytes] | None = None
    start_marker: float | None = None
    stop_facts = None
    status = "failed"
    exit_code = 126
    reason = "runner_failed"
    monotonic_deadline = time.monotonic() + max(0.0, (deadline - datetime.now(UTC)).total_seconds())
    output_limit = binding.budget.stdout_stderr_limit_bytes
    workdir_limit = binding.budget.workdir_limit_bytes
    try:
        if os.name == "nt":
            job = WindowsJobObject(memory_limit_bytes=binding.budget.total_memory_mb * 1024 * 1024)
            process = subprocess.Popen(
                [str(executable), str(input_path)],
                cwd=str(directory),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x4,
                close_fds=True,
            )
            try:
                job.assign_pid(process.pid)
                job.resume_pid(process.pid)
            except Exception:
                process.kill()
                process.wait(timeout=5)
                return _write_receipt(
                    directory,
                    "failed",
                    126,
                    "process_group_assignment_failed",
                    execution_id=execution_id,
                    job_id=str(spec.get("job_id", "")),
                    launch_token=str(spec.get("launch_token", "")),
                )
        else:
            process = subprocess.Popen(
                [str(executable), str(input_path)],
                cwd=str(directory),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        start_marker = process_start_marker(process.pid)
        (directory / "orca.pid").write_text(f"{process.pid}\n", encoding="ascii")
        if start_marker is not None:
            (directory / "orca.created").write_text(f"{start_marker:.6f}\n", encoding="ascii")
        while True:
            stdout_handle.flush()
            stderr_handle.flush()
            polled = process.poll()
            if polled is not None:
                exit_code = int(polled)
                status = "succeeded" if exit_code == 0 else "failed"
                reason = "normal_exit" if exit_code == 0 else "nonzero_exit"
                break
            if (
                stdout_handle.tell() + stderr_handle.tell() > output_limit
                or _directory_size(directory) > workdir_limit
            ):
                _stop_process(process, job)
                exit_code = process.wait(timeout=10)
                status = "failed"
                reason = "resource_limit_exceeded"
                break
            cancel = _cancel_pending(root, directory, execution_id)
            if cancel or time.monotonic() >= monotonic_deadline:
                reason = "cancel_requested" if cancel else "wall_time_deadline"
                stop_facts = _controlled_stop(process, job, start_marker, reason)
                exit_code = stop_facts["exit_code"]
                if stop_facts["stop_confirmed"]:
                    status = "cancelled" if cancel else "timed_out"
                elif exit_code is not None:
                    status = "succeeded" if exit_code == 0 else "failed"
                    reason = "natural_exit_during_control"
                else:
                    status, reason = "interrupted", "stop_unconfirmed"
                break
            time.sleep(0.25)
    except Exception:
        if process is not None and process.poll() is None:
            _stop_process(process, job)
            try:
                process.wait(timeout=10)
            except Exception:
                pass
        status = "interrupted"
        exit_code = None
        reason = "runner_exception"
    finally:
        stdout_handle.close()
        stderr_handle.close()
        if job is not None:
            job.close()
    return _write_receipt(
        directory,
        status,
        exit_code,
        reason,
        execution_id=execution_id,
        job_id=str(spec.get("job_id")),
        launch_token=str(spec.get("launch_token")),
        pid=None if process is None else process.pid,
        created=start_marker,
        stop_facts=stop_facts,
        memory_limit_bytes=binding.budget.total_memory_mb * 1024 * 1024
        if os.name == "nt"
        else None,
    )


def _validate_launch_ticket(root: Path, spec: dict[str, object], execution_id: str):
    if spec.get("execution_id") != execution_id:
        raise ValueError("launch execution identity does not match runner arguments")
    if not isinstance(spec.get("job_id"), str) or not str(spec["job_id"]).startswith("job_"):
        raise ValueError("launch job identity is missing")
    if not isinstance(spec.get("launch_token"), str) or not str(spec["launch_token"]).strip():
        raise ValueError("launch token is missing")
    try:
        launch_generation = int(spec["launch_generation"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("launch generation is invalid") from error
    if launch_generation < 1:
        raise ValueError("launch generation is invalid")
    if not isinstance(spec.get("binding_hash"), str):
        raise ValueError("launch binding hash is missing")
    with SQLiteUnitOfWork(root) as uow:
        uow.begin()
        job = LocalJobRepository(uow.connection).get_by_execution(ExecutionId(execution_id))
        if job is None:
            raise ValueError("trusted local job is missing")
        if (
            str(job.job_id) != spec["job_id"]
            or job.launch_token != spec["launch_token"]
            or job.launch_generation != launch_generation
            or job.binding_hash != spec["binding_hash"]
            or spec.get("host_identity") != job.host_identity
            or job.host_identity != host_identity()
            or job.status.value
            not in {
                "reserved",
                "starting",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "timed_out",
                "interrupted",
                "needs_reconciliation",
            }
        ):
            raise ValueError("launch ticket does not match the trusted local job")
        binding = P5RecordRepository(uow.connection).get_exact_p5(
            run_id=job.run_id,
            record_id=job.binding_id,
            record_type="p5.execution_binding",
            model_type=P5ExecutionBinding,
        )
        if binding is None or binding.binding_hash != job.binding_hash:
            raise ValueError("trusted P5 binding is missing or mismatched")
        if spec.get("input_sha256") != binding.input_sha256:
            raise ValueError("launch input hash does not match the binding")
        if spec.get("geometry_hash") != binding.geometry_hash:
            raise ValueError("launch geometry hash does not match the binding")
        if spec.get("xyz_bytes_sha256") != binding.xyz_bytes_sha256:
            raise ValueError("launch geometry bytes hash does not match the binding")
        if binding.executable_sha256 is None:
            raise ValueError("launch executable hash is missing")
        uow.commit()
        return job


def _cancel_pending(root: Path, directory: Path, execution_id: str) -> bool:
    if (directory / "cancel.requested").exists():
        return True
    connection = sqlite3.connect(
        resolve_database_path(root).as_uri() + "?mode=ro", uri=True, timeout=0.05
    )
    try:
        row = connection.execute(
            "SELECT cancel_requested FROM local_jobs WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            raise ValueError("runner lost its trusted job")
        return bool(row[0])
    finally:
        connection.close()


def _stop_process(process: subprocess.Popen[bytes], job: WindowsJobObject | None) -> None:
    if job is not None:
        job.terminate(1)
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.terminate()


CONTROL_EXIT_CODE = 0xE0050001


def _controlled_stop(process, job, expected_created, reason):
    """Recheck natural exit and identity, and return evidence of actual control.

    An accepted OS request alone is not proof: exit status must match this
    termination operation. A racing natural exit retains its natural outcome.
    """
    facts = dict(
        schema="p5-stop/v1",
        reason=reason,
        pid=process.pid,
        expected_created=expected_created,
        observed_created=None,
        identity_matched=False,
        alive_before_request=False,
        request_sent=False,
        stop_confirmed=False,
        tree_stopped=False,
        exit_code=process.poll(),
        requested_at_utc=None,
        confirmed_at_utc=None,
    )
    if facts["exit_code"] is not None:
        return facts
    facts["observed_created"] = process_start_marker(process.pid)
    facts["identity_matched"] = (
        expected_created is not None and facts["observed_created"] == expected_created
    )
    facts["exit_code"] = process.poll()
    if not facts["identity_matched"] or facts["exit_code"] is not None:
        return facts
    facts["alive_before_request"] = True
    facts["requested_at_utc"] = datetime.now(UTC).isoformat()
    try:
        if job is not None:
            job.terminate(CONTROL_EXIT_CODE)
        elif os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            return facts
        facts["request_sent"] = True
        facts["exit_code"] = process.wait(timeout=10)
        expected_code = CONTROL_EXIT_CODE if job is not None else -signal.SIGKILL
        facts["stop_confirmed"] = facts["exit_code"] == expected_code
        if job is not None:
            until = time.monotonic() + 5
            while job.active_process_count() and time.monotonic() < until:
                time.sleep(0.02)
            facts["tree_stopped"] = job.active_process_count() == 0
        # Production real gates are Windows-only. POSIX test control does not
        # fabricate a Job Object tree-empty assertion.
        facts["confirmed_at_utc"] = datetime.now(UTC).isoformat()
    except (OSError, subprocess.TimeoutExpired):
        facts["exit_code"] = process.poll()
    return facts


def _directory_size(directory: Path) -> int:
    total = 0
    for path in directory.iterdir():
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _write_receipt(
    directory: Path,
    status: str,
    exit_code: int | None,
    reason: str,
    *,
    execution_id: str,
    job_id: str,
    launch_token: str,
    pid: int | None = None,
    created: float | None = None,
    memory_limit_bytes: int | None = None,
    stop_facts: dict | None = None,
) -> int:
    for name in ("stdout.out", "stderr.err"):
        if not (directory / name).exists():
            (directory / name).touch()
    payload = {
        "status": status,
        "execution_id": execution_id,
        "job_id": job_id,
        "launch_token": launch_token,
        "exit_code": exit_code,
        "stop_reason": reason,
        "orca_pid": pid,
        "orca_created_at": created,
        "host_identity": host_identity(),
        "physical_start_count": int(pid is not None),
        "stop_facts": stop_facts,
        "resource_enforcement": {
            "process_tree": "windows_job_object" if os.name == "nt" else "posix_process_group",
            "job_memory_limit_bytes": memory_limit_bytes,
            "memory_enforced": memory_limit_bytes is not None,
            "deadline_clock": "monotonic",
        },
        "finished_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    temporary = directory / ".exit_receipt.json.tmp"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, directory / "exit_receipt.json")
    return 0 if status == "succeeded" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orca-agent-local-runner")
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args(argv)
    return run_supervisor(args.state_root, args.execution_id)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_supervisor"]
