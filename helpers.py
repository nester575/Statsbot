"""Pure utility functions — no DB, no Flask, no Telegram."""
from datetime import datetime, timedelta

import config


def is_working_day(d):
    """Returns True if `d` is Mon-Fri AND not in HOLIDAYS."""
    return d.weekday() < 5 and d.isoformat() not in config.HOLIDAYS


def parse_hhmm(s):
    """Parse 'HH:MM' to (hour, minute); None on bad input."""
    try:
        h, m = s.strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def parse_number(s):
    """Parse a user-typed number: handles commas, spaces, NBSP.
    Returns float or None.
    """
    if s is None:
        return None
    cleaned = str(s).replace(" ", "").replace(" ", "").replace(",", ".").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def period_range(period):
    """Return (start_date, end_date) for the requested period.

    - "month": from 1st of the current month to today
    - any other value: last 7 calendar days (today-6 .. today)
    """
    today = datetime.now(config.BISHKEK).date()
    if period == "month":
        start = today.replace(day=1)
    else:
        start = today - timedelta(days=6)
    return start, today
