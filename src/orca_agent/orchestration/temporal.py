"""UTC validation shared by pure kernel records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("business time must be timezone-aware UTC")
    return value.astimezone(UTC)


__all__ = ["ensure_utc"]
