"""Small clock boundary for user-facing dates.

Database/audit timestamps remain UTC. Report names, email subjects and other
calendar labels use the configured schedule timezone so a GitHub runner's local
timezone cannot move a Friday report onto the previous/next date.
"""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
from zoneinfo import ZoneInfo

import config


_research_time: ContextVar[datetime | None] = ContextVar("research_time", default=None)
_observation_time: ContextVar[datetime | None] = ContextVar("observation_time", default=None)
_research_days: ContextVar[int | None] = ContextVar("research_days", default=None)


def research_now(reference_time: datetime | None = None) -> datetime:
    """Business-window time, independent of wall-clock audit and timeout clocks."""
    value = reference_time or _research_time.get() or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reference_time must be timezone-aware")
    return value.astimezone(timezone.utc)


@contextmanager
def research_window(reference_time: datetime | None = None, *, observation_time: datetime | None = None, lookback_days: int | None = None):
    """Freeze one run's window; async children inherit it, other runs do not."""
    token = _research_time.set(research_now(reference_time))
    observed = observation_time or research_now()
    if observed.tzinfo is None:
        _research_time.reset(token)
        raise ValueError("observation_time must be timezone-aware")
    observed_token = _observation_time.set(observed.astimezone(timezone.utc))
    days_token = _research_days.set(lookback_days or _research_days.get())
    try:
        yield research_now()
    finally:
        _research_time.reset(token)
        _observation_time.reset(observed_token)
        _research_days.reset(days_token)


def observation_now() -> datetime:
    """Metric observations must not be backdated to a resumed report window."""
    return _observation_time.get() or datetime.now(timezone.utc)


def research_days() -> int:
    return _research_days.get() or config.SOURCING_LOOKBACK_DAYS


def scheduled_now(
    reference_time: datetime | None = None,
    *,
    timezone_name: str = "",
) -> datetime:
    value = reference_time or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("reference_time must be timezone-aware")
    return value.astimezone(
        ZoneInfo(timezone_name or config.SCHEDULE_TIMEZONE)
    )


def scheduled_date(
    reference_time: datetime | None = None,
    *,
    timezone_name: str = "",
) -> str:
    return scheduled_now(
        reference_time,
        timezone_name=timezone_name,
    ).strftime("%Y-%m-%d")
