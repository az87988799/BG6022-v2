"""Transactional outbox registration; delivery operations are added in P2.6."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from orca_agent.application.errors import LeaseLostError, StateIntegrityError
from orca_agent.domain.errors import HashMismatchError
from orca_agent.domain.hashing import sha256_hex, verify_sha256
from orca_agent.domain.ids import EffectId, EventId, RunId, WorkerId, effect_id_for
from orca_agent.domain.json_types import FrozenJsonObject, freeze_json_object
from orca_agent.domain.versions import CURRENT_SCHEMA_VERSION
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.events import KernelEvent
from orca_agent.orchestration.versions import ENGINE_VERSION

from .clock import format_utc, parse_utc
from .repositories import json_text, json_value


class OutboxStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"


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
    status: OutboxStatus
    attempt_count: int
    available_at_utc: datetime
    lease_owner: WorkerId | None
    lease_expires_at_utc: datetime | None
    completed_at_utc: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    created_at_utc: datetime
    updated_at_utc: datetime


class OutboxRepository:
    """Store deterministic effects in the caller's active transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    _SELECT = (
        "SELECT effect_id, run_id, source_event_id, effect_index, effect_type, effect_class, "
        "schema_version, engine_version, payload_json, payload_hash, status, attempt_count, "
        "available_at_utc, lease_owner, lease_expires_at_utc, completed_at_utc, "
        "last_error_code, last_error_message, created_at_utc, updated_at_utc FROM outbox"
    )

    def _load(self, row: sqlite3.Row) -> OutboxRecord:
        try:
            effect_id = EffectId(str(row[0]))
            source_event_id = EventId(str(row[2]))
            if effect_id != effect_id_for(source_event_id, int(row[3])):
                raise StateIntegrityError("stored effect ID is not deterministic")
            payload = freeze_json_object(json_value(str(row[8]), what="outbox payload"))
            payload_hash = str(row[9])
            verify_sha256(payload, payload_hash)
            status = OutboxStatus(str(row[10]))
            if int(row[6]) != CURRENT_SCHEMA_VERSION or str(row[7]) != ENGINE_VERSION:
                raise StateIntegrityError("stored outbox version is unsupported")
            lease_owner = None if row[13] is None else WorkerId(str(row[13]))
            lease_expires = None if row[14] is None else parse_utc(str(row[14]))
            completed = None if row[15] is None else parse_utc(str(row[15]))
            if status is OutboxStatus.LEASED:
                if lease_owner is None or lease_expires is None:
                    raise StateIntegrityError("leased effect is missing lease metadata")
            elif lease_owner is not None or lease_expires is not None:
                raise StateIntegrityError("non-leased effect contains lease metadata")
            if status in (OutboxStatus.SUCCEEDED, OutboxStatus.DEAD_LETTER) and completed is None:
                raise StateIntegrityError("terminal effect is missing completion time")
            if status in (OutboxStatus.PENDING, OutboxStatus.LEASED) and completed is not None:
                raise StateIntegrityError("active effect contains completion time")
            error_message = None if row[17] is None else str(row[17])
            if error_message is not None and (len(error_message) > 256 or "\x00" in error_message):
                raise StateIntegrityError("stored outbox error message is unsafe")
            return OutboxRecord(
                effect_id=effect_id,
                run_id=RunId(str(row[1])),
                source_event_id=source_event_id,
                effect_index=int(row[3]),
                effect_type=str(row[4]),
                effect_class=EffectClass(str(row[5])),
                schema_version=int(row[6]),
                engine_version=str(row[7]),
                payload=payload,
                payload_hash=payload_hash,
                status=status,
                attempt_count=int(row[11]),
                available_at_utc=parse_utc(str(row[12])),
                lease_owner=lease_owner,
                lease_expires_at_utc=lease_expires,
                completed_at_utc=completed,
                last_error_code=None if row[16] is None else str(row[16]),
                last_error_message=error_message,
                created_at_utc=parse_utc(str(row[18])),
                updated_at_utc=parse_utc(str(row[19])),
            )
        except StateIntegrityError:
            raise
        except (HashMismatchError, TypeError, ValueError) as error:
            raise StateIntegrityError("stored outbox record is invalid") from error

    def get(self, effect_id: EffectId) -> OutboxRecord | None:
        row = self.connection.execute(
            f"{self._SELECT} WHERE effect_id = ?",  # noqa: S608 - fixed internal SQL
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
        indexes = tuple(effect.effect_index for effect in effects)
        if indexes != tuple(range(len(indexes))):
            raise StateIntegrityError("effect indexes must be contiguous and ordered")
        effect_ids: list[EffectId] = []
        for effect in effects:
            effect_id = effect.effect_id(event.event_id)
            payload_hash = sha256_hex(effect.payload)
            payload_json = json_text(effect.payload)
            existing = self.connection.execute(
                "SELECT run_id, source_event_id, effect_index, effect_type, effect_class, "
                "payload_json, payload_hash FROM outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if existing is not None:
                same = (
                    str(existing[0]) == str(run_id)
                    and str(existing[1]) == str(event.event_id)
                    and int(existing[2]) == effect.effect_index
                    and str(existing[3]) == effect.effect_type
                    and str(existing[4]) == effect.effect_class.value
                    and str(existing[5]) == payload_json
                    and str(existing[6]) == payload_hash
                )
                if not same:
                    raise StateIntegrityError("deterministic effect ID maps to different content")
                effect_ids.append(effect_id)
                continue
            try:
                self.connection.execute(
                    "INSERT INTO outbox(effect_id, run_id, source_event_id, effect_index, "
                    "effect_type, effect_class, schema_version, engine_version, payload_json, "
                    "payload_hash, status, attempt_count, available_at_utc, lease_owner, "
                    "lease_expires_at_utc, completed_at_utc, last_error_code, last_error_message, "
                    "created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, "
                    "NULL, NULL, NULL, NULL, NULL, ?, ?)",
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
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE source_event_id = ?", (str(event_id),)
            ).fetchone()[0]
        )

    def count(self, *, status: OutboxStatus | None = None) -> int:
        if status is None:
            row = self.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = ?", (status.value,)
            ).fetchone()
        return int(row[0])

    def claim_due(
        self,
        *,
        worker_id: WorkerId,
        now: datetime,
        lease_duration,
        limit: int,
    ) -> tuple[OutboxRecord, ...]:
        """Claim due or expired-lease effects in one writer transaction."""

        if limit < 1:
            return ()
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        owner = WorkerId(str(worker_id))
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        now_text = format_utc(now)
        lease_expires_text = format_utc(now + lease_duration)
        claimed_ids: list[EffectId] = []
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute(
                f"{self._SELECT} WHERE "
                "(status = 'pending' AND available_at_utc <= ?) "
                "OR (status = 'leased' AND lease_expires_at_utc <= ?) "
                "ORDER BY available_at_utc, created_at_utc, effect_id LIMIT ?",
                (now_text, now_text, limit),
            ).fetchall()
            for row in rows:
                effect_id = EffectId(str(row[0]))
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
            records = tuple(
                self._load(
                    self.connection.execute(
                        f"{self._SELECT} WHERE effect_id = ?", (str(effect_id),)
                    ).fetchone()
                )
                for effect_id in claimed_ids
            )
            self.connection.commit()
            return records
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def renew(
        self,
        *,
        effect_id: EffectId,
        worker_id: WorkerId,
        now: datetime,
        lease_duration,
    ) -> OutboxRecord:
        """Extend only a currently valid lease owned by ``worker_id``."""

        owner = WorkerId(str(worker_id))
        now_text = format_utc(now)
        new_expiry = format_utc(now + lease_duration)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute(
                "UPDATE outbox SET lease_expires_at_utc = ?, updated_at_utc = ? "
                "WHERE effect_id = ? AND status = 'leased' AND lease_owner = ? "
                "AND lease_expires_at_utc > ?",
                (new_expiry, now_text, str(effect_id), str(owner), now_text),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError("outbox lease is no longer owned or valid")
            record = self.get(effect_id)
            self.connection.commit()
            if record is None:
                raise StateIntegrityError("renewed effect disappeared")
            return record
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def mark_succeeded(
        self,
        *,
        effect_id: EffectId,
        worker_id: WorkerId,
        now: datetime,
    ) -> bool:
        """Complete a valid lease; repeated completion is an idempotent no-op."""

        owner = WorkerId(str(worker_id))
        now_text = format_utc(now)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.get(effect_id)
            if current is None:
                raise StateIntegrityError("effect was not found")
            if current.status is OutboxStatus.SUCCEEDED:
                self.connection.commit()
                return True
            if (
                current.status is not OutboxStatus.LEASED
                or current.lease_owner != owner
                or current.lease_expires_at_utc is None
                or current.lease_expires_at_utc <= now
            ):
                raise LeaseLostError("outbox lease is no longer owned or valid")
            cursor = self.connection.execute(
                "UPDATE outbox SET status = 'succeeded', lease_owner = NULL, "
                "lease_expires_at_utc = NULL, completed_at_utc = ?, updated_at_utc = ? "
                "WHERE effect_id = ? AND status = 'leased' AND lease_owner = ?",
                (now_text, now_text, str(effect_id), str(owner)),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError("outbox lease is no longer owned or valid")
            self.connection.commit()
            return True
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def mark_failed(
        self,
        *,
        effect_id: EffectId,
        worker_id: WorkerId,
        now: datetime,
        error_code: str,
        error_message: str,
        max_attempts: int = 5,
    ) -> OutboxRecord:
        """Return a lease to pending with deterministic backoff or dead-letter it."""

        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        error_code = _safe_error_text(error_code, "error_code")
        error_message = _safe_error_text(error_message, "error_message")
        owner = WorkerId(str(worker_id))
        now_text = format_utc(now)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.get(effect_id)
            if current is None:
                raise StateIntegrityError("effect was not found")
            if (
                current.status is not OutboxStatus.LEASED
                or current.lease_owner != owner
                or current.lease_expires_at_utc is None
                or current.lease_expires_at_utc <= now
            ):
                raise LeaseLostError("outbox lease is no longer owned or valid")
            terminal = current.attempt_count >= max_attempts
            next_status = OutboxStatus.DEAD_LETTER if terminal else OutboxStatus.PENDING
            available = now if terminal else now + backoff_for_attempt(current.attempt_count)
            completed = now_text if terminal else None
            self.connection.execute(
                "UPDATE outbox SET status = ?, available_at_utc = ?, lease_owner = NULL, "
                "lease_expires_at_utc = NULL, completed_at_utc = ?, last_error_code = ?, "
                "last_error_message = ?, updated_at_utc = ? WHERE effect_id = ? AND "
                "status = 'leased' AND lease_owner = ?",
                (
                    next_status.value,
                    format_utc(available),
                    completed,
                    error_code,
                    error_message,
                    now_text,
                    str(effect_id),
                    str(owner),
                ),
            )
            updated = self.get(effect_id)
            self.connection.commit()
            if updated is None:
                raise StateIntegrityError("failed effect disappeared")
            return updated
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise


def backoff_for_attempt(attempt_count: int, *, initial_seconds: int = 1) -> timedelta:
    """Return a duration for the failed attempt's next delivery."""

    if type(attempt_count) is not int or attempt_count < 1:
        raise ValueError("attempt_count must be positive")
    if type(initial_seconds) is not int or initial_seconds < 1:
        raise ValueError("initial_seconds must be positive")
    return timedelta(seconds=min(60, initial_seconds * (2 ** (attempt_count - 1))))


def _safe_error_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value:
        raise ValueError(f"{field_name} is empty, too long, or contains NUL")
    return value.strip()


LeasedEffect = OutboxRecord

__all__ = [
    "LeasedEffect",
    "OutboxRecord",
    "OutboxRepository",
    "OutboxStatus",
    "backoff_for_attempt",
]
