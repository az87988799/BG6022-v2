"""Injectable UTC clocks used by application and storage boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


def ensure_utc(value: datetime) -> datetime:
    """Return a normalized UTC datetime or reject a naive/non-UTC value."""

    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("business time must be timezone-aware UTC")
    return value.astimezone(UTC)


def format_utc(value: datetime) -> str:
    """Format business time in the single SQLite text representation."""

    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(value: str) -> datetime:
    """Parse the canonical SQLite UTC representation."""

    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid UTC timestamp") from error
    return parsed


class Clock(Protocol):
    def now_utc(self) -> datetime:
        """Return the current timezone-aware UTC time."""


class SystemClock:
    """Production clock backed by the system UTC clock."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Deterministic mutable clock intended for tests and simulations."""

    def __init__(self, initial: datetime) -> None:
        self._current = ensure_utc(initial)

    def now_utc(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("FrozenClock cannot move backwards")
        self._current += delta


__all__ = ["Clock", "FrozenClock", "SystemClock", "ensure_utc", "format_utc", "parse_utc"]
