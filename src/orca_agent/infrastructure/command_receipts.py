"""Append-only command receipts used for durable idempotency."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.errors import DomainError
from orca_agent.domain.ids import CommandId, EffectId, EventId, RunId
from orca_agent.orchestration.commands import CommandType

from .clock import format_utc, parse_utc

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class CommandBindingKind(StrEnum):
    EVENT = "event"
    EFFECT_AUDIT_ALIAS = "effect_audit_alias"


@dataclass(frozen=True)
class CommandReceipt:
    command_id: CommandId
    command_type: CommandType
    command_hash: str
    run_id: RunId
    binding_kind: CommandBindingKind
    effect_id: EffectId | None
    result_event_id: EventId
    recorded_at_utc: datetime


class CommandReceiptRepository:
    """Read and append receipts in the caller's active transaction."""

    _SELECT = (
        "SELECT command_id, command_type, command_hash, run_id, binding_kind, effect_id, "
        "result_event_id, recorded_at_utc FROM command_receipts"
    )

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _load(self, row: sqlite3.Row) -> CommandReceipt:
        try:
            command_id = CommandId(str(row[0]))
            command_type = CommandType(str(row[1]))
            command_hash = str(row[2])
            run_id = RunId(str(row[3]))
            binding_kind = CommandBindingKind(str(row[4]))
            effect_id = None if row[5] is None else EffectId(str(row[5]))
            result_event_id = EventId(str(row[6]))
            recorded_at = parse_utc(str(row[7]))
        except (DomainError, TypeError, ValueError, ArithmeticError) as error:
            raise StateIntegrityError("stored command receipt is invalid") from error
        if _HASH_PATTERN.fullmatch(command_hash) is None:
            raise StateIntegrityError("stored command receipt hash is invalid")
        if binding_kind is CommandBindingKind.EVENT and effect_id is not None:
            raise StateIntegrityError("event command receipt contains an effect ID")
        if binding_kind is CommandBindingKind.EFFECT_AUDIT_ALIAS and effect_id is None:
            raise StateIntegrityError("effect audit alias is missing its effect ID")
        if binding_kind is CommandBindingKind.EFFECT_AUDIT_ALIAS and command_type not in (
            CommandType.RECORD_EFFECT_SUCCEEDED,
            CommandType.RECORD_EFFECT_FAILED,
        ):
            raise StateIntegrityError("effect audit alias has an invalid command type")
        return CommandReceipt(
            command_id=command_id,
            command_type=command_type,
            command_hash=command_hash,
            run_id=run_id,
            binding_kind=binding_kind,
            effect_id=effect_id,
            result_event_id=result_event_id,
            recorded_at_utc=recorded_at,
        )

    def get(self, command_id: CommandId) -> CommandReceipt | None:
        row = self.connection.execute(
            f"{self._SELECT} WHERE command_id = ?",  # noqa: S608 - fixed internal SQL
            (str(command_id),),
        ).fetchone()
        if row is None:
            return None
        receipt = self._load(row)
        self._verify_binding(receipt)
        return receipt

    def list_for_run(self, run_id: RunId) -> tuple[CommandReceipt, ...]:
        rows = self.connection.execute(
            f"{self._SELECT} WHERE run_id = ? ORDER BY recorded_at_utc, command_id",
            (str(run_id),),
        ).fetchall()
        receipts = tuple(self._load(row) for row in rows)
        for receipt in receipts:
            self._verify_binding(receipt)
        return receipts

    def append_event(self, *, event: object, recorded_at_utc: datetime) -> CommandReceipt:
        receipt = CommandReceipt(
            command_id=event.command_id,
            command_type=event.command_type,
            command_hash=event.command_hash,
            run_id=event.run_id,
            binding_kind=CommandBindingKind.EVENT,
            effect_id=None,
            result_event_id=event.event_id,
            recorded_at_utc=recorded_at_utc,
        )
        self._insert(receipt)
        return receipt

    def append_alias(
        self,
        *,
        command_id: CommandId,
        command_type: CommandType,
        command_hash: str,
        run_id: RunId,
        effect_id: EffectId,
        result_event_id: EventId,
        recorded_at_utc: datetime,
    ) -> CommandReceipt:
        receipt = CommandReceipt(
            command_id=command_id,
            command_type=command_type,
            command_hash=command_hash,
            run_id=run_id,
            binding_kind=CommandBindingKind.EFFECT_AUDIT_ALIAS,
            effect_id=effect_id,
            result_event_id=result_event_id,
            recorded_at_utc=recorded_at_utc,
        )
        self._insert(receipt)
        return receipt

    def _insert(self, receipt: CommandReceipt) -> None:
        if _HASH_PATTERN.fullmatch(receipt.command_hash) is None:
            raise StateIntegrityError("command receipt hash is invalid")
        try:
            self.connection.execute(
                "INSERT INTO command_receipts(command_id, command_type, command_hash, run_id, "
                "binding_kind, effect_id, result_event_id, recorded_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(receipt.command_id),
                    receipt.command_type.value,
                    receipt.command_hash,
                    str(receipt.run_id),
                    receipt.binding_kind.value,
                    None if receipt.effect_id is None else str(receipt.effect_id),
                    str(receipt.result_event_id),
                    format_utc(receipt.recorded_at_utc),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StateIntegrityError("command receipt violates an invariant") from error

    def _verify_binding(self, receipt: CommandReceipt) -> None:
        from .repositories import EventRepository

        event = EventRepository(self.connection).get(receipt.result_event_id)
        if event is None or event.run_id != receipt.run_id:
            raise StateIntegrityError("command receipt result event is invalid")
        if receipt.binding_kind is CommandBindingKind.EVENT:
            if (
                event.command_id != receipt.command_id
                or event.command_type is not receipt.command_type
                or event.command_hash != receipt.command_hash
            ):
                raise StateIntegrityError("event command receipt does not match its event")
            return
        from .outbox import OutboxRepository

        if receipt.effect_id is None:
            raise StateIntegrityError("effect audit alias is missing its effect")
        effect = OutboxRepository(self.connection).get(receipt.effect_id)
        if (
            effect is None
            or effect.run_id != receipt.run_id
            or effect.audit_event_id != receipt.result_event_id
        ):
            raise StateIntegrityError("effect audit alias is not authoritative")
        expected_status = (
            "succeeded"
            if receipt.command_type is CommandType.RECORD_EFFECT_SUCCEEDED
            else "dead_letter"
            if receipt.command_type is CommandType.RECORD_EFFECT_FAILED
            else None
        )
        if expected_status is None or effect.status.value != expected_status:
            raise StateIntegrityError("effect audit alias command type does not match effect")


__all__ = ["CommandBindingKind", "CommandReceipt", "CommandReceiptRepository"]
