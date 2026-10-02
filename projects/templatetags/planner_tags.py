"""Template filters for the planner's own display conventions (#189).

`projects` is a plain app in INSTALLED_APPS with APP_DIRS enabled, so this
module is autodiscovered — templates only need `{% load planner_tags %}`.
"""

from django import template

from ..date_format import MONTHS_SHORT, WEEKDAYS_SHORT, format_date

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
# all in it. Ordered as the client indexes them: weekdays from Monday (the
# order WEEKDAYS_SHORT is already in), months from January.
_NAME_TABLES = {
    "weekdays": tuple(WEEKDAYS_SHORT),
    "months": tuple(MONTHS_SHORT[month] for month in range(1, 13)),
}


@register.simple_tag
def date_names(table):
    """One of date_format's German name tables, comma-joined for a data
    attribute. An unknown table raises for format_date's reason: the name is
    passed as a bare string from a template, so a typo has no other way of
    announcing itself — it would render an empty attribute and the client
    would compose `undefined` into a date."""
    try:
        names = _NAME_TABLES[table]
    except KeyError:
        raise ValueError(
            f"unknown date name table {table!r} — expected one of "
            f"{', '.join(sorted(_NAME_TABLES))}"
        ) from None
    return ",".join(names)
