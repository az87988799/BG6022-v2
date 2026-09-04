"""Transactional outbox registration; delivery operations are added in P2.6."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import EffectId, EventId, RunId
from orca_agent.orchestration.effects import EffectSpec
from orca_agent.orchestration.events import KernelEvent

from .repositories import json_text


class OutboxStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"


class OutboxRepository:
    """Store deterministic effects in the caller's active transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

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
                        _format_utc(available_at_utc),
                        _format_utc(created_at),
                        _format_utc(created_at),
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


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("outbox times must be timezone-aware UTC")
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = ["OutboxRepository", "OutboxStatus"]
