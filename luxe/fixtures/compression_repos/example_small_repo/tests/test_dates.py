from datetime import datetime, timezone

from dates import add_business_days, days_between, parse_iso_date


def test_parse_iso_date_date_only():
    dt = parse_iso_date("2024-01-15")
    assert dt.year == 2024 and dt.month == 1 and dt.day == 15


def test_parse_iso_date_naive_datetime():
    dt = parse_iso_date("2024-01-15T09:30:00")
    assert dt.hour == 9 and dt.minute == 30
    assert dt.tzinfo is None


def test_parse_iso_date_preserves_timezone():
    dt = parse_iso_date("2024-01-15T09:30:00+00:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == (datetime(1, 1, 1, tzinfo=timezone.utc)).utcoffset()


def test_days_between_positive():
    a = datetime(2024, 1, 10)
    b = datetime(2024, 1, 1)
    assert days_between(a, b) == 9


def test_add_business_days_skips_weekend():
    # Friday → Monday is +1 business day.
    friday = datetime(2024, 1, 5)
    result = add_business_days(friday, 1)
    assert result.weekday() == 0  # Monday
