"""Calendar-week comparisons shared by views.py and ai.py (#169).

A separate module rather than living in either: views.py already imports
from ai.py, so the reverse import would be circular.
"""

from datetime import date, timedelta


def iso_week_bounds(d: date) -> tuple[date, date]:
    """The Monday and Sunday of the ISO calendar week containing d — the
    same Monday-Sunday week is_same_iso_week compares by (ISO year, ISO
    week) rather than by date range."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def is_same_iso_week(a: date, b: date) -> bool:
    """True if a and b fall in the same ISO calendar week (Monday-Sunday).

    Comparing the (ISO year, ISO week) tuple, not the bare week number, is
    what makes this safe across a year boundary — isocalendar() already
    assigns Dec 29-31 to week 1 of the following ISO year when appropriate.
    """
    return a.isocalendar()[:2] == b.isocalendar()[:2]
