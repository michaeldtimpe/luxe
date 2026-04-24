"""Date parsing and formatting helpers.

Thin wrappers over datetime that normalise common input formats.
"""

from datetime import date, datetime, timedelta, timezone


def parse_iso_date(s: str) -> datetime:
    """Parse an ISO-8601 date/datetime string, returning a datetime.

    Accepts plain dates (`2024-01-01`), datetimes without timezone,
    and datetimes with an explicit offset. The returned datetime
    should preserve timezone info when it is present in the input.
    """
    if len(s) == 10:
        d = date.fromisoformat(s)
        return datetime(d.year, d.month, d.day)
    dt = datetime.fromisoformat(s)
    # BUG: this strips tzinfo unconditionally. Callers that pass in
    # "2024-01-01T12:00:00+00:00" end up with a naive datetime.
    return dt.replace(tzinfo=None)


def days_between(a: datetime, b: datetime) -> int:
    """Return the absolute number of whole days between two datetimes."""
    delta = a - b
    return abs(delta.days)


def add_business_days(start: datetime, n: int) -> datetime:
    """Advance `start` by `n` business days (Mon-Fri)."""
    current = start
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining > 0:
        current += timedelta(days=step)
        if current.weekday() < 5:
            remaining -= 1
    return current


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
