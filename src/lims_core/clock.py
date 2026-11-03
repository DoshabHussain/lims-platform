from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)  # a Monday

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)
