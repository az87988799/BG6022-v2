"""Independent SQLite unit-of-work boundary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from orca_agent.infrastructure.migrations import apply_migrations

from .clock import Clock, SystemClock
from .outbox import OutboxRepository
from .repositories import EventRepository, RunRepository
from .sqlite import SQLiteConnectionFactory, begin_immediate


class SQLiteUnitOfWork:
    """Own one connection and one explicit writer transaction."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Clock | None = None,
        connection_factory: SQLiteConnectionFactory | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.connection_factory = connection_factory or SQLiteConnectionFactory(database_path)
        self.connection: sqlite3.Connection | None = None
        self.runs: RunRepository | None = None
        self.events: EventRepository | None = None
        self.outbox: OutboxRepository | None = None
        self._closed = False

    def __enter__(self) -> SQLiteUnitOfWork:
        self.connection = self.connection_factory.connect()
        apply_migrations(self.connection, clock=self.clock)
        self.runs = RunRepository(self.connection)
        self.events = EventRepository(self.connection)
        self.outbox = OutboxRepository(self.connection)
        return self

    def begin(self) -> None:
        self._require_connection()
        begin_immediate(self.connection)

    def commit(self) -> None:
        self._require_connection()
        self.connection.commit()

    def rollback(self) -> None:
        if self.connection is not None and self.connection.in_transaction:
            self.connection.rollback()

    def _require_connection(self) -> None:
        if self.connection is None or self._closed:
            raise RuntimeError("unit of work is not open")

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.rollback()
        if self.connection is not None:
            self.connection.close()
        self._closed = True


UnitOfWork = SQLiteUnitOfWork

__all__ = ["SQLiteUnitOfWork", "UnitOfWork"]
