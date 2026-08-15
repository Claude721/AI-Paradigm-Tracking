"""Small clock boundary for user-facing dates.

Database/audit timestamps remain UTC. Report names, email subjects and other
calendar labels use the configured schedule timezone so a GitHub runner's local
timezone cannot move a Friday report onto the previous/next date.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config


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
