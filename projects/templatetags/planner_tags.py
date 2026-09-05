"""Template filters for the planner's own display conventions (#189).

`projects` is a plain app in INSTALLED_APPS with APP_DIRS enabled, so this
module is autodiscovered — templates only need `{% load planner_tags %}`.
"""

from django import template

from ..date_format import format_date

register = template.Library()


@register.filter
def plan_date(d, role="long"):
    """A date in the project's German display format, resolved at render
    time. See date_format.format_date for what the roles produce."""
    return format_date(d, role)
