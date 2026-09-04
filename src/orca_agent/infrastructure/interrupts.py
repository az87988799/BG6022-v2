"""Durable interrupt projections applied inside the command transaction."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from orca_agent.application.errors import (
    InterruptAlreadyPendingError,
    InterruptNotPendingError,
    StateIntegrityError,
)
from orca_agent.domain.errors import HashMismatchError
from orca_agent.domain.hashing import sha256_hex, verify_sha256
from orca_agent.domain.ids import EventId, InterruptId, RunId
from orca_agent.domain.json_types import (
    FrozenJsonObject,
    FrozenJsonValue,
    freeze_json_object,
    freeze_json_value,
)
from orca_agent.orchestration.events import KernelEvent
from orca_agent.orchestration.transitions import (
    InterruptProjectionOp,
    InterruptProjectionOperation,
    InterruptStatus,
)

from .clock import format_utc, parse_utc
from .repositories import json_text, json_value


@dataclass(frozen=True)
class InterruptRecord:
    interrupt_id: InterruptId
    run_id: RunId
    kind: str
    status: InterruptStatus
    schema_version: int
    engine_version: str
    request_event_id: EventId
    terminal_event_id: EventId | None
    payload: FrozenJsonObject
    payload_hash: str
    response: FrozenJsonValue | None
    response_hash: str | None
    created_at_utc: datetime
    expires_at_utc: datetime
    terminal_at_utc: datetime | None
    superseded_by: InterruptId | None


class InterruptRepository:
    """Exact-ID access to interrupt projections; no JSON substring queries."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _load(self, row: sqlite3.Row) -> InterruptRecord:
        try:
            interrupt_id = InterruptId(str(row[0]))
            run_id = RunId(str(row[1]))
            status = InterruptStatus(str(row[3]))
            request_event_id = EventId(str(row[6]))
            terminal_event_id = None if row[7] is None else EventId(str(row[7]))
            payload = freeze_json_object(json_value(str(row[8]), what="interrupt payload"))
            payload_hash = str(row[9])
            verify_sha256(payload, payload_hash)
            response = None
            response_hash = None if row[11] is None else str(row[11])
            if row[10] is not None:
                response = freeze_json_value(json_value(str(row[10]), what="interrupt response"))
                if response_hash is None:
                    raise StateIntegrityError("stored interrupt response hash is missing")
                verify_sha256(response, response_hash)
            elif response_hash is not None:
                raise StateIntegrityError("stored interrupt response is missing")
            superseded_by = None if row[15] is None else InterruptId(str(row[15]))
            terminal_at = None if row[14] is None else parse_utc(str(row[14]))
            return InterruptRecord(
                interrupt_id=interrupt_id,
                run_id=run_id,
                kind=str(row[2]),
                status=status,
                schema_version=int(row[4]),
                engine_version=str(row[5]),
                request_event_id=request_event_id,
                terminal_event_id=terminal_event_id,
                payload=payload,
                payload_hash=payload_hash,
                response=response,
                response_hash=response_hash,
                created_at_utc=parse_utc(str(row[12])),
                expires_at_utc=parse_utc(str(row[13])),
                terminal_at_utc=terminal_at,
                superseded_by=superseded_by,
            )
        except StateIntegrityError:
            raise
        except (HashMismatchError, ValidationError, TypeError, ValueError) as error:
            raise StateIntegrityError("stored interrupt projection is invalid") from error

    def get(self, interrupt_id: InterruptId) -> InterruptRecord | None:
        row = self.connection.execute(
            "SELECT interrupt_id, run_id, kind, status, schema_version, engine_version, "
            "request_event_id, terminal_event_id, payload_json, payload_hash, response_json, "
            "response_hash, created_at_utc, expires_at_utc, terminal_at_utc, superseded_by "
            "FROM interrupts WHERE interrupt_id = ?",
            (str(interrupt_id),),
        ).fetchone()
        if row is None:
            return None
        return self._load(row)

    def get_pending_for_run(self, run_id: RunId) -> InterruptRecord | None:
        row = self.connection.execute(
            "SELECT interrupt_id FROM interrupts WHERE run_id = ? AND status = 'pending'",
            (str(run_id),),
        ).fetchone()
        return None if row is None else self.get(InterruptId(str(row[0])))

    def due_ids(self, *, now: datetime, limit: int) -> tuple[InterruptId, ...]:
        if limit < 1:
            return ()
        rows = self.connection.execute(
            "SELECT interrupt_id FROM interrupts "
            "WHERE status = 'pending' AND expires_at_utc <= ? "
            "ORDER BY expires_at_utc, created_at_utc, interrupt_id LIMIT ?",
            (format_utc(now), limit),
        ).fetchall()
        return tuple(InterruptId(str(row[0])) for row in rows)

    def apply_operations(
        self,
        *,
        event: KernelEvent,
        operations: tuple[InterruptProjectionOp, ...],
    ) -> None:
        for operation in operations:
            if operation.run_id != event.run_id:
                raise StateIntegrityError("interrupt projection run_id does not match event")
            if operation.operation is InterruptProjectionOperation.INSERT_PENDING:
                self._insert_pending(event=event, operation=operation)
            else:
                self._finalize(event=event, operation=operation)

    def _insert_pending(self, *, event: KernelEvent, operation: InterruptProjectionOp) -> None:
        if operation.kind is None or operation.payload is None or operation.expires_at_utc is None:
            raise StateIntegrityError("pending interrupt projection is incomplete")
        payload_hash = sha256_hex(operation.payload)
        try:
            self.connection.execute(
                "INSERT INTO interrupts(interrupt_id, run_id, kind, status, schema_version, "
                "engine_version, request_event_id, terminal_event_id, payload_json, payload_hash, "
                "response_json, response_hash, created_at_utc, expires_at_utc, terminal_at_utc, "
                "superseded_by) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL, ?, ?, "
                "NULL, NULL)",
                (
                    str(operation.interrupt_id),
                    str(operation.run_id),
                    operation.kind,
                    InterruptStatus.PENDING.value,
                    event.schema_version,
                    event.engine_version,
                    str(event.event_id),
                    json_text(operation.payload),
                    payload_hash,
                    format_utc(event.occurred_at_utc),
                    format_utc(operation.expires_at_utc),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise InterruptAlreadyPendingError(
                "run already has a pending interrupt",
                details={"run_id": str(operation.run_id)},
            ) from error

    def _finalize(self, *, event: KernelEvent, operation: InterruptProjectionOp) -> None:
        response_json = None
        response_hash = None
        if operation.response is not None:
            response_json = json_text(operation.response)
            response_hash = sha256_hex(operation.response)
        cursor = self.connection.execute(
            "UPDATE interrupts SET status = ?, terminal_event_id = ?, response_json = ?, "
            "response_hash = ?, terminal_at_utc = ?, superseded_by = ? "
            "WHERE interrupt_id = ? AND run_id = ? AND status = 'pending'",
            (
                operation.status.value,
                str(event.event_id),
                response_json,
                response_hash,
                format_utc(event.occurred_at_utc),
                None if operation.superseded_by is None else str(operation.superseded_by),
                str(operation.interrupt_id),
                str(operation.run_id),
            ),
        )
        if cursor.rowcount != 1:
            raise InterruptNotPendingError(
                "interrupt is not pending",
                details={"interrupt_id": str(operation.interrupt_id)},
            )

    def count_for_run(self, run_id: RunId) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM interrupts WHERE run_id = ?", (str(run_id),)
            ).fetchone()[0]
        )


__all__ = ["InterruptRecord", "InterruptRepository"]
