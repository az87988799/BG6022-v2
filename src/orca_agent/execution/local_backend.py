"""P5 fake and local-ORCA backend adapters."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from orca_agent.application.p5_errors import (
    ExecutableVersionMismatch,
    LaunchStateUnknown,
    RealExecutionDisabled,
    ResourceLimitExceeded,
)
from orca_agent.domain.p5 import (
    P5DataOrigin,
    P5JobStatus,
)

from .ports import (
    CancellationObservation,
    JobObservation,
    LaunchObservation,
    LaunchRequest,
    OutputManifest,
)
from .windows_job import host_identity


class _BackendBase:
    def __init__(self, state_root: str | Path) -> None:
        self.state_root = Path(state_root).resolve()
        self.work_root = self.state_root / "work"
        self.work_root.mkdir(parents=True, exist_ok=True)

    def _workdir(self, execution_id: str) -> Path:
        if not execution_id.startswith("execution_") or any(
            part in execution_id for part in ("..", "/", "\\")
        ):
            raise ValueError("execution ID cannot be used as a work directory name")
        directory = self.work_root / execution_id
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ResourceLimitExceeded("execution work directory is a symlink")
        resolved = directory.resolve()
        try:
            resolved.relative_to(self.work_root)
        except ValueError as error:
            raise ResourceLimitExceeded("execution work directory escapes state root") from error
        return resolved

    @staticmethod
    def _write_atomic(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
        try:
            with temporary.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _materialize(self, request: LaunchRequest) -> Path:
        directory = self._workdir(request.job.execution_id)
        input_path = directory / "input.inp"
        geometry_path = directory / "geometry.xyz"
        if input_path.exists() and input_path.read_bytes() != request.input_bytes:
            raise ResourceLimitExceeded("frozen input bytes were changed")
        if geometry_path.exists() and geometry_path.read_bytes() != request.geometry_bytes:
            raise ResourceLimitExceeded("frozen geometry bytes were changed")
        if not input_path.exists():
            self._write_atomic(input_path, request.input_bytes)
        if not geometry_path.exists():
            self._write_atomic(geometry_path, request.geometry_bytes)
        return directory

    @staticmethod
    def _read_receipt(
        directory: Path, *, execution_id: str | None = None
    ) -> dict[str, object] | None:
        path = directory / "exit_receipt.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            if execution_id is not None and payload.get("execution_id") != execution_id:
                raise ResourceLimitExceeded("exit receipt execution identity does not match")
            return payload
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _status(value: object) -> P5JobStatus:
        try:
            return P5JobStatus(str(value))
        except ValueError:
            return P5JobStatus.NEEDS_RECONCILIATION


class FakeExecutionBackend(_BackendBase):
    """Deterministic lifecycle substitute, never an ORCA result."""

    backend_kind = "fake"

    def start_or_reconcile(self, launch_request: LaunchRequest) -> LaunchObservation:
        directory = self._materialize(launch_request)
        receipt = self._read_receipt(
            directory, execution_id=str(launch_request.job.execution_id)
        )
        if receipt is not None:
            return LaunchObservation(
                execution_id=str(launch_request.job.execution_id),
                job_id=str(launch_request.job.job_id),
                status=self._status(receipt.get("status")),
                started=False,
                physical_start_count=int(receipt.get("physical_start_count", 1)),
                receipt_path=directory / "exit_receipt.json",
                data_origin=P5DataOrigin.FAKE_FIXTURE,
                message="replayed simulated receipt",
            )
        count_path = directory / "physical_start_count.txt"
        count = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
        if count >= 1:
            raise RuntimeError("fake launch evidence exists without a terminal receipt")
        self._write_atomic(count_path, b"1\n")
        kind = launch_request.node.kind.value
        digest = hashlib.sha256(launch_request.geometry_bytes + kind.encode()).hexdigest()
        energy = -(75.0 + int(digest[:8], 16) / 1_000_000_000)
        lines = [
            "Program Version 6.1.0",
            "SCF CONVERGED",
            f"FINAL {'SINGLE POINT ' if kind == 'sp' else ''}ENERGY     {energy:.12f}",
        ]
        if kind == "opt":
            lines.extend(
                [
                    "THE OPTIMIZATION HAS CONVERGED",
                    "P5 OPTIMIZED XYZ BEGIN",
                    launch_request.geometry_bytes.decode("utf-8").rstrip("\n"),
                    "P5 OPTIMIZED XYZ END",
                ]
            )
        if kind == "freq":
            lines.extend(
                ["VIBRATIONAL FREQUENCIES", "  1:  1595.3210", "  2:  3657.2230", "  3:  3755.1200"]
            )
            dimension = len(launch_request.geometry_bytes.decode("utf-8").splitlines()) - 2
            self._write_atomic(
                directory / "frequency.hess", f"$hessian\n{dimension * 3}\n$end\n".encode()
            )
        lines.append("****ORCA TERMINATED NORMALLY****")
        self._write_atomic(directory / "stdout.out", ("\n".join(lines) + "\n").encode("utf-8"))
        self._write_atomic(directory / "stderr.err", b"")
        payload = {
            "status": P5JobStatus.SUCCEEDED.value,
            "exit_code": 0,
            "physical_start_count": 1,
            "data_origin": P5DataOrigin.FAKE_FIXTURE.value,
            "execution_id": str(launch_request.job.execution_id),
            "job_id": str(launch_request.job.job_id),
            "finished_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        self._write_atomic(
            directory / "exit_receipt.json", json.dumps(payload, sort_keys=True).encode("utf-8")
        )
        return LaunchObservation(
            execution_id=str(launch_request.job.execution_id),
            job_id=str(launch_request.job.job_id),
            status=P5JobStatus.SUCCEEDED,
            started=True,
            physical_start_count=1,
            receipt_path=directory / "exit_receipt.json",
            data_origin=P5DataOrigin.FAKE_FIXTURE,
        )

    def poll(self, execution_ref: str) -> JobObservation:
        directory = self._workdir(execution_ref)
        receipt = self._read_receipt(directory, execution_id=execution_ref)
        if receipt is None:
            return JobObservation(
                execution_id=execution_ref,
                status=P5JobStatus.RUNNING,
                exit_code=None,
                data_origin=P5DataOrigin.FAKE_FIXTURE,
            )
        status = self._status(receipt.get("status"))
        return JobObservation(
            execution_id=execution_ref,
            status=status,
            exit_code=receipt.get("exit_code")
            if isinstance(receipt.get("exit_code"), int)
            else None,
            data_origin=P5DataOrigin.FAKE_FIXTURE,
            stdout_path=directory / "stdout.out",
            stderr_path=directory / "stderr.err",
            hessian_path=(directory / "frequency.hess")
            if (directory / "frequency.hess").exists()
            else None,
            optimized_xyz_path=None,
        )

    def cancel(self, execution_ref: str, cancellation_ref: str) -> CancellationObservation:
        observation = self.poll(execution_ref)
        return CancellationObservation(
            execution_ref,
            observation.status,
            False,
            P5DataOrigin.FAKE_FIXTURE,
            "simulated jobs complete immediately",
        )

    def collect(self, execution_ref: str) -> OutputManifest:
        observation = self.poll(execution_ref)
        if observation.status not in {
            P5JobStatus.SUCCEEDED,
            P5JobStatus.FAILED,
            P5JobStatus.CANCELLED,
            P5JobStatus.TIMED_OUT,
        }:
            raise RuntimeError("fake job is not terminal")
        directory = self._workdir(execution_ref)
        return OutputManifest(
            execution_id=execution_ref,
            status=observation.status,
            data_origin=P5DataOrigin.FAKE_FIXTURE,
            input_path=directory / "input.inp",
            geometry_path=directory / "geometry.xyz",
            stdout_path=directory / "stdout.out",
            stderr_path=directory / "stderr.err",
            hessian_path=observation.hessian_path,
            optimized_xyz_path=observation.optimized_xyz_path,
            exit_code=observation.exit_code,
        )


class LocalOrcaBackend(_BackendBase):
    """Launch ORCA only through the fixed local runner module."""

    backend_kind = "local_orca"

    def start_or_reconcile(self, launch_request: LaunchRequest) -> LaunchObservation:
        if not launch_request.allow_real:
            raise RealExecutionDisabled("real ORCA execution is disabled")
        executable = launch_request.executable
        if executable is None:
            raise ExecutableVersionMismatch("an explicit ORCA executable is required")
        executable = executable.resolve()
        if executable.suffix.casefold() != ".exe" or not executable.is_file():
            raise ExecutableVersionMismatch("P5 accepts only a verified ORCA .exe")
        if launch_request.job.backend_kind != self.backend_kind:
            raise LaunchStateUnknown("job backend kind does not match the local ORCA backend")
        if launch_request.job.launch_consumed_at_utc is None or launch_request.job.status not in {
            P5JobStatus.STARTING,
            P5JobStatus.RUNNING,
        }:
            raise LaunchStateUnknown("local ORCA launch ticket is not consumed and active")
        if (
            launch_request.job.binding_hash != launch_request.binding.binding_hash
            or launch_request.job.input_manifest_hash != launch_request.binding.input_manifest_hash
            or launch_request.job.geometry_hash != launch_request.binding.geometry_hash
        ):
            raise LaunchStateUnknown("local ORCA launch ticket does not match the binding")
        if (
            hashlib.sha256(launch_request.input_bytes).hexdigest()
            != launch_request.binding.input_sha256
            or hashlib.sha256(launch_request.geometry_bytes).hexdigest()
            != launch_request.binding.xyz_bytes_sha256
        ):
            raise ResourceLimitExceeded("local ORCA launch bytes do not match the binding")
        orca_version = launch_request.binding.orca_version
        if orca_version is None or not orca_version.startswith("6.1"):
            raise ExecutableVersionMismatch("P5 requires an explicit ORCA 6.1 version")
        actual_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
        if launch_request.binding.executable_sha256 != actual_hash:
            raise ExecutableVersionMismatch("ORCA executable hash changed after approval")
        directory = self._materialize(launch_request)
        receipt = self._read_receipt(
            directory, execution_id=str(launch_request.job.execution_id)
        )
        if receipt is not None:
            return LaunchObservation(
                execution_id=str(launch_request.job.execution_id),
                job_id=str(launch_request.job.job_id),
                status=self._status(receipt.get("status")),
                started=False,
                physical_start_count=1,
                receipt_path=directory / "exit_receipt.json",
                data_origin=P5DataOrigin.ORCA_LOCAL,
                message="replayed local runner receipt",
            )
        if (directory / "launch.json").exists():
            raise LaunchStateUnknown("local ORCA launch has no trustworthy terminal receipt")
        launch_spec = {
            "executable": str(executable),
            "input": "input.inp",
            "deadline_utc": launch_request.job.deadline_utc.isoformat().replace("+00:00", "Z"),
            "execution_id": str(launch_request.job.execution_id),
            "job_id": str(launch_request.job.job_id),
            "launch_token": launch_request.job.launch_token,
            "launch_generation": launch_request.job.launch_generation,
            "binding_hash": launch_request.binding.binding_hash,
            "input_sha256": launch_request.binding.input_sha256,
            "geometry_hash": launch_request.binding.geometry_hash,
            "xyz_bytes_sha256": launch_request.binding.xyz_bytes_sha256,
            "host_identity": host_identity(),
        }
        self._write_atomic(
            directory / "launch.json", json.dumps(launch_spec, sort_keys=True).encode("utf-8")
        )
        supervisor = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "orca_agent.execution.local_runner",
                "--state-root",
                str(self.state_root),
                "--execution-id",
                str(launch_request.job.execution_id),
            ],
            cwd=str(self.state_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            close_fds=True,
        )
        self._write_atomic(directory / "supervisor.pid", f"{supervisor.pid}\n".encode("ascii"))
        return LaunchObservation(
            execution_id=str(launch_request.job.execution_id),
            job_id=str(launch_request.job.job_id),
            status=P5JobStatus.STARTING,
            started=True,
            physical_start_count=1,
            supervisor_pid=supervisor.pid,
            data_origin=P5DataOrigin.ORCA_LOCAL,
        )

    def poll(self, execution_ref: str) -> JobObservation:
        directory = self._workdir(execution_ref)
        receipt = self._read_receipt(directory, execution_id=execution_ref)
        if receipt is None:
            supervisor_pid = _read_pid(directory / "supervisor.pid")
            if supervisor_pid is None or not _pid_is_alive(supervisor_pid):
                return JobObservation(
                    execution_id=execution_ref,
                    status=P5JobStatus.NEEDS_RECONCILIATION,
                    exit_code=None,
                    data_origin=P5DataOrigin.ORCA_LOCAL,
                    message="local runner has no trustworthy terminal receipt or live supervisor",
                )
            return JobObservation(
                execution_id=execution_ref,
                status=P5JobStatus.RUNNING,
                exit_code=None,
                data_origin=P5DataOrigin.ORCA_LOCAL,
            )
        status = self._status(receipt.get("status"))
        return JobObservation(
            execution_id=execution_ref,
            status=status,
            exit_code=receipt.get("exit_code")
            if isinstance(receipt.get("exit_code"), int)
            else None,
            data_origin=P5DataOrigin.ORCA_LOCAL,
            stdout_path=directory / "stdout.out",
            stderr_path=directory / "stderr.err",
            hessian_path=(directory / "frequency.hess")
            if (directory / "frequency.hess").exists()
            else None,
            optimized_xyz_path=None,
        )

    def cancel(self, execution_ref: str, cancellation_ref: str) -> CancellationObservation:
        directory = self._workdir(execution_ref)
        self._write_atomic(directory / "cancel.requested", f"{cancellation_ref}\n".encode())
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            observation = self.poll(execution_ref)
            if observation.status in {
                P5JobStatus.CANCELLED,
                P5JobStatus.TIMED_OUT,
                P5JobStatus.SUCCEEDED,
                P5JobStatus.FAILED,
                P5JobStatus.INTERRUPTED,
            }:
                return CancellationObservation(
                    execution_ref,
                    observation.status,
                    observation.status is P5JobStatus.CANCELLED,
                    P5DataOrigin.ORCA_LOCAL,
                )
            time.sleep(0.1)
        return CancellationObservation(
            execution_ref,
            P5JobStatus.NEEDS_RECONCILIATION,
            False,
            P5DataOrigin.ORCA_LOCAL,
            "runner stop is not yet confirmed",
        )

    def collect(self, execution_ref: str) -> OutputManifest:
        observation = self.poll(execution_ref)
        if observation.status not in {
            P5JobStatus.SUCCEEDED,
            P5JobStatus.FAILED,
            P5JobStatus.CANCELLED,
            P5JobStatus.TIMED_OUT,
            P5JobStatus.INTERRUPTED,
        }:
            raise RuntimeError("local ORCA job is not terminal")
        directory = self._workdir(execution_ref)
        return OutputManifest(
            execution_id=execution_ref,
            status=observation.status,
            data_origin=P5DataOrigin.ORCA_LOCAL,
            input_path=directory / "input.inp",
            geometry_path=directory / "geometry.xyz",
            stdout_path=directory / "stdout.out",
            stderr_path=directory / "stderr.err",
            hessian_path=observation.hessian_path,
            optimized_xyz_path=observation.optimized_xyz_path,
            exit_code=observation.exit_code,
        )


__all__ = ["FakeExecutionBackend", "LocalOrcaBackend"]


def _read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
