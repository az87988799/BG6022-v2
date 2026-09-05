"""Transactional outbox registration, fenced delivery, and terminal receipts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import ValidationError

from orca_agent.application.errors import (
    EffectAuditConflictError,
    EffectAuditNotReadyError,
    EffectCompletionConflictError,
    EffectDispatchBlockedError,
    EffectInFlightError,
    LeaseLostError,
    StateIntegrityError,
    StorageBusyError,
)
from orca_agent.domain.errors import DomainError, HashMismatchError
from orca_agent.domain.hashing import effect_spec_hash, sha256_hex, verify_sha256
from orca_agent.domain.ids import (
    EffectId,
    EventId,
    RunId,
    WorkerId,
    completion_command_id,
    effect_id_for,
)
from orca_agent.domain.json_types import (
    FrozenJsonObject,
    freeze_json_object,
    thaw_json,
)
from orca_agent.domain.versions import CURRENT_SCHEMA_VERSION
from orca_agent.orchestration.codes import HandlerErrorCode, handler_error_message
from orca_agent.orchestration.dispatch_policy import (
    DEFAULT_EFFECT_REGISTRY,
    DispatchDecision,
    EffectRegistry,
    evaluate_dispatch,
)
from orca_agent.orchestration.effect_receipts import (
    EffectSuccessReceiptV1,
    parse_effect_success_receipt,
    receipt_json,
)
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.events import EventType, KernelEvent
from orca_agent.orchestration.schema1_read import read_error_text
from orca_agent.orchestration.versions import ENGINE_VERSION

from .clock import format_utc, parse_utc
from .repositories import json_text, json_value, stored_int
from .sqlite import begin_immediate


class OutboxStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


SYSTEM_WORKER_ID = WorkerId("worker_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")


@dataclass(frozen=True)
class OutboxRecord:
    effect_id: EffectId
    run_id: RunId
    source_event_id: EventId
    effect_index: int
    effect_type: str
    effect_class: EffectClass
    schema_version: int
    engine_version: str
    payload: FrozenJsonObject
    payload_hash: str
    spec_hash: str
    status: OutboxStatus
    attempt_count: int
    available_at_utc: datetime
    lease_owner: WorkerId | None
    lease_expires_at_utc: datetime | None
    dispatch_authorized_at_utc: datetime | None
    dispatch_run_revision: int | None
    dispatch_policy_version: int | None
    completed_at_utc: datetime | None
    completed_by_worker_id: WorkerId | None
    terminal_generation: int | None
    audit_event_id: EventId | None
    result_summary: EffectSuccessReceiptV1 | FrozenJsonObject | None
    result_summary_hash: str | None
    completion_protocol: int
    last_error_code: str | None
    last_error_message: str | None
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(frozen=True)
class DispatchPermit:
    effect: OutboxRecord
    worker_id: WorkerId
    generation: int
    run_revision: int
    policy_version: int


class OutboxRepository:
    """Store deterministic effects in the caller's active transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        try:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(outbox)").fetchall()
            }
        except sqlite3.OperationalError as error:
            if "locked" in str(error).casefold() or "busy" in str(error).casefold():
                raise StorageBusyError("database is busy") from error
            raise
        self._v4 = "dispatch_authorized_at_utc" in columns

    _SELECT = (
        "SELECT effect_id, run_id, source_event_id, effect_index, effect_type, effect_class, "
        "schema_version, engine_version, payload_json, payload_hash, spec_hash, status, "
        "attempt_count, available_at_utc, lease_owner, lease_expires_at_utc, "
        "dispatch_authorized_at_utc, dispatch_run_revision, dispatch_policy_version, "
        "completed_at_utc, completed_by_worker_id, terminal_generation, audit_event_id, "
        "result_summary_json, result_summary_hash, completion_protocol, last_error_code, "
        "last_error_message, created_at_utc, updated_at_utc FROM outbox"
    )

    _SELECT_V3 = (
        "SELECT effect_id, run_id, source_event_id, effect_index, effect_type, effect_class, "
        "schema_version, engine_version, payload_json, payload_hash, spec_hash, status, "
        "attempt_count, available_at_utc, lease_owner, lease_expires_at_utc, completed_at_utc, "
        "completed_by_worker_id, terminal_generation, audit_event_id, result_summary_json, "
        "result_summary_hash, last_error_code, last_error_message, created_at_utc, "
        "updated_at_utc FROM outbox"
    )

    def _select_sql(self) -> str:
        return self._SELECT if self._v4 else self._SELECT_V3

    def _load(self, row: sqlite3.Row) -> OutboxRecord:
        if not self._v4:
            return self._load_v3(row)
        try:
            effect_index = stored_int(row[3], what="outbox effect_index", minimum=0)
            schema_version = stored_int(row[6], what="outbox schema_version", minimum=1)
            attempt_count = stored_int(row[12], what="outbox attempt_count", minimum=0)
            dispatch_run_revision = (
                None
                if row[17] is None
                else stored_int(row[17], what="outbox dispatch_run_revision", minimum=1)
            )
            dispatch_policy_version = (
                None
                if row[18] is None
                else stored_int(row[18], what="outbox dispatch_policy_version", minimum=1)
            )
            completion_protocol = stored_int(row[25], what="outbox completion_protocol", minimum=3)
            if completion_protocol not in (3, 4):
                raise StateIntegrityError("outbox completion protocol is unsupported")
            terminal_generation = (
                None
                if row[21] is None
                else stored_int(row[21], what="outbox terminal_generation", minimum=0)
            )
            effect_id = EffectId(str(row[0]))
            source_event_id = EventId(str(row[2]))
            if effect_id != effect_id_for(source_event_id, effect_index):
                raise StateIntegrityError("stored effect ID is not deterministic")
            payload = freeze_json_object(json_value(str(row[8]), what="outbox payload"))
            payload_hash = str(row[9])
            verify_sha256(payload, payload_hash)
            spec_hash = str(row[10])
            status = OutboxStatus(str(row[11]))
            if schema_version != CURRENT_SCHEMA_VERSION or str(row[7]) != ENGINE_VERSION:
                raise StateIntegrityError("stored outbox version is unsupported")
            run_id = RunId(str(row[1]))
            effect_class = EffectClass(str(row[5]))
            lease_owner = None if row[14] is None else WorkerId(str(row[14]))
            lease_expires = None if row[15] is None else parse_utc(str(row[15]))
            dispatch_authorized_at = None if row[16] is None else parse_utc(str(row[16]))
            completed = None if row[19] is None else parse_utc(str(row[19]))
            completed_by = None if row[20] is None else WorkerId(str(row[20]))
            audit_event_id = None if row[22] is None else EventId(str(row[22]))
            result_summary: FrozenJsonObject | None = None
            result_summary_hash = None if row[24] is None else str(row[24])
            if row[23] is not None:
                raw_summary = json_value(str(row[23]), what="outbox result summary")
                result_summary = freeze_json_object(raw_summary)
                if result_summary_hash is None:
                    raise StateIntegrityError("outbox result summary hash is missing")
                if json_text(result_summary) != str(row[23]):
                    raise StateIntegrityError("outbox result summary is not canonical JSON")
                verify_sha256(result_summary, result_summary_hash)
            elif result_summary_hash is not None:
                raise StateIntegrityError("outbox result summary is missing")

            if status is OutboxStatus.LEASED:
                if (
                    lease_owner is None
                    or lease_expires is None
                    or attempt_count < 1
                    or dispatch_authorized_at is not None
                    or dispatch_run_revision is not None
                    or dispatch_policy_version is not None
                ):
                    raise StateIntegrityError("leased effect is missing lease metadata")
            elif status is OutboxStatus.DISPATCHING:
                if (
                    lease_owner is None
                    or lease_expires is None
                    or attempt_count < 1
                    or dispatch_authorized_at is None
                    or dispatch_run_revision is None
                    or dispatch_policy_version is None
                ):
                    raise StateIntegrityError("dispatching effect is missing permit metadata")
            elif lease_owner is not None or lease_expires is not None:
                raise StateIntegrityError("non-leased effect contains lease metadata")
            if status is not OutboxStatus.DISPATCHING and (
                dispatch_authorized_at is not None
                or dispatch_run_revision is not None
                or dispatch_policy_version is not None
            ):
                raise StateIntegrityError("non-dispatching effect contains permit metadata")
            terminal_statuses = (
                OutboxStatus.SUCCEEDED,
                OutboxStatus.DEAD_LETTER,
                OutboxStatus.CANCELLED,
            )
            if status in terminal_statuses and (
                completed is None or completed_by is None or terminal_generation is None
            ):
                raise StateIntegrityError("terminal effect is missing completion metadata")
            if status in terminal_statuses and terminal_generation != attempt_count:
                raise StateIntegrityError("terminal effect generation does not match attempt count")
            if status in (OutboxStatus.SUCCEEDED, OutboxStatus.DEAD_LETTER) and attempt_count < 1:
                raise StateIntegrityError("terminal effect has no leased generation")
            if status in (OutboxStatus.PENDING, OutboxStatus.LEASED, OutboxStatus.DISPATCHING) and (
                completed is not None or completed_by is not None or terminal_generation is not None
            ):
                raise StateIntegrityError("active effect contains completion metadata")
            if status is OutboxStatus.SUCCEEDED and result_summary is None:
                raise StateIntegrityError("successful effect is missing result summary")
            if status is not OutboxStatus.SUCCEEDED and result_summary is not None:
                raise StateIntegrityError("non-success effect contains a result summary")
            if status is OutboxStatus.CANCELLED and audit_event_id is not None:
                raise StateIntegrityError("cancelled effect contains an audit event")
            if status in (OutboxStatus.PENDING, OutboxStatus.LEASED, OutboxStatus.DISPATCHING):
                if completion_protocol != 4 or audit_event_id is not None:
                    raise StateIntegrityError("active effect has an invalid completion protocol")
            elif (
                completion_protocol == 4
                and status
                in (
                    OutboxStatus.SUCCEEDED,
                    OutboxStatus.DEAD_LETTER,
                )
                and audit_event_id is None
            ):
                raise StateIntegrityError("protocol-4 terminal effect is missing audit event")
            elif completion_protocol == 3 and audit_event_id is not None:
                raise StateIntegrityError("legacy terminal effect cannot have an audit event")

            expected_spec_hash = effect_spec_hash(
                effect_id=str(effect_id),
                run_id=str(run_id),
                source_event_id=str(source_event_id),
                effect_index=effect_index,
                effect_type=str(row[4]),
                effect_class=effect_class.value,
                schema_version=schema_version,
                engine_version=str(row[7]),
                payload=payload,
                payload_hash=payload_hash,
            )
            if spec_hash != expected_spec_hash:
                raise StateIntegrityError("stored effect specification hash does not match")
            self._verify_source_event(
                run_id=run_id,
                source_event_id=source_event_id,
                effect_id=effect_id,
                effect_index=effect_index,
                effect_type=str(row[4]),
                effect_class=effect_class,
                schema_version=schema_version,
                engine_version=str(row[7]),
                payload=payload,
                payload_hash=payload_hash,
            )
            last_error_code = None if row[26] is None else read_error_text(row[26], "error_code")
            last_error_message = (
                None if row[27] is None else read_error_text(row[27], "error_message")
            )
            if status is OutboxStatus.DEAD_LETTER and (
                last_error_code is None or last_error_message is None
            ):
                raise StateIntegrityError("dead-letter effect is missing persisted error")
            if (last_error_code is None) != (last_error_message is None):
                raise StateIntegrityError("outbox error fields are incomplete")
            if status is OutboxStatus.CANCELLED:
                if (
                    last_error_code != "run_cancelled"
                    or last_error_message != "The run is terminal; the effect was not dispatched."
                ):
                    raise StateIntegrityError("cancelled effect has an invalid cancellation reason")
            record = OutboxRecord(
                effect_id=effect_id,
                run_id=run_id,
                source_event_id=source_event_id,
                effect_index=effect_index,
                effect_type=str(row[4]),
                effect_class=effect_class,
                schema_version=schema_version,
                engine_version=str(row[7]),
                payload=payload,
                payload_hash=payload_hash,
                spec_hash=spec_hash,
                status=status,
                attempt_count=attempt_count,
                available_at_utc=parse_utc(str(row[13])),
                lease_owner=lease_owner,
                lease_expires_at_utc=lease_expires,
                dispatch_authorized_at_utc=dispatch_authorized_at,
                dispatch_run_revision=dispatch_run_revision,
                dispatch_policy_version=dispatch_policy_version,
                completed_at_utc=completed,
                completed_by_worker_id=completed_by,
                terminal_generation=terminal_generation,
                audit_event_id=audit_event_id,
                result_summary=result_summary,
                result_summary_hash=result_summary_hash,
                completion_protocol=completion_protocol,
                last_error_code=last_error_code,
                last_error_message=last_error_message,
                created_at_utc=parse_utc(str(row[28])),
                updated_at_utc=parse_utc(str(row[29])),
            )
            self._verify_audit_event(record)
            return record
        except StateIntegrityError:
            raise
        except (
            DomainError,
            HashMismatchError,
            ValidationError,
            TypeError,
            ValueError,
            ArithmeticError,
        ) as error:
            raise StateIntegrityError("stored outbox record is invalid") from error

    def _load_v3(self, row: sqlite3.Row) -> OutboxRecord:
        """Read a v3 row without applying v4-only receipt semantics."""

        try:
            effect_index = stored_int(row[3], what="outbox effect_index", minimum=0)
            schema_version = stored_int(row[6], what="outbox schema_version", minimum=1)
            attempt_count = stored_int(row[12], what="outbox attempt_count", minimum=0)
            effect_id = EffectId(str(row[0]))
            run_id = RunId(str(row[1]))
            source_event_id = EventId(str(row[2]))
            if effect_id != effect_id_for(source_event_id, effect_index):
                raise StateIntegrityError("stored effect ID is not deterministic")
            payload = freeze_json_object(json_value(str(row[8]), what="outbox payload"))
            payload_hash = str(row[9])
            verify_sha256(payload, payload_hash)
            effect_class = EffectClass(str(row[5]))
            status = OutboxStatus(str(row[11]))
            if schema_version != CURRENT_SCHEMA_VERSION or str(row[7]) != ENGINE_VERSION:
                raise StateIntegrityError("stored outbox version is unsupported")
            raw_summary = None
            if row[20] is not None:
                raw_summary = freeze_json_object(
                    json_value(str(row[20]), what="outbox result summary")
                )
                if row[21] is None:
                    raise StateIntegrityError("stored outbox result summary hash is missing")
                verify_sha256(raw_summary, str(row[21]))
            elif row[21] is not None:
                raise StateIntegrityError("stored outbox result summary is missing")
            completed_by = None if row[17] is None else WorkerId(str(row[17]))
            terminal_generation = (
                None
                if row[18] is None
                else stored_int(row[18], what="outbox terminal_generation", minimum=0)
            )
            audit_event_id = None if row[19] is None else EventId(str(row[19]))
            record = OutboxRecord(
                effect_id=effect_id,
                run_id=run_id,
                source_event_id=source_event_id,
                effect_index=effect_index,
                effect_type=str(row[4]),
                effect_class=effect_class,
                schema_version=schema_version,
                engine_version=str(row[7]),
                payload=payload,
                payload_hash=payload_hash,
                spec_hash=str(row[10]),
                status=status,
                attempt_count=attempt_count,
                available_at_utc=parse_utc(str(row[13])),
                lease_owner=None if row[14] is None else WorkerId(str(row[14])),
                lease_expires_at_utc=(None if row[15] is None else parse_utc(str(row[15]))),
                dispatch_authorized_at_utc=None,
                dispatch_run_revision=None,
                dispatch_policy_version=None,
                completed_at_utc=None if row[16] is None else parse_utc(str(row[16])),
                completed_by_worker_id=completed_by,
                terminal_generation=terminal_generation,
                audit_event_id=audit_event_id,
                result_summary=raw_summary,
                result_summary_hash=None if row[21] is None else str(row[21]),
                completion_protocol=3,
                last_error_code=None if row[22] is None else str(row[22]),
                last_error_message=None if row[23] is None else str(row[23]),
                created_at_utc=parse_utc(str(row[24])),
                updated_at_utc=parse_utc(str(row[25])),
            )
            expected_spec_hash = effect_spec_hash(
                effect_id=str(effect_id),
                run_id=str(run_id),
                source_event_id=str(source_event_id),
                effect_index=effect_index,
                effect_type=record.effect_type,
                effect_class=effect_class.value,
                schema_version=schema_version,
                engine_version=record.engine_version,
                payload=payload,
                payload_hash=payload_hash,
            )
            if record.spec_hash != expected_spec_hash:
                raise StateIntegrityError("stored effect specification hash does not match")
            self._verify_source_event(
                run_id=run_id,
                source_event_id=source_event_id,
                effect_id=effect_id,
                effect_index=effect_index,
                effect_type=record.effect_type,
                effect_class=effect_class,
                schema_version=schema_version,
                engine_version=record.engine_version,
                payload=payload,
                payload_hash=payload_hash,
            )
            self._verify_audit_event(record)
            return record
        except StateIntegrityError:
            raise
        except (
            DomainError,
            HashMismatchError,
            TypeError,
            ValueError,
            ArithmeticError,
        ) as error:
            raise StateIntegrityError("stored legacy outbox record is invalid") from error

    def _verify_source_event(
        self,
        *,
        run_id: RunId,
        source_event_id: EventId,
        effect_id: EffectId,
        effect_index: int,
        effect_type: str,
        effect_class: EffectClass,
        schema_version: int,
        engine_version: str,
        payload: FrozenJsonObject,
        payload_hash: str,
    ) -> None:
        from .repositories import EventRepository

        event = EventRepository(self.connection).get(source_event_id)
        if event is None or event.run_id != run_id:
            raise StateIntegrityError("outbox source event does not belong to the run")
        raw_effects = event.payload.get("effects", [])
        if not isinstance(raw_effects, (list, tuple)):
            raise StateIntegrityError("outbox source event effects are invalid")
        try:
            effects = tuple(
                EffectSpec.model_validate_json(
                    json.dumps(thaw_json(item), ensure_ascii=False), strict=True
                )
                for item in raw_effects
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise StateIntegrityError("outbox source event effect is invalid") from error
        indexes = tuple(effect.effect_index for effect in effects)
        if indexes != tuple(range(len(indexes))):
            raise StateIntegrityError("outbox source event effect indexes are invalid")
        if sum(effect.effect_class is EffectClass.EXTERNAL for effect in effects) > 1:
            raise StateIntegrityError("outbox source event contains too many external effects")
        matches = tuple(effect for effect in effects if effect.effect_index == effect_index)
        if len(matches) != 1:
            raise StateIntegrityError("outbox effect is not present in its source event")
        expected = matches[0]
        if (
            effect_id != expected.effect_id(source_event_id)
            or effect_type != expected.effect_type
            or effect_class != expected.effect_class
            or payload != expected.payload
            or payload_hash != sha256_hex(expected.payload)
            or schema_version != event.schema_version
            or engine_version != event.engine_version
        ):
            raise StateIntegrityError("outbox effect does not match its source event")

    def _verify_audit_event(self, record: OutboxRecord) -> None:
        if record.audit_event_id is None:
            return
        from .repositories import EventRepository

        event = EventRepository(self.connection).get(record.audit_event_id)
        if event is None or event.run_id != record.run_id:
            raise StateIntegrityError("outbox audit event does not belong to the run")
        payload = event.payload
        if payload.get("effect_id") != str(record.effect_id):
            raise StateIntegrityError("outbox audit event effect ID does not match")
        if record.status is OutboxStatus.SUCCEEDED:
            if event.event_type is not EventType.EFFECT_SUCCEEDED:
                raise StateIntegrityError("success receipt has an invalid event type")
            raw_summary = payload.get("result_summary")
            try:
                summary = freeze_json_object(raw_summary)
            except ValueError as error:
                raise StateIntegrityError("success receipt summary is invalid") from error
            # Published v4 rewrote exactly {} to this empty receipt. No other
            # mismatch is a compatibility case; both stored hashes are checked.
            from .migrations import _V4_LEGACY_EMPTY_RECEIPT_JSON

            converted_empty = (
                self._v4
                and summary == freeze_json_object({})
                and record.result_summary
                == freeze_json_object(json.loads(_V4_LEGACY_EMPTY_RECEIPT_JSON))
            )
            if record.result_summary != summary and not converted_empty:
                raise StateIntegrityError("success receipt summary does not match outbox")
        elif record.status is OutboxStatus.DEAD_LETTER:
            if event.event_type is not EventType.EFFECT_DEAD_LETTERED:
                raise StateIntegrityError("failure receipt has an invalid event type")
            if (
                payload.get("error_code") != record.last_error_code
                or payload.get("error_message") != record.last_error_message
            ):
                raise StateIntegrityError("failure receipt error does not match outbox")
        else:
            raise StateIntegrityError("active or cancelled effect has an audit event")

    def get(self, effect_id: EffectId) -> OutboxRecord | None:
        row = self.connection.execute(
            f"{self._select_sql()} WHERE effect_id = ?",  # noqa: S608 - fixed internal SQL
            (str(effect_id),),
        ).fetchone()
        return None if row is None else self._load(row)

    def register_effects(
        self,
        *,
        event: KernelEvent,
        run_id: RunId,
        effects: tuple[EffectSpec, ...],
        available_at_utc: datetime,
        created_at_utc: datetime | None = None,
    ) -> tuple[EffectId, ...]:
        created_at = created_at_utc or available_at_utc
        if event.run_id != run_id:
            raise StateIntegrityError("outbox run_id does not match source event")
        indexes = tuple(effect.effect_index for effect in effects)
        if indexes != tuple(range(len(effects))):
            raise StateIntegrityError("effect indexes must be contiguous and ordered")
        effect_ids: list[EffectId] = []
        for effect in effects:
            effect_id = effect.effect_id(event.event_id)
            payload_hash = sha256_hex(effect.payload)
            payload_json = json_text(effect.payload)
            spec_hash = effect_spec_hash(
                effect_id=str(effect_id),
                run_id=str(run_id),
                source_event_id=str(event.event_id),
                effect_index=effect.effect_index,
                effect_type=effect.effect_type,
                effect_class=effect.effect_class.value,
                schema_version=event.schema_version,
                engine_version=event.engine_version,
                payload=effect.payload,
                payload_hash=payload_hash,
            )
            existing = self.connection.execute(
                "SELECT run_id, source_event_id, effect_index, effect_type, effect_class, "
                "payload_json, payload_hash, spec_hash FROM outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if existing is not None:
                existing_index = stored_int(existing[2], what="outbox effect_index", minimum=0)
                same = (
                    str(existing[0]) == str(run_id)
                    and str(existing[1]) == str(event.event_id)
                    and existing_index == effect.effect_index
                    and str(existing[3]) == effect.effect_type
                    and str(existing[4]) == effect.effect_class.value
                    and str(existing[5]) == payload_json
                    and str(existing[6]) == payload_hash
                    and str(existing[7]) == spec_hash
                )
                if not same:
                    raise StateIntegrityError("deterministic effect ID maps to different content")
                effect_ids.append(effect_id)
                continue
            try:
                self.connection.execute(
                    "INSERT INTO outbox(effect_id, run_id, source_event_id, effect_index, "
                    "effect_type, effect_class, schema_version, engine_version, payload_json, "
                    "payload_hash, spec_hash, status, attempt_count, available_at_utc, "
                    "lease_owner, lease_expires_at_utc, dispatch_authorized_at_utc, "
                    "dispatch_run_revision, dispatch_policy_version, completed_at_utc, "
                    "completed_by_worker_id, terminal_generation, audit_event_id, "
                    "result_summary_json, result_summary_hash, completion_protocol, "
                    "last_error_code, last_error_message, created_at_utc, updated_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, "
                    "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 4, NULL, NULL, ?, ?)",
                    (
                        str(effect_id),
                        str(run_id),
                        str(event.event_id),
                        effect.effect_index,
                        effect.effect_type,
                        effect.effect_class.value,
                        event.schema_version,
                        event.engine_version,
                        payload_json,
                        payload_hash,
                        spec_hash,
                        OutboxStatus.PENDING.value,
                        0,
                        format_utc(available_at_utc),
                        format_utc(created_at),
                        format_utc(created_at),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StateIntegrityError(
                    "effect registration violated an outbox invariant"
                ) from error
            effect_ids.append(effect_id)
        return tuple(effect_ids)

    def count_for_event(self, event_id: EventId) -> int:
        return stored_int(
            self.connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE source_event_id = ?", (str(event_id),)
            ).fetchone()[0],
            what="outbox event count",
            minimum=0,
        )

    def list_for_run(self, run_id: RunId) -> tuple[OutboxRecord, ...]:
        rows = self.connection.execute(
            f"{self._select_sql()} WHERE run_id = ? ORDER BY created_at_utc, effect_id",
            (str(run_id),),
        ).fetchall()
        return tuple(self._load(row) for row in rows)

    def has_dispatching_effect(
        self,
        run_id: RunId,
        *,
        effect_class: EffectClass | None = None,
    ) -> bool:
        """Return whether a run has an authorized effect past the cancel fence."""

        records = self.list_for_run(run_id)
        return any(
            record.status is OutboxStatus.DISPATCHING
            and (effect_class is None or record.effect_class is effect_class)
            for record in records
        )

    def count(self, *, status: OutboxStatus | None = None) -> int:
        if status is None:
            row = self.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = ?", (status.value,)
            ).fetchone()
        return stored_int(row[0], what="outbox count", minimum=0)

    def verify_completion_namespace(self, record: OutboxRecord) -> None:
        """Reject historical collisions, including the next dispatch generation.

        UUID5 cannot be reversed. Recheck the imminent generation before every
        claim; upgrade also checks every observed generation without overwriting
        any historical receipt.
        """
        for generation in range(1, record.attempt_count + 2):
            for outcome in ("succeeded", "dead_letter"):
                identifier = completion_command_id(record.effect_id, generation, outcome)
                row = self.connection.execute(
                    "SELECT run_id, binding_kind, result_event_id FROM command_receipts "
                    "WHERE command_id = ?",
                    (str(identifier),),
                ).fetchone()
                if row is not None and not (
                    str(row[0]) == str(record.run_id)
                    and str(row[1]) == "event"
                    and str(row[2]) == str(record.audit_event_id)
                    and generation == record.terminal_generation
                    and outcome == record.status.value
                ):
                    raise StateIntegrityError(
                        "historical command occupies an internal completion ID",
                        details={"effect_id": str(record.effect_id), "generation": generation},
                    )

    def claim_due(
        self,
        *,
        worker_id: WorkerId,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
        registry: EffectRegistry = DEFAULT_EFFECT_REGISTRY,
    ) -> tuple[OutboxRecord, ...]:
        """Claim due effects; the verified worker path uses ``claim_due_verified``."""

        if type(limit) is not int or limit < 1:
            return ()
        from .interrupts import InterruptRepository
        from .repositories import EventRepository, RunRepository

        return self.claim_due_verified(
            runs=RunRepository(self.connection),
            events=EventRepository(self.connection),
            interrupts=InterruptRepository(self.connection),
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
            limit=limit,
            registry=registry,
        )

    def claim_due_verified(
        self,
        *,
        runs: object,
        events: object,
        interrupts: object,
        worker_id: WorkerId,
        now: datetime,
        lease_duration: timedelta,
        limit: int = 1,
        registry: EffectRegistry = DEFAULT_EFFECT_REGISTRY,
    ) -> tuple[OutboxRecord, ...]:
        """Claim only after every candidate run and projection has been replay-verified."""

        if type(limit) is not int or limit < 1:
            return ()
        _require_positive_lease_duration(lease_duration)
        owner = WorkerId(str(worker_id))
        now_text = format_utc(now)
        lease_expires_text = format_utc(now + lease_duration)
        try:
            begin_immediate(self.connection)
            snapshots = {
                run_id: runs.get_verified(
                    run_id,
                    events,
                    interrupts=interrupts,
                    outbox=self,
                )
                for run_id in runs.list_ids()
            }
            claimed_ids: list[EffectId] = []
            after = ("", "", "")
            while len(claimed_ids) < limit:
                rows = self.connection.execute(
                    f"{self._select_sql()} WHERE "
                    "((status = 'pending' AND available_at_utc <= ?) "
                    "OR (status IN ('leased', 'dispatching') AND lease_expires_at_utc <= ?)) "
                    "AND (available_at_utc, created_at_utc, effect_id) > (?, ?, ?) "
                    "ORDER BY available_at_utc, created_at_utc, effect_id LIMIT 64",
                    (now_text, now_text, *after),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    after = (str(row[13]), str(row[28]), str(row[0]))
                    record = self._load(row)
                    self.verify_completion_namespace(record)
                    snapshot = snapshots.get(record.run_id)
                    if snapshot is None:
                        raise StateIntegrityError("outbox effect references an unknown run")
                    decision = evaluate_dispatch(snapshot.state, record, registry)
                    if decision is DispatchDecision.CANCEL:
                        if record.status is OutboxStatus.PENDING or (
                            record.status is OutboxStatus.LEASED
                            and record.lease_expires_at_utc is not None
                            and record.lease_expires_at_utc <= now
                        ):
                            self._cancel_effect(record.effect_id, now_text=now_text)
                        continue
                    if decision is DispatchDecision.BLOCK:
                        continue
                    existing_dispatch = self.connection.execute(
                        "SELECT effect_id FROM outbox WHERE run_id = ? AND status = 'dispatching' "
                        "AND effect_id <> ?",
                        (str(record.run_id), str(record.effect_id)),
                    ).fetchone()
                    if existing_dispatch is not None:
                        continue
                    cursor = self.connection.execute(
                        "UPDATE outbox SET status = 'leased', lease_owner = ?, "
                        "lease_expires_at_utc = ?, attempt_count = attempt_count + 1, "
                        "dispatch_authorized_at_utc = NULL, dispatch_run_revision = NULL, "
                        "dispatch_policy_version = NULL, updated_at_utc = ? "
                        "WHERE effect_id = ? AND "
                        "((status = 'pending' AND available_at_utc <= ?) "
                        "OR (status IN ('leased', 'dispatching') AND lease_expires_at_utc <= ?))",
                        (
                            str(owner),
                            lease_expires_text,
                            now_text,
                            str(record.effect_id),
                            now_text,
                            now_text,
                        ),
                    )
                    if cursor.rowcount == 1:
                        claimed_ids.append(record.effect_id)
                        if len(claimed_ids) == limit:
                            break
            records = tuple(self.get(effect_id) for effect_id in claimed_ids)
            if any(record is None for record in records):
                raise StateIntegrityError("claimed effect disappeared")
            self.connection.commit()
            return tuple(record for record in records if record is not None)
        except Exception:
            _rollback(self.connection)
            raise

    def authorize_dispatch(
        self,
        *,
        runs: object,
        events: object,
        interrupts: object,
        effect_id: EffectId,
        worker_id: WorkerId,
        expected_generation: int,
        now: datetime,
        registry: EffectRegistry = DEFAULT_EFFECT_REGISTRY,
    ) -> DispatchPermit | None:
        """Persist the linearized authorization point before handler invocation."""

        _require_generation(expected_generation)
        owner = WorkerId(str(worker_id))
        try:
            begin_immediate(self.connection)
            run_id = self.get_required_run_id(effect_id)
            snapshot = runs.get_verified(
                run_id,
                events,
                interrupts=interrupts,
                outbox=self,
            )
            current = self.get(effect_id)
            if current is None:
                raise StateIntegrityError("claimed effect disappeared")
            decision = evaluate_dispatch(snapshot.state, current, registry)
            if decision is DispatchDecision.CANCEL:
                if (
                    current.status is OutboxStatus.LEASED
                    and current.lease_owner == owner
                    and current.attempt_count == expected_generation
                ):
                    self._cancel_effect(effect_id, now_text=format_utc(now))
                self.connection.commit()
                return None
            if decision is DispatchDecision.BLOCK:
                raise EffectDispatchBlockedError("effect is blocked by dispatch policy")
            if (
                current.status is not OutboxStatus.LEASED
                or current.lease_owner != owner
                or current.attempt_count != expected_generation
                or current.lease_expires_at_utc is None
                or current.lease_expires_at_utc <= now
            ):
                raise LeaseLostError("outbox lease is no longer owned or valid")
            existing_dispatch = self.connection.execute(
                "SELECT effect_id FROM outbox WHERE run_id = ? AND status = 'dispatching' "
                "AND effect_id <> ?",
                (str(current.run_id), str(effect_id)),
            ).fetchone()
            if existing_dispatch is not None and str(existing_dispatch[0]) != str(effect_id):
                raise EffectInFlightError("another effect is already dispatching for the run")
            dispatch_at = format_utc(now)
            cursor = self.connection.execute(
                "UPDATE outbox SET status = 'dispatching', "
                "dispatch_authorized_at_utc = ?, dispatch_run_revision = ?, "
                "dispatch_policy_version = ?, updated_at_utc = ? "
                "WHERE effect_id = ? AND status = 'leased' AND lease_owner = ? "
                "AND attempt_count = ? AND lease_expires_at_utc > ?",
                (
                    dispatch_at,
                    snapshot.revision,
                    _policy_version(registry),
                    dispatch_at,
                    str(effect_id),
                    str(owner),
                    expected_generation,
                    format_utc(now),
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError("outbox lease is no longer owned or valid")
            authorized = self.get(effect_id)
            if authorized is None:
                raise StateIntegrityError("authorized effect disappeared")
            self.connection.commit()
            return DispatchPermit(
                effect=authorized,
                worker_id=owner,
                generation=expected_generation,
                run_revision=snapshot.revision,
                policy_version=_policy_version(registry),
            )
        except Exception:
            _rollback(self.connection)
            raise

    def prepare_dispatch(
        self,
        *,
        runs: object,
        events: object,
        interrupts: object,
        effect_id: EffectId,
        worker_id: WorkerId,
        expected_generation: int,
        now: datetime,
        registry: EffectRegistry = DEFAULT_EFFECT_REGISTRY,
    ) -> OutboxRecord | None:
        """Compatibility wrapper returning the authorized effect record."""

        permit = self.authorize_dispatch(
            runs=runs,
            events=events,
            interrupts=interrupts,
            effect_id=effect_id,
            worker_id=worker_id,
            expected_generation=expected_generation,
            now=now,
            registry=registry,
        )
        return None if permit is None else permit.effect

    def get_required_run_id(self, effect_id: EffectId) -> RunId:
        row = self.connection.execute(
            "SELECT run_id FROM outbox WHERE effect_id = ?", (str(effect_id),)
        ).fetchone()
        if row is None:
            raise StateIntegrityError("claimed effect disappeared")
        try:
            return RunId(str(row[0]))
        except DomainError as error:
            raise StateIntegrityError("stored outbox run ID is invalid") from error

    def _claim_due_rows(
        self,
        *,
        owner: WorkerId,
        now_text: str,
        lease_expires_text: str,
        limit: int,
    ) -> list[EffectId]:
        rows = self.connection.execute(
            f"{self._select_sql()} WHERE "
            "(status = 'pending' AND available_at_utc <= ?) "
            "OR (status = 'leased' AND lease_expires_at_utc <= ?) "
            "ORDER BY available_at_utc, created_at_utc, effect_id LIMIT ?",
            (now_text, now_text, limit),
        ).fetchall()
        claimed_ids: list[EffectId] = []
        for row in rows:
            try:
                effect_id = EffectId(str(row[0]))
            except DomainError as error:
                raise StateIntegrityError("stored outbox effect ID is invalid") from error
            cursor = self.connection.execute(
                "UPDATE outbox SET status = 'leased', lease_owner = ?, "
                "lease_expires_at_utc = ?, attempt_count = attempt_count + 1, "
                "updated_at_utc = ? WHERE effect_id = ? AND "
                "((status = 'pending' AND available_at_utc <= ?) "
                "OR (status = 'leased' AND lease_expires_at_utc <= ?))",
                (str(owner), lease_expires_text, now_text, str(effect_id), now_text, now_text),
            )
            if cursor.rowcount == 1:
                claimed_ids.append(effect_id)
        return claimed_ids

    def _cancel_effect(self, effect_id: EffectId, *, now_text: str) -> None:
        cursor = self.connection.execute(
            "UPDATE outbox SET status = 'cancelled', lease_owner = NULL, "
            "lease_expires_at_utc = NULL, completed_at_utc = ?, "
            "completed_by_worker_id = ?, terminal_generation = attempt_count, "
            "last_error_code = 'run_cancelled', "
            "last_error_message = 'The run is terminal; the effect was not dispatched.', "
            "updated_at_utc = ? WHERE effect_id = ? AND status IN ('pending', 'leased')",
            (now_text, str(SYSTEM_WORKER_ID), now_text, str(effect_id)),
        )
        if cursor.rowcount != 1:
            raise StateIntegrityError("effect could not be cancelled")

    def cancel_pending_for_run(self, *, run_id: RunId, now: datetime) -> int:
        """Cancel all not-yet-dispatched siblings after a run enters a terminal state."""

        now_text = format_utc(now)
        cursor = self.connection.execute(
            "UPDATE outbox SET status = 'cancelled', lease_owner = NULL, "
            "lease_expires_at_utc = NULL, completed_at_utc = ?, "
            "completed_by_worker_id = ?, terminal_generation = attempt_count, "
            "last_error_code = 'run_cancelled', "
            "last_error_message = 'The run is terminal; the effect was not dispatched.', "
            "updated_at_utc = ? WHERE run_id = ? AND status IN ('pending', 'leased')",
            (now_text, str(SYSTEM_WORKER_ID), now_text, str(run_id)),
        )
        return cursor.rowcount

    def renew(
        self,
        *,
        effect_id: EffectId,
        worker_id: WorkerId,
        expected_generation: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> OutboxRecord:
        """Extend only the expected generation of a currently valid lease."""

        _require_generation(expected_generation)
        _require_positive_lease_duration(lease_duration)
        owner = WorkerId(str(worker_id))
        now_text = format_utc(now)
        try:
            begin_immediate(self.connection)
            current = self.get(effect_id)
            if current is None:
                raise StateIntegrityError("effect was not found")
            if (
                current.status is not OutboxStatus.LEASED
                or current.lease_owner != owner
                or current.attempt_count != expected_generation
                or current.lease_expires_at_utc is None
                or current.lease_expires_at_utc <= now
            ):
                raise LeaseLostError("outbox lease is no longer owned or valid")
            next_expiry = now + lease_duration
            if next_expiry <= current.lease_expires_at_utc:
                raise ValueError("lease renewal must extend the current lease")
            cursor = self.connection.execute(
                "UPDATE outbox SET lease_expires_at_utc = ?, updated_at_utc = ? "
                "WHERE effect_id = ? AND status = 'leased' AND lease_owner = ? "
                "AND attempt_count = ? AND lease_expires_at_utc > ?",
                (
                    format_utc(next_expiry),
                    now_text,
                    str(effect_id),
                    str(owner),
                    expected_generation,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError("outbox lease is no longer owned or valid")
            record = self.get(effect_id)
            if record is None:
                raise StateIntegrityError("renewed effect disappeared")
            self.connection.commit()
            return record
        except Exception:
            _rollback(self.connection)
            raise

    def validate_dispatch_permit(
        self,
        *,
        permit: DispatchPermit,
        now: datetime,
    ) -> OutboxRecord:
        """Validate a permit against the current dispatching row in a transaction."""

        current = self.get(permit.effect.effect_id)
        if current is None:
            raise StateIntegrityError("effect was not found")
        if current.status is not OutboxStatus.DISPATCHING:
            raise LeaseLostError("outbox effect is no longer dispatching")
        if (
            current.run_id != permit.effect.run_id
            or current.lease_owner != permit.worker_id
            or current.attempt_count != permit.generation
            or current.dispatch_run_revision != permit.run_revision
            or current.dispatch_policy_version != permit.policy_version
            or current.lease_expires_at_utc is None
            or current.lease_expires_at_utc <= now
        ):
            raise LeaseLostError("dispatch permit is no longer owned or valid")
        if current != permit.effect:
            raise StateIntegrityError("dispatch permit effect specification changed")
        return current

    def complete_terminal_in_transaction(
        self,
        *,
        permit: DispatchPermit,
        status: OutboxStatus,
        now: datetime,
        audit_event_id: EventId,
        result_summary: EffectSuccessReceiptV1 | None = None,
        error_code: HandlerErrorCode | None = None,
    ) -> OutboxRecord:
        """Atomically persist a protocol-4 terminal receipt and audit binding."""

        if status not in (OutboxStatus.SUCCEEDED, OutboxStatus.DEAD_LETTER):
            raise ValueError("terminal completion requires succeeded or dead_letter status")
        try:
            audit_id = EventId(str(audit_event_id))
        except (DomainError, TypeError, ValueError) as error:
            raise StateIntegrityError("terminal audit event ID is invalid") from error
        current = self.validate_dispatch_permit(permit=permit, now=now)
        if status is OutboxStatus.SUCCEEDED:
            if result_summary is None or error_code is not None:
                raise ValueError("success completion requires only a typed receipt")
            receipt = parse_effect_success_receipt(result_summary)
            summary_json = json_text(receipt_json(receipt))
            summary_hash = sha256_hex(receipt)
            persisted_error_code = None
            persisted_error_message = None
        else:
            if result_summary is not None or error_code is None:
                raise ValueError("failure completion requires only an allowlisted error code")
            try:
                failure_code = HandlerErrorCode(error_code)
            except ValueError as error:
                raise ValueError("failure completion error code is not allowlisted") from error
            summary_json = None
            summary_hash = None
            persisted_error_code = failure_code.value
            persisted_error_message = handler_error_message(failure_code)

        try:
            cursor = self.connection.execute(
                "UPDATE outbox SET status = ?, lease_owner = NULL, "
                "lease_expires_at_utc = NULL, dispatch_authorized_at_utc = NULL, "
                "dispatch_run_revision = NULL, dispatch_policy_version = NULL, "
                "completed_at_utc = ?, completed_by_worker_id = ?, terminal_generation = ?, "
                "audit_event_id = ?, result_summary_json = ?, result_summary_hash = ?, "
                "last_error_code = ?, last_error_message = ?, completion_protocol = 4, "
                "updated_at_utc = ? WHERE effect_id = ? AND status = 'dispatching' "
                "AND lease_owner = ? AND attempt_count = ? AND lease_expires_at_utc > ? "
                "AND dispatch_run_revision = ? AND dispatch_policy_version = ?",
                (
                    status.value,
                    format_utc(now),
                    str(permit.worker_id),
                    permit.generation,
                    str(audit_id),
                    summary_json,
                    summary_hash,
                    persisted_error_code,
                    persisted_error_message,
                    format_utc(now),
                    str(current.effect_id),
                    str(permit.worker_id),
                    permit.generation,
                    format_utc(now),
                    permit.run_revision,
                    permit.policy_version,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StateIntegrityError("terminal effect receipt violated an invariant") from error
        if cursor.rowcount != 1:
            raise LeaseLostError("dispatch permit is no longer owned or valid")
        updated = self.get(current.effect_id)
        if updated is None:
            raise StateIntegrityError("completed effect disappeared")
        return updated

    def retry_dispatch_in_transaction(
        self,
        *,
        permit: DispatchPermit,
        now: datetime,
        error_code: HandlerErrorCode,
    ) -> OutboxRecord:
        """Return a dispatching effect to pending without a business Event."""

        try:
            failure_code = HandlerErrorCode(error_code)
        except ValueError as error:
            raise ValueError("retry error code is not allowlisted") from error
        current = self.validate_dispatch_permit(permit=permit, now=now)
        available_at = now + backoff_for_attempt(current.attempt_count)
        cursor = self.connection.execute(
            "UPDATE outbox SET status = 'pending', available_at_utc = ?, "
            "lease_owner = NULL, lease_expires_at_utc = NULL, "
            "dispatch_authorized_at_utc = NULL, dispatch_run_revision = NULL, "
            "dispatch_policy_version = NULL, completed_at_utc = NULL, "
            "completed_by_worker_id = NULL, terminal_generation = NULL, "
            "audit_event_id = NULL, result_summary_json = NULL, result_summary_hash = NULL, "
            "last_error_code = ?, last_error_message = ?, completion_protocol = 4, "
            "updated_at_utc = ? WHERE effect_id = ? AND status = 'dispatching' "
            "AND lease_owner = ? AND attempt_count = ? AND lease_expires_at_utc > ? "
            "AND dispatch_run_revision = ? AND dispatch_policy_version = ?",
            (
                format_utc(available_at),
                failure_code.value,
                handler_error_message(failure_code),
                format_utc(now),
                str(current.effect_id),
                str(permit.worker_id),
                permit.generation,
                format_utc(now),
                permit.run_revision,
                permit.policy_version,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseLostError("dispatch permit is no longer owned or valid")
        updated = self.get(current.effect_id)
        if updated is None:
            raise StateIntegrityError("retried effect disappeared")
        return updated

    def mark_succeeded(
        self,
        *,
        effect_id: EffectId,
        worker_id: WorkerId,
        expected_generation: int,
        now: datetime,
        result_summary: object,
    ) -> bool:
        """Reject the pre-permit completion API."""

        raise EffectCompletionConflictError(
            "direct outbox completion is disabled; use EffectCompletionService"
        )

    def mark_failed(
        self,
        *,
        effect_id: EffectId,
        worker_id: WorkerId,
        expected_generation: int,
        now: datetime,
        error_code: str,
        error_message: str | None = None,
        max_attempts: int = 5,
    ) -> OutboxRecord:
        """Reject the pre-permit completion API."""

        raise EffectCompletionConflictError(
            "direct outbox completion is disabled; use EffectCompletionService"
        )

    def bind_audit_event(self, *, effect_id: EffectId, event: KernelEvent) -> OutboxRecord:
        """Reject post-hoc binding; protocol-4 binding is one atomic UPDATE."""

        raise EffectAuditNotReadyError(
            "post-hoc audit binding is disabled; complete the effect atomically"
        )

    def get_required(self, effect_id: EffectId) -> OutboxRecord:
        record = self.get(effect_id)
        if record is None:
            raise StateIntegrityError("effect was not found")
        return record

    def _verify_audit_candidate(self, record: OutboxRecord, event: KernelEvent) -> None:
        payload = event.payload
        if payload.get("effect_id") != str(record.effect_id):
            raise EffectAuditConflictError("audit event effect ID does not match effect")
        if record.status is OutboxStatus.SUCCEEDED:
            if event.event_type is not EventType.EFFECT_SUCCEEDED:
                raise EffectAuditConflictError("audit event type does not match success")
            raw_summary = payload.get("result_summary")
            try:
                summary = parse_effect_success_receipt(raw_summary)
            except ValueError as error:
                raise EffectAuditConflictError("audit success summary is invalid") from error
            if record.result_summary != summary:
                raise EffectAuditConflictError("audit success summary does not match receipt")
        elif record.status is OutboxStatus.DEAD_LETTER:
            if event.event_type is not EventType.EFFECT_DEAD_LETTERED:
                raise EffectAuditConflictError("audit event type does not match failure")
            if (
                payload.get("error_code") != record.last_error_code
                or payload.get("error_message") != record.last_error_message
            ):
                raise EffectAuditConflictError("audit failure does not match persisted error")
        else:
            raise EffectAuditNotReadyError("effect is not terminal for audit")


def backoff_for_attempt(attempt_count: int, *, initial_seconds: int = 1) -> timedelta:
    """Return a duration for the failed attempt's next delivery."""

    if type(attempt_count) is not int or attempt_count < 1:
        raise ValueError("attempt_count must be positive")
    if type(initial_seconds) is not int or initial_seconds < 1:
        raise ValueError("initial_seconds must be positive")
    return timedelta(seconds=min(60, initial_seconds * (2 ** (attempt_count - 1))))


def _safe_error_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value:
        raise ValueError(f"{field_name} is empty, too long, or contains NUL")
    lowered = value.casefold()
    if any(token in lowered for token in ("traceback", "secret", "api_key", "password", "token=")):
        raise ValueError(f"{field_name} contains unsafe diagnostic text")
    return value.strip()


def _require_positive_lease_duration(value: object) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError("lease_duration must be a positive timedelta")


def _require_generation(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected_generation must be a positive integer")
    return value


def _policy_version(registry: object) -> int:
    value = getattr(registry, "policy_version", 1)
    if type(value) is not int or value < 1:
        raise ValueError("dispatch policy version must be a positive integer")
    return value


def _rollback(connection: sqlite3.Connection) -> None:
    if getattr(connection, "in_transaction", False):
        connection.rollback()


LeasedEffect = OutboxRecord

__all__ = [
    "LeasedEffect",
    "OutboxRecord",
    "OutboxRepository",
    "OutboxStatus",
    "DispatchPermit",
    "SYSTEM_WORKER_ID",
    "backoff_for_attempt",
]
