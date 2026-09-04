"""SQLite repositories for runs, append-only events, and verified snapshots."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from orca_agent.application.errors import ApplicationError, RunNotFoundError, StateIntegrityError
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.errors import DomainError
from orca_agent.domain.ids import EventId, RunId
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.versions import CURRENT_SCHEMA_VERSION
from orca_agent.orchestration.events import KernelEvent
from orca_agent.orchestration.replay import state_hash, verify_snapshot
from orca_agent.orchestration.state import KernelState
from orca_agent.orchestration.versions import ENGINE_VERSION

from .clock import format_utc, parse_utc

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def json_text(value: object) -> str:
    """Serialize a JSON value in the same canonical form used for hashes."""

    return canonical_json_bytes(value).decode("utf-8")


def json_value(value: str, *, what: str) -> object:
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise StateIntegrityError(f"stored {what} is not valid JSON") from error


def stored_int(value: object, *, what: str, minimum: int | None = None) -> int:
    """Read a SQLite integer without coercing corrupt text or booleans."""

    if type(value) is not int:
        raise StateIntegrityError(f"stored {what} is not an integer")
    if minimum is not None and value < minimum:
        raise StateIntegrityError(f"stored {what} is below its minimum")
    return value


@dataclass(frozen=True)
class RunSnapshot:
    run_id: RunId
    schema_version: int
    engine_version: str
    revision: int
    state: KernelState
    state_hash: str
    last_event_id: EventId
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(frozen=True)
class StoredEvent:
    event: KernelEvent
    command_hash: str


class RunRepository:
    """Repository that never writes a snapshot without an explicit CAS."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, run_id: RunId) -> RunSnapshot | None:
        row = self.connection.execute(
            "SELECT run_id, schema_version, engine_version, revision, status, state_json, "
            "state_hash, last_event_id, created_at_utc, updated_at_utc "
            "FROM runs WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            schema_version = stored_int(row[1], what="run schema_version", minimum=1)
            revision = stored_int(row[3], what="run revision", minimum=1)
            stored_run_id = RunId(str(row[0]))
            state = KernelState.model_validate_json(str(row[5]))
            stored_hash = str(row[6])
            if (
                stored_run_id != run_id
                or state.run_id != run_id
                or state.status.value != str(row[4])
                or schema_version != CURRENT_SCHEMA_VERSION
                or str(row[2]) != ENGINE_VERSION
            ):
                raise StateIntegrityError("stored run state does not match run metadata")
            if state_hash(state) != stored_hash:
                raise StateIntegrityError("stored run snapshot hash does not match state")
            last_event_id = EventId(str(row[7]))
            created_at = parse_utc(str(row[8]))
            updated_at = parse_utc(str(row[9]))
        except StateIntegrityError:
            raise
        except (DomainError, ValidationError, ValueError, TypeError, ArithmeticError) as error:
            raise StateIntegrityError("stored run snapshot is invalid") from error
        return RunSnapshot(
            run_id=run_id,
            schema_version=schema_version,
            engine_version=str(row[2]),
            revision=revision,
            state=state,
            state_hash=stored_hash,
            last_event_id=last_event_id,
            created_at_utc=created_at,
            updated_at_utc=updated_at,
        )

    def require(self, run_id: RunId) -> RunSnapshot:
        snapshot = self.get(run_id)
        if snapshot is None:
            raise RunNotFoundError("run was not found", details={"run_id": str(run_id)})
        return snapshot

    def list_ids(self) -> tuple[RunId, ...]:
        rows = self.connection.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall()
        try:
            return tuple(RunId(str(row[0])) for row in rows)
        except DomainError as error:
            raise StateIntegrityError("stored run ID is invalid") from error

    def insert(self, snapshot: RunSnapshot) -> None:
        if snapshot.state_hash != state_hash(snapshot.state):
            raise StateIntegrityError("snapshot hash does not match state")
        self.connection.execute(
            "INSERT INTO runs(run_id, schema_version, engine_version, revision, status, "
            "state_json, state_hash, last_event_id, created_at_utc, updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(snapshot.run_id),
                snapshot.schema_version,
                snapshot.engine_version,
                snapshot.revision,
                snapshot.state.status.value,
                json_text(snapshot.state.model_dump(mode="json")),
                snapshot.state_hash,
                str(snapshot.last_event_id),
                format_utc(snapshot.created_at_utc),
                format_utc(snapshot.updated_at_utc),
            ),
        )

    def compare_and_swap(
        self,
        *,
        run_id: RunId,
        expected_revision: int,
        state: KernelState,
        event_id: EventId,
        updated_at_utc: datetime,
    ) -> bool:
        if state.run_id != run_id:
            raise StateIntegrityError("next state run_id does not match aggregate")
        next_revision = expected_revision + 1
        cursor = self.connection.execute(
            "UPDATE runs SET revision = ?, status = ?, state_json = ?, state_hash = ?, "
            "last_event_id = ?, updated_at_utc = ? "
            "WHERE run_id = ? AND revision = ?",
            (
                next_revision,
                state.status.value,
                json_text(state.model_dump(mode="json")),
                state_hash(state),
                str(event_id),
                format_utc(updated_at_utc),
                str(run_id),
                expected_revision,
            ),
        )
        return cursor.rowcount == 1

    def get_verified(
        self,
        run_id: RunId,
        events: EventRepository,
        *,
        interrupts: object | None = None,
        outbox: object | None = None,
    ) -> RunSnapshot:
        """Load a snapshot only after replaying and checking every projection."""

        snapshot = self.require(run_id)
        stored_events = events.list_for_run(run_id)
        event_values = tuple(item.event for item in stored_events)
        try:
            verify_snapshot(
                snapshot=snapshot.state,
                stored_state_hash=snapshot.state_hash,
                stored_revision=snapshot.revision,
                stored_last_event_id=snapshot.last_event_id,
                events=event_values,
            )
            if interrupts is None:
                from .interrupts import InterruptRepository

                interrupts = InterruptRepository(self.connection)
            if outbox is None:
                from .outbox import OutboxRepository

                outbox = OutboxRepository(self.connection)
            from .integrity import verify_run_projections

            verify_run_projections(
                snapshot=snapshot,
                events=event_values,
                interrupts=interrupts.list_for_run(run_id),  # type: ignore[union-attr]
                outbox=outbox.list_for_run(run_id),  # type: ignore[union-attr]
            )
        except StateIntegrityError:
            raise
        except (ApplicationError, DomainError, ValidationError, TypeError, ValueError) as error:
            raise StateIntegrityError("stored run projections are invalid") from error
        return snapshot


