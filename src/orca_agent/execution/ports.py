"""Narrow backend contract for P5 physical execution facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from orca_agent.domain.p5 import (
    P5DataOrigin,
    P5ExecutionBinding,
    P5ExecutionNode,
    P5JobRecord,
    P5JobStatus,
)


@dataclass(frozen=True)
class LaunchRequest:
    state_root: Path
    job: P5JobRecord
    binding: P5ExecutionBinding
    node: P5ExecutionNode
    input_bytes: bytes
    geometry_bytes: bytes
    executable: Path | None
    allow_real: bool
    requested_at_utc: datetime
    permit: object | None = None


@dataclass(frozen=True)
class LaunchObservation:
    execution_id: str
    job_id: str
    status: P5JobStatus
    started: bool
    physical_start_count: int
    supervisor_pid: int | None = None
    orca_pid: int | None = None
    receipt_path: Path | None = None
    data_origin: P5DataOrigin = P5DataOrigin.FAKE_FIXTURE
    message: str | None = None


@dataclass(frozen=True)
class JobObservation:
    execution_id: str
    status: P5JobStatus
    exit_code: int | None
    data_origin: P5DataOrigin
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    hessian_path: Path | None = None
    optimized_xyz_path: Path | None = None
    message: str | None = None


@dataclass(frozen=True)
class CancellationObservation:
    execution_id: str
    status: P5JobStatus
    stopped: bool
    data_origin: P5DataOrigin
    message: str | None = None


@dataclass(frozen=True)
class OutputManifest:
    execution_id: str
    status: P5JobStatus
    data_origin: P5DataOrigin
    input_path: Path
    geometry_path: Path
    stdout_path: Path
    stderr_path: Path
    hessian_path: Path | None
    optimized_xyz_path: Path | None
    exit_code: int | None


class ExecutionBackend(Protocol):
    """Exactly four operations used by the P5 gateway."""

    def start_or_reconcile(self, launch_request: LaunchRequest) -> LaunchObservation: ...

    def poll(self, execution_ref: str) -> JobObservation: ...

    def cancel(self, execution_ref: str, cancellation_ref: str) -> CancellationObservation: ...

    def collect(self, execution_ref: str) -> OutputManifest: ...


__all__ = [
    "CancellationObservation",
    "ExecutionBackend",
    "JobObservation",
    "LaunchObservation",
    "LaunchRequest",
    "OutputManifest",
]
