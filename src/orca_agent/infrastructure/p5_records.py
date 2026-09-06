"""P5 record codec and durable local-job fact projection."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.ids import ActionId, ExecutionId, JobId, RunId, WorkflowRecordId
from orca_agent.domain.p5 import P5JobRecord, P5JobStatus

from .clock import format_utc, parse_utc
from .p3_records import P5RecordRepository
from .repositories import stored_int

_JOB_COLUMNS = (
    "job_id, run_id, action_id, execution_id, idempotency_key, binding_id, binding_hash, "
    "input_manifest_hash, geometry_hash, backend_kind, launch_token, launch_generation, "
    "launch_reserved_at_utc, launch_consumed_at_utc, status, supervisor_pid, "
    "supervisor_created_at, orca_pid, orca_created_at, host_identity, executable_sha256, "
    "job_directory_id, deadline_utc, cancel_requested, stop_reason, last_observation_sequence, "
    "last_observation_hash, exit_code, terminal_receipt_id, terminal_receipt_hash, "
    "terminal_at_utc"
)


class LocalJobRepository:
    """Persist and fence one physical launch ticket per P5 action."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert(self, job: P5JobRecord) -> None:
        try:
            self.connection.execute(
                f"INSERT INTO local_jobs({_JOB_COLUMNS}) VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?)",
                (
                    str(job.job_id),
                    str(job.run_id),
                    str(job.action_id),
                    str(job.execution_id),
                    job.idempotency_key,
                    str(job.binding_id),
                    job.binding_hash,
                    job.input_manifest_hash,
                    job.geometry_hash,
                    job.backend_kind,
                    job.launch_token,
                    job.launch_generation,
                    format_utc(job.launch_reserved_at_utc),
                    None
                    if job.launch_consumed_at_utc is None
                    else format_utc(job.launch_consumed_at_utc),
                    job.status.value,
                    job.supervisor_pid,
                    job.supervisor_created_at,
                    job.orca_pid,
                    job.orca_created_at,
                    job.host_identity,
                    job.executable_sha256,
                    job.job_directory_id,
                    format_utc(job.deadline_utc),
                    int(job.cancel_requested),
                    job.stop_reason,
                    job.last_observation_sequence,
                    job.last_observation_hash,
                    job.exit_code,
                    None if job.terminal_receipt_id is None else str(job.terminal_receipt_id),
                    job.terminal_receipt_hash,
                    None if job.terminal_at_utc is None else format_utc(job.terminal_at_utc),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StateIntegrityError("local job identity violates an invariant") from error

    def get(self, job_id: JobId) -> P5JobRecord | None:
        row = self.connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM local_jobs WHERE job_id = ?",
            (str(job_id),),
        ).fetchone()
        return None if row is None else self._parse(row)

    def get_by_execution(self, execution_id: ExecutionId) -> P5JobRecord | None:
        row = self.connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM local_jobs WHERE execution_id = ?",
            (str(execution_id),),
        ).fetchone()
        return None if row is None else self._parse(row)

    def get_by_action(self, action_id: ActionId) -> P5JobRecord | None:
        row = self.connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM local_jobs WHERE action_id = ?",
            (str(action_id),),
        ).fetchone()
        return None if row is None else self._parse(row)

    def get_by_run(self, run_id: RunId) -> P5JobRecord | None:
        row = self.connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM local_jobs WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
            (str(run_id),),
        ).fetchone()
        return None if row is None else self._parse(row)

    def list_for_run(self, run_id: RunId) -> tuple[tuple[JobId, P5JobRecord], ...]:
        rows = self.connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM local_jobs WHERE run_id = ? ORDER BY rowid",
            (str(run_id),),
        ).fetchall()
        return tuple((JobId(str(row[0])), self._parse(row)) for row in rows)

    def consume_launch_ticket(
        self, *, execution_id: ExecutionId, generation: int, now: datetime
    ) -> bool:
        cursor = self.connection.execute(
            "UPDATE local_jobs SET status = 'starting', launch_consumed_at_utc = ? "
            "WHERE execution_id = ? AND launch_generation = ? AND status = 'reserved' "
            "AND launch_consumed_at_utc IS NULL AND cancel_requested = 0",
            (format_utc(now), str(execution_id), generation),
        )
        return cursor.rowcount == 1

    def update_runtime(
        self,
        *,
        execution_id: ExecutionId,
        status: P5JobStatus,
        supervisor_pid: int | None = None,
        supervisor_created_at: float | None = None,
        orca_pid: int | None = None,
        orca_created_at: float | None = None,
        cancel_requested: bool | None = None,
        stop_reason: str | None = None,
        last_observation_sequence: int | None = None,
        last_observation_hash: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        sets = ["status = ?"]
        values: list[object] = [status.value]
        for column, value in (
            ("supervisor_pid", supervisor_pid),
            ("supervisor_created_at", supervisor_created_at),
            ("orca_pid", orca_pid),
            ("orca_created_at", orca_created_at),
            ("stop_reason", stop_reason),
            ("last_observation_sequence", last_observation_sequence),
            ("last_observation_hash", last_observation_hash),
            ("exit_code", exit_code),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                values.append(value)
        if cancel_requested is not None:
            sets.append("cancel_requested = ?")
            values.append(int(cancel_requested))
        values.append(str(execution_id))
        self.connection.execute(
            f"UPDATE local_jobs SET {', '.join(sets)} WHERE execution_id = ?", values
        )

    def request_cancel(self, execution_id: ExecutionId) -> None:
        self.connection.execute(
            "UPDATE local_jobs SET cancel_requested = 1 WHERE execution_id = ? "
            "AND status NOT IN ('succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted')",
            (str(execution_id),),
        )

    def mark_terminal(
        self,
        *,
        execution_id: ExecutionId,
        status: P5JobStatus,
        exit_code: int | None,
        stop_reason: str | None,
        terminal_receipt_id: WorkflowRecordId | None,
        terminal_receipt_hash: str | None,
        terminal_at_utc: datetime,
    ) -> None:
        if status not in {
            P5JobStatus.SUCCEEDED,
            P5JobStatus.FAILED,
            P5JobStatus.CANCELLED,
            P5JobStatus.TIMED_OUT,
            P5JobStatus.INTERRUPTED,
        }:
            raise ValueError("local job terminal status is invalid")
        self.connection.execute(
            "UPDATE local_jobs SET status = ?, exit_code = ?, stop_reason = ?, "
            "terminal_receipt_id = ?, terminal_receipt_hash = ?, terminal_at_utc = ? "
            "WHERE execution_id = ?",
            (
                status.value,
                exit_code,
                stop_reason,
                None if terminal_receipt_id is None else str(terminal_receipt_id),
                terminal_receipt_hash,
                format_utc(terminal_at_utc),
                str(execution_id),
            ),
        )

    def _parse(self, row: sqlite3.Row) -> P5JobRecord:
        try:
            return P5JobRecord(
                job_id=JobId(str(row[0])),
                run_id=RunId(str(row[1])),
                action_id=ActionId(str(row[2])),
                execution_id=ExecutionId(str(row[3])),
                idempotency_key=str(row[4]),
                binding_id=WorkflowRecordId(str(row[5])),
                binding_hash=str(row[6]),
                input_manifest_hash=str(row[7]),
                geometry_hash=str(row[8]),
                backend_kind=str(row[9]),
                launch_token=str(row[10]),
                launch_generation=stored_int(row[11], what="launch generation", minimum=1),
                launch_reserved_at_utc=parse_utc(str(row[12])),
                launch_consumed_at_utc=None if row[13] is None else parse_utc(str(row[13])),
                status=P5JobStatus(str(row[14])),
                supervisor_pid=row[15],
                supervisor_created_at=row[16],
                orca_pid=row[17],
                orca_created_at=row[18],
                host_identity=str(row[19]),
                executable_sha256=None if row[20] is None else str(row[20]),
                job_directory_id=str(row[21]),
                deadline_utc=parse_utc(str(row[22])),
                cancel_requested=bool(row[23]),
                stop_reason=None if row[24] is None else str(row[24]),
                last_observation_sequence=stored_int(
                    row[25], what="observation sequence", minimum=0
                ),
                last_observation_hash=None if row[26] is None else str(row[26]),
                exit_code=row[27],
                terminal_receipt_id=None if row[28] is None else WorkflowRecordId(str(row[28])),
                terminal_receipt_hash=None if row[29] is None else str(row[29]),
                terminal_at_utc=None if row[30] is None else parse_utc(str(row[30])),
            )
        except StateIntegrityError:
            raise
        except Exception as error:
            raise StateIntegrityError("stored local job is invalid") from error


__all__ = ["LocalJobRepository", "P5RecordRepository"]
