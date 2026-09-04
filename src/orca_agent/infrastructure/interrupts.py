"""Durable interrupt projections applied inside the command transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from orca_agent.application.errors import (
    InterruptAlreadyPendingError,
    InterruptNotPendingError,
    StateIntegrityError,
)
from orca_agent.domain.errors import DomainError, HashMismatchError
from orca_agent.domain.hashing import sha256_hex, verify_sha256
from orca_agent.domain.ids import EventId, InterruptId, RunId
from orca_agent.domain.json_types import (
    FrozenJsonObject,
    FrozenJsonValue,
    freeze_json_object,
    freeze_json_value,
)
from orca_agent.domain.versions import CURRENT_SCHEMA_VERSION
from orca_agent.orchestration.events import KernelEvent
from orca_agent.orchestration.transitions import (
    InterruptProjectionOp,
    InterruptProjectionOperation,
    InterruptStatus,
)
from orca_agent.orchestration.versions import ENGINE_VERSION

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
            if int(row[4]) != CURRENT_SCHEMA_VERSION or str(row[5]) != ENGINE_VERSION:
                raise StateIntegrityError("stored interrupt version is unsupported")
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
            record = InterruptRecord(
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
            self._verify_event_links(record)
            return record
        except StateIntegrityError:
            raise
        except (DomainError, HashMismatchError, ValidationError, TypeError, ValueError) as error:
            raise StateIntegrityError("stored interrupt projection is invalid") from error

    def _verify_event_links(self, record: InterruptRecord) -> None:
        from .repositories import EventRepository

        events = EventRepository(self.connection)
        request_event = events.get(record.request_event_id)
        if request_event is None or request_event.run_id != record.run_id:
            raise StateIntegrityError("interrupt request event does not belong to the run")
        request_payload = request_event.payload
        request_id_key: str
        if request_event.event_type.value == "interrupt_requested":
            request_id_key = "interrupt_id"
        elif request_event.event_type.value == "interrupt_replaced":
            request_id_key = "new_interrupt_id"
        else:
            raise StateIntegrityError("interrupt request event has an invalid type")
        if request_payload.get(request_id_key) != str(record.interrupt_id):
            raise StateIntegrityError("interrupt ID does not match its request event")
        if request_payload.get("kind") != record.kind:
            raise StateIntegrityError("interrupt kind does not match its request event")
        if (
            record.schema_version != request_event.schema_version
            or record.engine_version != request_event.engine_version
        ):
            raise StateIntegrityError("interrupt version does not match its request event")
        if record.created_at_utc != request_event.occurred_at_utc:
            raise StateIntegrityError("interrupt creation time does not match its request event")
        raw_payload = request_payload.get("payload")
        if not isinstance(raw_payload, Mapping):
            raise StateIntegrityError("interrupt request payload is invalid")
        if freeze_json_object(raw_payload) != record.payload:
            raise StateIntegrityError("interrupt payload does not match its request event")
        expires_at = _event_timestamp(request_payload.get("expires_at_utc"))
        if expires_at != record.expires_at_utc:
            raise StateIntegrityError("interrupt expiry does not match its request event")

        if record.status is InterruptStatus.PENDING:
            if record.terminal_event_id is not None or record.terminal_at_utc is not None:
                raise StateIntegrityError("pending interrupt contains terminal metadata")
            if record.response is not None or record.response_hash is not None:
                raise StateIntegrityError("pending interrupt contains a response")
            if record.superseded_by is not None:
                raise StateIntegrityError("pending interrupt contains a replacement")
            return

        if record.terminal_event_id is None or record.terminal_at_utc is None:
            raise StateIntegrityError("terminal interrupt is missing terminal metadata")
        terminal_event = events.get(record.terminal_event_id)
        if terminal_event is None or terminal_event.run_id != record.run_id:
            raise StateIntegrityError("interrupt terminal event does not belong to the run")
        if terminal_event.occurred_at_utc != record.terminal_at_utc:
            raise StateIntegrityError("interrupt terminal time does not match its event")

        terminal_payload = terminal_event.payload
        if record.status is InterruptStatus.SUPERSEDED:
            if terminal_event.event_type.value != "interrupt_replaced":
                raise StateIntegrityError("superseded interrupt has an invalid terminal event")
            if terminal_payload.get("old_interrupt_id") != str(record.interrupt_id):
                raise StateIntegrityError("superseded interrupt ID does not match its event")
            if record.superseded_by is None:
                raise StateIntegrityError("superseded interrupt is missing replacement ID")
            if terminal_payload.get("new_interrupt_id") != str(record.superseded_by):
                raise StateIntegrityError("replacement ID does not match its event")
            if record.superseded_by == record.interrupt_id:
                raise StateIntegrityError("interrupt cannot supersede itself")
            replacement_row = self.connection.execute(
                "SELECT run_id FROM interrupts WHERE interrupt_id = ?",
                (str(record.superseded_by),),
            ).fetchone()
            if replacement_row is None or RunId(str(replacement_row[0])) != record.run_id:
                raise StateIntegrityError("interrupt replacement does not belong to the run")
            if record.response is not None or record.response_hash is not None:
                raise StateIntegrityError("superseded interrupt contains a response")
            return

        if record.superseded_by is not None:
            raise StateIntegrityError("non-superseded interrupt contains a replacement")
        if record.status is InterruptStatus.RESOLVED:
            if terminal_event.event_type.value != "interrupt_resolved":
                raise StateIntegrityError("resolved interrupt has an invalid terminal event")
            if terminal_payload.get("interrupt_id") != str(record.interrupt_id):
                raise StateIntegrityError("resolved interrupt ID does not match its event")
            response = terminal_payload.get("response")
            if not isinstance(response, Mapping) or freeze_json_object(response) != record.response:
                raise StateIntegrityError("interrupt response does not match its event")
        elif record.status is InterruptStatus.EXPIRED:
            if terminal_event.event_type.value != "interrupt_expired":
                raise StateIntegrityError("expired interrupt has an invalid terminal event")
            if terminal_payload.get("interrupt_id") != str(record.interrupt_id):
                raise StateIntegrityError("expired interrupt ID does not match its event")
            if _event_timestamp(terminal_payload.get("expires_at_utc")) != record.expires_at_utc:
                raise StateIntegrityError("expired interrupt deadline does not match its event")
            if record.response is not None or record.response_hash is not None:
                raise StateIntegrityError("expired interrupt contains a response")
        elif record.status is InterruptStatus.CANCELLED:
            if terminal_event.event_type.value not in {"run_cancelled", "effect_dead_lettered"}:
                raise StateIntegrityError("cancelled interrupt has an invalid terminal event")
            if record.response is not None or record.response_hash is not None:
                raise StateIntegrityError("cancelled interrupt contains a response")
        else:
            raise StateIntegrityError("interrupt has an unsupported status")

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
        if row is None:
            return None
        try:
            interrupt_id = InterruptId(str(row[0]))
        except DomainError as error:
            raise StateIntegrityError("stored pending interrupt ID is invalid") from error
        return self.get(interrupt_id)

    def list_for_run(self, run_id: RunId) -> tuple[InterruptRecord, ...]:
        rows = self.connection.execute(
            "SELECT interrupt_id FROM interrupts WHERE run_id = ? "
            "ORDER BY created_at_utc, interrupt_id",
            (str(run_id),),
        ).fetchall()
        records: list[InterruptRecord] = []
        for row in rows:
            try:
                interrupt_id = InterruptId(str(row[0]))
            except DomainError as error:
                raise StateIntegrityError("stored interrupt ID is invalid") from error
            record = self.get(interrupt_id)
            if record is None:
                raise StateIntegrityError("interrupt projection disappeared")
            records.append(record)
        return tuple(records)

    def due_ids(self, *, now: datetime, limit: int) -> tuple[InterruptId, ...]:
        if limit < 1:
            return ()
        rows = self.connection.execute(
            "SELECT interrupt_id FROM interrupts "
            "WHERE status = 'pending' AND expires_at_utc <= ? "
            "ORDER BY expires_at_utc, created_at_utc, interrupt_id LIMIT ?",
            (format_utc(now), limit),
        ).fetchall()
        try:
            return tuple(InterruptId(str(row[0])) for row in rows)
        except DomainError as error:
            raise StateIntegrityError("stored due interrupt ID is invalid") from error

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


def _event_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise StateIntegrityError("interrupt event timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StateIntegrityError("interrupt event timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise StateIntegrityError("interrupt event timestamp is not UTC")
    return parsed


__all__ = ["InterruptRecord", "InterruptRepository"]
