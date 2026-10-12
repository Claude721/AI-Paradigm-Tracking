"""Shared conservative transport policy; unrelated to the research clock."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

OPENREVIEW_REQUEST_SPACING_SECONDS = 3.1
MAX_RETRY_AFTER_SECONDS = 60.0


def retry_after_seconds(value: str, *, now: datetime | None = None) -> float | None:
    """Parse delta seconds or HTTP date without shortening a provider's wait.

    Missing/malformed values stay unknown. Use wall time (or an injected test
    clock), NOT the fixed historical research window, for HTTP dates.
    """
    value = str(value or "").strip()
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = (date - (now or datetime.now(timezone.utc))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None
