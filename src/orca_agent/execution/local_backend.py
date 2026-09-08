"""P5 fake and local-ORCA backend adapters."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
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

from .control_evidence import control_gate_status
from .launch_ticket import consume_ticket, permit_fields
from .orca_config import (
    available_physical_memory_mb,
    runtime_config,
    validate_runtime_config,
)
from .orca_parser import ATOMIC_MASSES
from .output_contract import BOHR_TO_ANGSTROM, HESSIAN_NAME, OPTIMIZED_XYZ_NAME
from .ports import (
    CancellationObservation,
    JobObservation,
    LaunchObservation,
    LaunchRequest,
    OutputManifest,
)
from .windows_job import host_identity, process_start_marker
from .work_paths import execution_directory


class _BackendBase:
    def __init__(self, state_root: str | Path) -> None:
        self.state_root = Path(state_root).resolve()
        self.work_root = self.state_root / "work"
        self.work_root.mkdir(parents=True, exist_ok=True)

    def _workdir(self, execution_id: str) -> Path:
        return execution_directory(self.state_root, str(execution_id), create=True)

    def _existing_workdir(self, execution_id: str) -> Path | None:
        try:
            return execution_directory(self.state_root, str(execution_id), create=False)
        except ValueError as error:
            if str(error) == "execution directory is missing":
                return None
            raise

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
        receipt = self._read_receipt(directory, execution_id=str(launch_request.job.execution_id))
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
        consume_ticket(
            self.state_root,
            {
                "execution_id": str(launch_request.job.execution_id),
                "launch_token": launch_request.job.launch_token,
                "binding_hash": launch_request.binding.binding_hash,
                "permit": permit_fields(launch_request.permit),
            },
            now=launch_request.requested_at_utc,
        )
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
            self._write_atomic(directory / OPTIMIZED_XYZ_NAME, launch_request.geometry_bytes)
            lines.extend(
                [
                    "THE OPTIMIZATION HAS CONVERGED",
                    "CARTESIAN COORDINATES (ANGSTROEM)",
                    "---------------------------------",
                    "\n".join(launch_request.geometry_bytes.decode("utf-8").splitlines()[2:]),
                    "",
                ]
            )
        if kind == "freq":
            atoms = launch_request.geometry_bytes.decode("utf-8").splitlines()[2:]
            dimension = len(atoms) * 3
            values = [0.0] * (dimension - 3) + [1595.321, 3657.223, 3755.120]
            lines.extend(
                ["VIBRATIONAL FREQUENCIES"] + [f"{i}: {v:.6f} cm**-1" for i, v in enumerate(values)]
            )
            hess = ["$hessian", str(dimension), " ".join(map(str, range(dimension)))]
            hess.extend(
                f"{i} " + " ".join("1.000000" if i == j else "0.000000" for j in range(dimension))
                for i in range(dimension)
            )
            hess.extend(
                ["$vibrational_frequencies", str(dimension)]
                + [f"{i} {v:.6f}" for i, v in enumerate(values)]
            )
            hess.extend(["$atoms", str(len(atoms))])
            for atom in atoms:
                symbol, *xyz = atom.split()
                hess.append(
                    f"{symbol} {ATOMIC_MASSES[symbol]} "
                    + " ".join(f"{float(c) / BOHR_TO_ANGSTROM:.12f}" for c in xyz)
                )
            hess.append("$end")
            self._write_atomic(directory / HESSIAN_NAME, ("\n".join(hess) + "\n").encode())
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
            hessian_path=(directory / HESSIAN_NAME)
            if (directory / HESSIAN_NAME).exists()
            else None,
            optimized_xyz_path=(directory / OPTIMIZED_XYZ_NAME)
            if (directory / OPTIMIZED_XYZ_NAME).exists()
            else None,
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

    def _read_receipt(self, directory, *, execution_id=None):
        receipt = super()._read_receipt(directory, execution_id=execution_id)
        if receipt is None:
            return None
        job = self._trusted_job(execution_id)
        if (
            job is None
            or job.backend_kind != "local_orca"
            or receipt.get("job_id") != str(job.job_id)
            or receipt.get("launch_token") != job.launch_token
            or receipt.get("host_identity") != job.host_identity
            or job.host_identity != host_identity()
        ):
            raise LaunchStateUnknown("runner receipt does not match trusted execution identity")
        facts = receipt.get("stop_facts")
        enforcement = receipt.get("resource_enforcement") or {}
        windows_control = os.name == "nt" or (
            isinstance(enforcement, dict)
            and enforcement.get("process_tree") == "windows_job_object"
        )
        if facts is not None and not isinstance(facts, dict):
            return {**receipt, "status": "needs_reconciliation"}
        if isinstance(facts, dict) and windows_control:
            status = receipt.get("status")
            if (
                (facts.get("request_sent") and facts.get("tree_stopped") is not True)
                or (
                    status in {"cancelled", "timed_out"}
                    and control_gate_status(receipt, status) != "PASS"
                )
                or (facts.get("stop_confirmed") and status not in {"cancelled", "timed_out"})
            ):
                receipt = {**receipt, "status": "needs_reconciliation"}
        return receipt

    def _trusted_job(self, execution_id):
        from orca_agent.domain.ids import ExecutionId
        from orca_agent.infrastructure.p5_records import LocalJobRepository
        from orca_agent.infrastructure.sqlite import resolve_database_path

        connection = sqlite3.connect(
            resolve_database_path(self.state_root).as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            return LocalJobRepository(connection).get_by_execution(ExecutionId(execution_id))
        finally:
            connection.close()

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
        if launch_request.job.status not in {
            P5JobStatus.RESERVED,
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
        expected_runtime = runtime_config(
            state_root=self.state_root,
            executable=executable,
            orca_version=orca_version,
            profile_hash=launch_request.binding.feature_profile_hash,
            probe=False,
            nprocs=launch_request.node.budget.nprocs,
            implicit_threads=1,
            parallel=launch_request.node.budget.nprocs > 1,
        )
        supplied_runtime = launch_request.runtime_config or expected_runtime
        try:
            supplied_runtime = validate_runtime_config(
                supplied_runtime,
                expected_nprocs=launch_request.node.budget.nprocs,
                expected_parallel=launch_request.node.budget.nprocs > 1,
            )
        except (TypeError, ValueError) as error:
            raise LaunchStateUnknown("local ORCA runtime configuration is invalid") from error
        if supplied_runtime != expected_runtime:
            raise LaunchStateUnknown("local ORCA runtime configuration does not match its binding")
        if supplied_runtime["runtime_config_hash"] != launch_request.binding.runtime_config_hash:
            raise LaunchStateUnknown("local ORCA runtime hash does not match its binding")
        execution_id = str(launch_request.job.execution_id)
        directory = self._existing_workdir(execution_id)
        if directory is not None:
            receipt_path = directory / "exit_receipt.json"
            if receipt_path.exists():
                receipt = self._read_receipt(directory, execution_id=execution_id)
                if receipt is None:
                    raise LaunchStateUnknown("local ORCA receipt is not trustworthy")
                status = self._status(receipt.get("status"))
                if status in {
                    P5JobStatus.SUCCEEDED,
                    P5JobStatus.FAILED,
                    P5JobStatus.CANCELLED,
                    P5JobStatus.TIMED_OUT,
                    P5JobStatus.INTERRUPTED,
                }:
                    return LaunchObservation(
                        execution_id=execution_id,
                        job_id=str(launch_request.job.job_id),
                        status=status,
                        started=False,
                        physical_start_count=1,
                        receipt_path=receipt_path,
                        data_origin=P5DataOrigin.ORCA_LOCAL,
                        message="replayed local runner receipt",
                    )
                raise LaunchStateUnknown("local ORCA launch has no trustworthy terminal receipt")
            if _has_launch_evidence(directory):
                raise LaunchStateUnknown("local ORCA launch has no trustworthy terminal receipt")

        if launch_request.node.budget.nprocs > 1 and os.name == "nt":
            available_mb = available_physical_memory_mb()
            required_mb = launch_request.node.budget.total_memory_mb
            if available_mb is None or available_mb < required_mb:
                raise ResourceLimitExceeded(
                    "four-core ORCA launch requires "
                    f"{required_mb} MB available physical memory; observed {available_mb} MB"
                )
        directory = self._materialize(launch_request)
        receipt_path = directory / "exit_receipt.json"
        if receipt_path.exists():
            receipt = self._read_receipt(directory, execution_id=execution_id)
            if receipt is None:
                raise LaunchStateUnknown("local ORCA receipt is not trustworthy")
            status = self._status(receipt.get("status"))
            if status in {
                P5JobStatus.SUCCEEDED,
                P5JobStatus.FAILED,
                P5JobStatus.CANCELLED,
                P5JobStatus.TIMED_OUT,
                P5JobStatus.INTERRUPTED,
            }:
                return LaunchObservation(
                    execution_id=execution_id,
                    job_id=str(launch_request.job.job_id),
                    status=status,
                    started=False,
                    physical_start_count=1,
                    receipt_path=receipt_path,
                    data_origin=P5DataOrigin.ORCA_LOCAL,
                    message="replayed local runner receipt",
                )
            raise LaunchStateUnknown("local ORCA launch has no trustworthy terminal receipt")
        if _has_launch_evidence(directory):
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
            "permit": permit_fields(launch_request.permit),
            "runtime_config": supplied_runtime,
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
                execution_id,
            ],
            cwd=str(self.state_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            close_fds=True,
        )
        self._write_atomic(
            directory / "supervisor.json",
            json.dumps(
                {
                    "pid": supervisor.pid,
                    "created": process_start_marker(supervisor.pid),
                    "execution_id": str(launch_request.job.execution_id),
                    "launch_token": launch_request.job.launch_token,
                    "host_identity": host_identity(),
                }
            ).encode(),
        )
        return LaunchObservation(
            execution_id=str(launch_request.job.execution_id),
            job_id=str(launch_request.job.job_id),
            status=self._await_consumed(launch_request, supervisor),
            started=True,
            physical_start_count=1,
            supervisor_pid=supervisor.pid,
            data_origin=P5DataOrigin.ORCA_LOCAL,
        )

    def _await_consumed(self, request, supervisor):
        from orca_agent.domain.ids import ExecutionId
        from orca_agent.infrastructure.p5_records import LocalJobRepository
        from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

        until = time.monotonic() + 5.0
        while time.monotonic() < until:
            with SQLiteUnitOfWork(self.state_root) as uow:
                current = LocalJobRepository(uow.connection).get_by_execution(
                    ExecutionId(request.job.execution_id)
                )
            if current is not None and current.launch_consumed_at_utc is not None:
                return P5JobStatus.STARTING
            if supervisor.poll() is not None:
                raise LaunchStateUnknown("supervisor exited before consuming its launch permit")
            time.sleep(0.025)
        raise LaunchStateUnknown("supervisor did not acknowledge launch before handshake deadline")

    def poll(self, execution_ref: str) -> JobObservation:
        directory = self._workdir(execution_ref)
        receipt = self._read_receipt(directory, execution_id=execution_ref)
        if receipt is None:
            if not _supervisor_is_alive(directory, execution_ref):
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
            hessian_path=(directory / HESSIAN_NAME)
            if (directory / HESSIAN_NAME).exists()
            else None,
            optimized_xyz_path=(directory / OPTIMIZED_XYZ_NAME)
            if (directory / OPTIMIZED_XYZ_NAME).exists()
            else None,
        )

    def cancel(self, execution_ref: str, cancellation_ref: str) -> CancellationObservation:
        directory = self._workdir(execution_ref)
        self._write_atomic(directory / "cancel.requested", f"{cancellation_ref}\n".encode())
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            observation = self.poll(execution_ref)
            if observation.status in {
                P5JobStatus.NEEDS_RECONCILIATION,
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


def _has_launch_evidence(directory: Path) -> bool:
    return any(
        (directory / name).exists()
        for name in ("launch.json", "supervisor.json", "orca.pid", "orca.created")
    )


def _read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _supervisor_is_alive(directory: Path, execution_id: str) -> bool:
    try:
        record = json.loads((directory / "supervisor.json").read_text())
        spec = json.loads((directory / "launch.json").read_text())
        return (
            record["execution_id"] == execution_id == spec["execution_id"]
            and record["launch_token"] == spec["launch_token"]
            and record["host_identity"] == spec["host_identity"] == host_identity()
            and type(record["pid"]) is int
            and record["pid"] > 0
            and record["created"] is not None
            and process_start_marker(record["pid"]) == record["created"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False
