"""Clock abstraction tests."""

from datetime import UTC, datetime, timedelta

from waxprep.clock import FakeClock, SystemClock


def test_system_clock_is_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_fake_clock_is_controllable() -> None:
    start = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    assert clock.now() == start
    clock.advance(60)
    assert clock.now() == start + timedelta(seconds=60)
