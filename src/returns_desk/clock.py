from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A clock that only moves when told to. Tests and the seeded demo use it so that
    return windows, retry backoff and return history are reproducible."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock needs a timezone-aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FrozenClock needs a timezone-aware datetime")
        self._now = moment.astimezone(UTC)

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> datetime:
        self._now = self._now + (delta if delta is not None else timedelta(**kwargs))
        return self._now
