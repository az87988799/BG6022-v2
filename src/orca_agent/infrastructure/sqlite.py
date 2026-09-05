"""SQLite connection policy for the durable kernel."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from orca_agent.application.errors import StorageBusyError


def resolve_database_path(state_root: str | Path) -> Path:
    """Resolve a configured state root without embedding a machine-specific path."""

    if str(state_root) == ":memory:":
        return Path(":memory:")
    root = Path(state_root)
    if root.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        return root
    return root / "state.sqlite3"


class SQLiteConnectionFactory:
    """Create one independently configured connection per unit of work."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        state_root: str | Path | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if database_path is None and state_root is None:
            raise ValueError("database_path or state_root is required")
        if database_path is not None and state_root is not None:
            raise ValueError("database_path and state_root are mutually exclusive")
        configured_path = database_path if database_path is not None else state_root
        self.database_path = resolve_database_path(configured_path)  # type: ignore[arg-type]
        self.timeout_seconds = timeout_seconds

    def connect(self) -> sqlite3.Connection:
        if str(self.database_path) != ":memory:":
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(
                str(self.database_path),
                timeout=self.timeout_seconds,
                isolation_level=None,
                check_same_thread=True,
            )
        except sqlite3.OperationalError as error:
            if "locked" in str(error).casefold() or "busy" in str(error).casefold():
                raise StorageBusyError("database is busy") from error
            raise

        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.OperationalError as error:
            connection.close()
            if "locked" in str(error).casefold() or "busy" in str(error).casefold():
                raise StorageBusyError("database is busy") from error
            raise
        except sqlite3.Error:
            connection.close()
            raise
        return connection


def connect_sqlite(
    database_path: str | Path | None = None,
    *,
    state_root: str | Path | None = None,
    timeout_seconds: float = 5.0,
) -> sqlite3.Connection:
    """Convenience wrapper for callers that do not need to retain a factory."""

    return SQLiteConnectionFactory(
        database_path,
        state_root=state_root,
        timeout_seconds=timeout_seconds,
    ).connect()


def begin_immediate(connection: sqlite3.Connection) -> None:
    """Start the deterministic writer transaction used by every UoW."""

    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        if "locked" in str(error).casefold() or "busy" in str(error).casefold():
            raise StorageBusyError("database is busy") from error
        raise


__all__ = [
    "SQLiteConnectionFactory",
    "begin_immediate",
    "connect_sqlite",
    "resolve_database_path",
]