class EventRepository:
    """Append-only event access with hash verification on every load."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _load(self, row: sqlite3.Row) -> StoredEvent:
        try:
            sequence_no = stored_int(row[5], what="event sequence_no", minimum=1)
            expected_revision = stored_int(row[6], what="event expected_revision", minimum=0)
            new_revision = stored_int(row[7], what="event new_revision", minimum=1)
            schema_version = stored_int(row[9], what="event schema_version", minimum=1)
            event = KernelEvent.model_validate_json(
                json.dumps(
                    {
                        "event_id": str(row[0]),
                        "command_id": str(row[1]),
                        "command_type": str(row[2]),
                        "command_hash": str(row[3]),
                        "run_id": str(row[4]),
                        "sequence_no": sequence_no,
                        "expected_revision": expected_revision,
                        "new_revision": new_revision,
                        "event_type": str(row[8]),
                        "schema_version": schema_version,
                        "engine_version": str(row[10]),
                        "payload": json_value(str(row[11]), what="event payload"),
                        "payload_hash": str(row[12]),
                        "result": json_value(str(row[13]), what="event result"),
                        "result_hash": str(row[14]),
                        "occurred_at_utc": str(row[15]),
                        "recorded_at_utc": str(row[16]),
                        "previous_event_hash": str(row[17]),
                        "event_hash": str(row[18]),
                    },
                    ensure_ascii=False,
                )
            )
            if (
                event.schema_version != CURRENT_SCHEMA_VERSION
                or event.engine_version != ENGINE_VERSION
            ):
                raise StateIntegrityError("stored event version is unsupported")
            result = ApplicationResult.model_validate_json(
                json.dumps(thaw_json(event.result), ensure_ascii=False)
            )
            if (
                result.run_id != event.run_id
                or result.event_id != event.event_id
                or result.revision != event.new_revision
            ):
                raise StateIntegrityError("stored event result does not match its envelope")
            if _HASH_PATTERN.fullmatch(str(row[3])) is None:
                raise StateIntegrityError("stored command hash is invalid")
            if event.command_hash != str(row[3]):
                raise StateIntegrityError("stored event command hash does not match")
        except (DomainError, ValidationError, TypeError, ValueError, ArithmeticError) as error:
            raise StateIntegrityError("stored event is invalid") from error
        return StoredEvent(event=event, command_hash=event.command_hash)

    def get_by_command_id(self, command_id: object) -> StoredEvent | None:
        row = self.connection.execute(
            "SELECT event_id, command_id, command_type, command_hash, run_id, sequence_no, "
            "expected_revision, new_revision, event_type, schema_version, engine_version, "
            "payload_json, payload_hash, result_json, result_hash, occurred_at_utc, "
            "recorded_at_utc, previous_event_hash, event_hash "
            "FROM events WHERE command_id = ?",
            (str(command_id),),
        ).fetchone()
        return None if row is None else self._load(row)

    def get(self, event_id: EventId) -> KernelEvent | None:
        row = self.connection.execute(
            "SELECT event_id, command_id, command_type, command_hash, run_id, sequence_no, "
            "expected_revision, new_revision, event_type, schema_version, engine_version, "
            "payload_json, payload_hash, result_json, result_hash, occurred_at_utc, "
            "recorded_at_utc, previous_event_hash, event_hash FROM events WHERE event_id = ?",
            (str(event_id),),
        ).fetchone()
        return None if row is None else self._load(row).event

    def append(self, event: KernelEvent, *, command_hash: str) -> None:
        if event.command_hash != command_hash:
            raise StateIntegrityError("event command hash does not match append envelope")
        self.connection.execute(
            "INSERT INTO events(event_id, command_id, command_type, command_hash, run_id, "
            "sequence_no, expected_revision, new_revision, event_type, schema_version, "
            "engine_version, payload_json, payload_hash, result_json, result_hash, "
            "occurred_at_utc, recorded_at_utc, previous_event_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(event.event_id),
                str(event.command_id),
                event.command_type.value,
                event.command_hash,
                str(event.run_id),
                event.sequence_no,
                event.expected_revision,
                event.new_revision,
                event.event_type.value,
                event.schema_version,
                event.engine_version,
                json_text(event.payload),
                event.payload_hash,
                json_text(event.result),
                event.result_hash,
                event.occurred_at_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                event.recorded_at_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                event.previous_event_hash,
                event.event_hash,
            ),
        )

    def list_for_run(self, run_id: RunId) -> tuple[StoredEvent, ...]:
        rows = self.connection.execute(
            "SELECT event_id, command_id, command_type, command_hash, run_id, sequence_no, "
            "expected_revision, new_revision, event_type, schema_version, engine_version, "
            "payload_json, payload_hash, result_json, result_hash, occurred_at_utc, "
            "recorded_at_utc, previous_event_hash, event_hash "
            "FROM events WHERE run_id = ? ORDER BY sequence_no",
            (str(run_id),),
        ).fetchall()
        return tuple(self._load(row) for row in rows)

    def count_for_run(self, run_id: RunId) -> int:
        return stored_int(
            self.connection.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ?", (str(run_id),)
            ).fetchone()[0],
            what="event count",
            minimum=0,
        )


__all__ = [
    "EventRepository",
    "RunRepository",
    "RunSnapshot",
    "StoredEvent",
    "json_text",
    "json_value",
    "stored_int",
]
