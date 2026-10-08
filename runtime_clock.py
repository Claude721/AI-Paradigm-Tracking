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


def research_now(reference_time: datetime | None = None) -> datetime:
    """Business-window time, independent of wall-clock audit and timeout clocks."""
    value = reference_time or _research_time.get() or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reference_time must be timezone-aware")
    return value.astimezone(timezone.utc)


@contextmanager
def research_window(reference_time: datetime | None = None):
    """Freeze one run's window; async children inherit it, other runs do not."""
    token = _research_time.set(research_now(reference_time))
    try:
        yield research_now()
    finally:
        _research_time.reset(token)


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
