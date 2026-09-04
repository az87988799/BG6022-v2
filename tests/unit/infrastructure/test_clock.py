from datetime import UTC, datetime, timedelta, timezone

import pytest

from orca_agent.infrastructure.clock import FrozenClock, SystemClock, format_utc, parse_utc


def test_system_clock_is_utc_aware() -> None:
    now = SystemClock().now_utc()
    assert now.tzinfo is UTC
    assert now.utcoffset() == timedelta(0)


def test_frozen_clock_advances_without_sleep() -> None:
    initial = datetime(2026, 9, 4, 12, 0, 0, 123456, tzinfo=UTC)
    clock = FrozenClock(initial)

    clock.advance(timedelta(seconds=2, microseconds=3))

    assert clock.now_utc() == datetime(2026, 9, 4, 12, 0, 2, 123459, tzinfo=UTC)
    assert parse_utc(format_utc(clock.now_utc())) == clock.now_utc()

    with pytest.raises(ValueError):
        clock.advance(timedelta(seconds=-1))


def test_clock_rejects_naive_and_non_utc_values() -> None:
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 9, 4))
    with pytest.raises(ValueError):
        format_utc(datetime(2026, 9, 4, tzinfo=timezone(timedelta(hours=8))))
