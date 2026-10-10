"""Template filters for the planner's own display conventions (#189).

`projects` is a plain app in INSTALLED_APPS with APP_DIRS enabled, so this
module is autodiscovered — templates only need `{% load planner_tags %}`.
"""

from django import template

from ..date_format import (
    MONTHS_DE,
    MONTHS_SHORT,
    WEEKDAYS_SHORT,
    format_date,
    role_pattern,
)

register = template.Library()


@register.filter
def plan_date(d, role="long"):
    """A date in the project's German display format, resolved at render
    time. See date_format.format_date for what the roles produce — a role
    that module does not know raises, so a typo here is a loud failure
    rather than a quietly wrong format."""
    return format_date(d, role)


@register.filter
def closing_period(label):
    """The period that ends a sentence whose last word is a date label, or
    nothing when the label already ends in one (#192). German does not
    double it: under the "numeric" style a date reads "03.03.", and that
    period closes the sentence too. Returns only the punctuation, so it also
    works where markup sits between the label and the sentence's end."""
    return "" if str(label).endswith(".") else "."


# #279: the two name tables the add row's date has to compose a label from in
# the client. Rendered from here rather than retyped in JavaScript, so
# date_format stays the one place the German names live.
#
# Per role, because which form a surface spells a date in is a statement about
# that surface (#238) and the add row follows the list it closes: the
# dashboard's rows abbreviate the month, /mein-plan/'s write it out. The shape
# the names go into travels separately, through date_pattern below (#192).
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


@register.simple_tag
def date_pattern(role):
    """The pattern the configured DATE_STYLE resolves a role to, for the add
    row to fill in the client (#192). Handed over rather than mirrored in
    JavaScript, so a style is written once, in date_format.DATE_STYLES.

    Limited to the roles date_names serves, and for its reason: the client
    can only fill a pattern whose names it was handed. An unknown role
    raises."""
    if role not in _NAME_TABLES:
        raise ValueError(
            f"no client date pattern for role {role!r} — expected one of "
            f"{', '.join(sorted(_NAME_TABLES))}"
        )
    return role_pattern(role)
