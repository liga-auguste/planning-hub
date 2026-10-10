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

from django.conf import settings

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


# #192: every display date is a role (chosen per call site, below) resolved
# through a style (chosen once, settings.DATE_STYLE). The two axes stay
# separate because the surfaces need different densities whatever the style
# — a calendar card has no room for a weekday in any of them.
#
# A fixed set of named styles rather than a free-form format string: the
# German names live in the tables above, which a Django or strftime format
# string would never reach (#14). The patterns are internal, filled from
# exactly the tokens _date_parts produces, and they are also what the add row
# composes its label from in the client (#279) — task_add_row.js reads the
# pattern off the partial and fills the same tokens, so a style is written
# once, here, and a test pins the client's token names against these.
#
# Every style defines every role. A role names its surface, not its format,
# so a style may resolve two roles to the same output ("numeric" does), but it
# may not drop one.
DATE_STYLES = {
    # What every surface rendered before #192.
    "standard": {
        "long": "{weekday}, {day}. {month}",
        # #238: the task row, where the spelled-out month cost width the task
        # name needed on a phone. Named "row" and not "short": "short" is the
        # numeric calendar form the day cards use, and a role names its
        # surface rather than its format. No trailing period on the
        # abbreviation — MONTHS_SHORT carries none and format_week_range has
        # read fine without one since it was written.
        "row": "{weekday}, {day}. {month}",
        "short": "{dd}.{mm}.",
        # #214: the empty-summary note, where the date sits inside a sentence
        # ("Die nächste Aufgabe ist am 23. Dezember."). No weekday: it would
        # read as a second clause there.
        "note": "{day}. {month}",
    },
    "numeric": {
        "long": "{weekday}, {dd}.{mm}.",
        "row": "{weekday}, {dd}.{mm}.",
        "short": "{dd}.{mm}.",
        "note": "{dd}.{mm}.",
    },
    "no_weekday": {
        "long": "{day}. {month}",
        "row": "{day}. {month}",
        "short": "{dd}.{mm}.",
        "note": "{day}. {month}",
    },
}

# Which month table a role's {month} token spells with. A property of the
# role rather than the style: the row abbreviates because a phone's task row
# is narrow, whichever style is set. "short" never spells a month, but carries
# a table so every role resolves the same way.
_ROLE_MONTHS = {
    "long": MONTHS_DE,
    "row": MONTHS_SHORT,
    "short": MONTHS_DE,
    "note": MONTHS_DE,
}


def role_pattern(role):
    """The pattern the configured style resolves a role to.

    An unknown role raises rather than falling back to "long": callers pass
    the role as a bare string, including from templates, so a typo has no
    other way of announcing itself — it would just render the wrong format
    and no test could catch it. An unknown style raises for the same reason:
    a typo in .env would otherwise render the default and look like the
    setting had no effect.

    The setting is read on every call, not once at import, so a changed
    DATE_STYLE takes effect on the next render.
    """
    style = settings.DATE_STYLE
    try:
        patterns = DATE_STYLES[style]
    except KeyError:
        raise ValueError(
            f"unknown DATE_STYLE {style!r} — expected one of "
            f"{', '.join(sorted(DATE_STYLES))}"
        ) from None
    try:
        return patterns[role]
    except KeyError:
        raise ValueError(
            f"unknown date role {role!r} — expected one of "
            f"{', '.join(sorted(patterns))}"
        ) from None


def _date_parts(d, role):
    return {
        "weekday": WEEKDAYS_SHORT[d.weekday()],
        "day": d.day,
        "dd": f"{d.day:02d}",
        "mm": f"{d.month:02d}",
        "month": _ROLE_MONTHS[role][d.month],
    }


def format_date(d, role="long"):
    """A display date in the format the given role calls for, in the
    configured style.

    The role argument exists because no single format serves every surface:
    a task row wants the weekday but not the spelled-out month ("Mo, 15.
    Jun"), the summary around it has room for both ("Mo, 15. Juni"), and a
    calendar card has room for the numeric form only ("03.03."). Callers
    name the surface, not the format, so a style can change what a role
    produces in one place. See role_pattern for why an unknown role or style
    raises.

    The role is checked before the date, so an undated row fails the same
    way a dated one does. Otherwise a typo would surface only once some
    task happened to carry a due date.
    """
    pattern = role_pattern(role)
    if not d:
        return ""
    return pattern.format(**_date_parts(d, role))


def format_week_range(monday, sunday):
    """A week's span for a heading. Deliberately outside DATE_STYLES (#192):
    the same string goes into the close-out prompt (ai.py), whose dates the
    display setting must not reach."""
    if monday.month == sunday.month:
        return f"{monday.day}.–{sunday.day}. {MONTHS_DE[sunday.month]}"
    return (
        f"{monday.day}. {MONTHS_SHORT[monday.month]} – "
        f"{sunday.day}. {MONTHS_DE[sunday.month]}"
    )
