"""German display formatting for dates, shared by views.py and the
planner_tags template filter (#189).

A separate module rather than living in views.py: the template filter needs
the same function the views call, and formatting has to happen at render
time rather than being baked into the dashboard cache. Not folded into
dates.py — that module holds the ISO-week *comparisons*, which are logic,
not presentation.

#14: kept rather than switched to Django's l10n date formatting. Every date
display that reads LANGUAGE_CODE-dependent formatting (dashboard, kanban,
/mein-plan/, /stats/, planner review, Markdown export) goes through these
tables or format_date(), not Django's |date filter — the remaining |date
uses in the templates are fully numeric, locale-invariant formats. Removing
these would buy nothing.
"""

MONTHS_DE = {
    1: "Januar",
    2: "Februar",
    3: "März",
    4: "April",
    5: "Mai",
    6: "Juni",
    7: "Juli",
    8: "August",
    9: "September",
    10: "Oktober",
    11: "November",
    12: "Dezember",
}
MONTHS_SHORT = {
    1: "Jan",
    2: "Feb",
    3: "Mär",
    4: "Apr",
    5: "Mai",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Okt",
    11: "Nov",
    12: "Dez",
}
WEEKDAYS_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


# One entry per surface. A dict rather than an if/elif chain so an
# unrecognised role is a missing key — see format_date on why that has to
# be an error rather than a fallback.
_ROLE_FORMATTERS = {
    "long": lambda d: f"{WEEKDAYS_SHORT[d.weekday()]}, {d.day}. {MONTHS_DE[d.month]}",
    "short": lambda d: f"{d.day:02d}.{d.month:02d}.",
}


def format_date(d, role="long"):
    """A display date in the format the given role calls for.

    The role argument exists because no single format serves every surface:
    a task row wants the weekday ("Mo, 15. Juni"), a calendar card has room
    for the numeric form only ("03.03."). Callers name the surface, not the
    format, so #192 can change what a role produces in one place.

    An unknown role raises rather than falling back to "long": callers pass
    the role as a bare string, including from templates, so a typo has no
    other way of announcing itself — it would just render the wrong format
    and no test could catch it. #192 adds and changes roles, which is
    exactly when a silent fallback would start costing something.

    The role is checked before the date, so an undated row fails the same
    way a dated one does. Otherwise a typo would surface only once some
    task happened to carry a due date.
    """
    try:
        formatter = _ROLE_FORMATTERS[role]
    except KeyError:
        raise ValueError(
            f"unknown date role {role!r} — expected one of "
            f"{', '.join(sorted(_ROLE_FORMATTERS))}"
        ) from None
    if not d:
        return ""
    return formatter(d)


def format_week_range(monday, sunday):
    if monday.month == sunday.month:
        return f"{monday.day}.–{sunday.day}. {MONTHS_DE[sunday.month]}"
    return (
        f"{monday.day}. {MONTHS_SHORT[monday.month]} – "
        f"{sunday.day}. {MONTHS_DE[sunday.month]}"
    )
