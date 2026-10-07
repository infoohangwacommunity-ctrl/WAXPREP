"""Clock abstraction for timezone-aware UTC timestamps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Provides the current time as an aware UTC datetime."""

    def now(self) -> datetime:
        """Return the current time in UTC."""
        ...


class SystemClock:
    """Production clock using the system UTC time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Deterministic clock for tests."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime.")
        self._current = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._current

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime.")
        self._current = when.astimezone(UTC)

    def advance(self, seconds: float) -> None:
        self._current = self._current + timedelta(seconds=seconds)


__all__ = ["Clock", "FakeClock", "SystemClock"]
