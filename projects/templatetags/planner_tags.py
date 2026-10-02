"""Template filters for the planner's own display conventions (#189).

`projects` is a plain app in INSTALLED_APPS with APP_DIRS enabled, so this
module is autodiscovered — templates only need `{% load planner_tags %}`.
"""

from django import template

from ..date_format import MONTHS_DE, MONTHS_SHORT, WEEKDAYS_SHORT, format_date

register = template.Library()


@register.filter
def plan_date(d, role="long"):
    """A date in the project's German display format, resolved at render
    time. See date_format.format_date for what the roles produce — a role
    that module does not know raises, so a typo here is a loud failure
    rather than a quietly wrong format."""
    return format_date(d, role)


# #279: the two name tables the add row's date has to compose a label from in
# the client. Rendered from here rather than retyped in JavaScript, so
# date_format stays the one place the German names live and #192 finds them
# all in it.
#
# Per role, because which form a surface spells a date in is a statement about
# that surface (#238) and the add row follows the list it closes: the
# dashboard's rows abbreviate the month, /mein-plan/'s write it out. The roles
# differ in nothing else — same weekdays, same shape — which is why one
# template literal in the client serves both and only the tables travel.
#
# Ordered as the client indexes them: weekdays from Monday (the order
# WEEKDAYS_SHORT is already in), months from January.
_MONTH_TABLES = {"long": MONTHS_DE, "row": MONTHS_SHORT}
_NAME_TABLES = {
    role: {
        "weekdays": tuple(WEEKDAYS_SHORT),
        "months": tuple(months[month] for month in range(1, 13)),
    }
    for role, months in _MONTH_TABLES.items()
}


@register.simple_tag
def date_names(table, role):
    """One of date_format's German name tables, as the given role spells it,
    comma-joined for a data attribute. An unknown table or role raises for
    format_date's reason: both are passed as bare strings from a template, so
    a typo has no other way of announcing itself — it would render an empty
    attribute and the client would compose `undefined` into a date.

    Only the roles the add row can be included under are listed. A role
    format_date knows but this does not is the same loud failure: the client
    cannot compose a form whose names it was never handed."""
    try:
        names = _NAME_TABLES[role][table]
    except KeyError:
        raise ValueError(
            f"no date name table {table!r} for role {role!r} — expected one of "
            f"{', '.join(sorted(_NAME_TABLES['row']))} "
            f"for one of {', '.join(sorted(_NAME_TABLES))}"
        ) from None
    return ",".join(names)
