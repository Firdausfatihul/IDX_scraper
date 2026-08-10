from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse


def parse_boundary(value: str, timezone: str, *, is_end: bool = False) -> datetime:
    """Parse ISO date/datetime. Date-only end values include the full day."""
    tz = ZoneInfo(timezone)
    if len(value) == 10:
        day = datetime.strptime(value, "%Y-%m-%d").date()
        local_time = time.max if is_end else time.min
        return datetime.combine(day, local_time, tzinfo=tz)

    parsed = isoparse(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def parse_idx_datetime(value: str, timezone: str) -> datetime:
    parsed = isoparse(value)
    tz = ZoneInfo(timezone)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)
