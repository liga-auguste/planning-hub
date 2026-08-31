"""Calendar-week comparisons shared by views.py and ai.py (#169).

A separate module rather than living in either: views.py already imports
from ai.py, so the reverse import would be circular.
"""

from datetime import date


def is_same_iso_week(a: date, b: date) -> bool:
    """True if a and b fall in the same ISO calendar week (Monday-Sunday).

    Comparing the (ISO year, ISO week) tuple, not the bare week number, is
    what makes this safe across a year boundary — isocalendar() already
    assigns Dec 29-31 to week 1 of the following ISO year when appropriate.
    """
    return a.isocalendar()[:2] == b.isocalendar()[:2]
