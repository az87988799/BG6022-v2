"""An offline, deterministic fake backend; no ORCA, network, or subprocess calls."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import ExecutionId
from orca_agent.domain.models import ValidatedAction
from orca_agent.domain.p3 import ExecutionIntent
from orca_agent.infrastructure.clock import Clock, SystemClock, format_utc
from orca_agent.infrastructure.sqlite import SQLiteConnectionFactory
from orca_agent.planning.water import WaterFixture


@dataclass(frozen=True)
class FakeBackendResult:
    execution_id: ExecutionId
    input_hash: str
    fixture_hash: str
    raw_result: bytes
    reused: bool
    call_count: int


class FakeBackend:
    """Persist one deterministic result per execution ID in a separate SQLite file."""

    def __init__(self, state_root: str | Path, *, clock: Clock | None = None) -> None:
        self.state_root = Path(state_root)
        self.database_path = self.state_root / "fake_backend.sqlite"
        self.clock = clock or SystemClock()
        self.state_root.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fake_executions (
                    execution_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    input_hash TEXT NOT NULL,
                    fixture_id TEXT NOT NULL,
                    fixture_version TEXT NOT NULL,
                    fixture_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status = 'succeeded'),
                    raw_result BLOB NOT NULL,
                    call_count INTEGER NOT NULL CHECK (call_count = 1),
                    created_at_utc TEXT NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def submit_or_get(
        self,
        *,
        intent: ExecutionIntent,
        action: ValidatedAction,
        fixture: WaterFixture,
    ) -> FakeBackendResult:
        input_hash = sha256_hex(
            {
                "action_hash": action.action_hash,
                "execution_id": str(intent.execution_id),
                "fixture_hash": fixture.fixture_hash,
            }
        )
        raw_result = canonical_json_bytes(
            {
                "format": "p3-fake-water-result-v1",
                "schema_version": 2,
                "engine_version": "p3-water-v1",
                "execution_id": str(intent.execution_id),
                "action_id": str(action.action_id),
                "input_hash": input_hash,
                "fixture_id": fixture.fixture_id,
                "fixture_version": fixture.fixture_version,
                "fixture_hash": fixture.fixture_hash,
                "energy": fixture.energy,
                "unit": fixture.unit,
                "source": fixture.source,
            }
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT execution_id, idempotency_key, input_hash, fixture_id, fixture_version, "
                "fixture_hash, status, raw_result, call_count "
                "FROM fake_executions WHERE execution_id = ?",
                (str(intent.execution_id),),
            ).fetchone()
            if row is not None:
                if (
                    tuple(row[:7])
                    != (
                        str(intent.execution_id),
                        intent.idempotency_key,
                        input_hash,
                        fixture.fixture_id,
                        fixture.fixture_version,
                        fixture.fixture_hash,
                        "succeeded",
                    )
                    or bytes(row[7]) != raw_result
                    or row[8] != 1
                ):
                    raise StateIntegrityError("fake execution binding does not match request")
                connection.commit()
                return FakeBackendResult(
                    execution_id=intent.execution_id,
                    input_hash=input_hash,
                    fixture_hash=fixture.fixture_hash,
                    raw_result=raw_result,
                    reused=True,
                    call_count=int(row[8]),
                )
            try:
                connection.execute(
                    "INSERT INTO fake_executions(execution_id, idempotency_key, input_hash, "
                    "fixture_id, fixture_version, fixture_hash, status, raw_result, call_count, "
                    "created_at_utc) VALUES (?, ?, ?, ?, ?, ?, 'succeeded', ?, 1, ?)",
                    (
                        str(intent.execution_id),
                        intent.idempotency_key,
                        input_hash,
                        fixture.fixture_id,
                        fixture.fixture_version,
                        fixture.fixture_hash,
                        raw_result,
                        format_utc(self.clock.now_utc()),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StateIntegrityError(
                    "fake execution idempotency key is already bound"
                ) from error
            connection.commit()
            return FakeBackendResult(
                execution_id=intent.execution_id,
                input_hash=input_hash,
                fixture_hash=fixture.fixture_hash,
                raw_result=raw_result,
                reused=False,
                call_count=1,
            )
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def execution_count(self) -> int:
        connection = self._connect()
        try:
            return int(connection.execute("SELECT count(*) FROM fake_executions").fetchone()[0])
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        return SQLiteConnectionFactory(self.database_path).connect()


__all__ = ["FakeBackend", "FakeBackendResult"]
