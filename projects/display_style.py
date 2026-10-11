"""Storage for the date style chosen on /regeln/ (#192 option 2).

Two backends behind one interface, the way rules.py does it: production keeps
the choice in the single DisplaySetting row, demo mode in request.session, so
one anonymous visitor cannot change how every other visitor's dates read.
Every public function takes the request first and branches on DEMO_MODE.

With nothing chosen, settings.DATE_STYLE decides — the deploy-time default
from option 1, which startup.require_valid_date_style already guards. A
stored value that names no style is ignored the same way, rather than
raising: it can only come from a style that was removed after it was saved,
and the page should keep rendering. Saving one is refused instead.
"""

from django.conf import settings

from .date_format import DATE_STYLES, style_resolver
from .models import DisplaySetting

DEMO_DATE_STYLE_KEY = "demo_date_style"

# The names the /regeln/ form shows. German: they are UI. Same keys as
# DATE_STYLES, which a test pins.
STYLE_LABELS = {
    "standard": "Standard",
    "numeric": "Numerisch",
    "no_weekday": "Ohne Wochentag",
}


def _stored_style(request):
    if settings.DEMO_MODE:
        return request.session.get(DEMO_DATE_STYLE_KEY)
    row = DisplaySetting.objects.filter(pk=1).first()
    return row.date_style if row else None


def get_style(request):
    """The style this request renders in."""
    stored = _stored_style(request)
    return stored if stored in DATE_STYLES else settings.DATE_STYLE


def set_style(request, style):
    """Stores the choice. Returns False, storing nothing, for a name that is
    not in DATE_STYLES."""
    if style not in DATE_STYLES:
        return False
    if settings.DEMO_MODE:
        request.session[DEMO_DATE_STYLE_KEY] = style
    else:
        DisplaySetting.objects.update_or_create(pk=1, defaults={"date_style": style})
    return True


class DateStyleMiddleware:
    """Makes every format_date call within a request use get_style(request).

    Lazy and memoised: the stored choice costs one query (or one session read)
    the first time a date is formatted, and nothing on a request that formats
    none — /health/ among them. Reset afterwards, so a thread that serves the
    next request starts from the default again.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        resolved = []

        def resolve():
            if not resolved:
                resolved.append(get_style(request))
            return resolved[0]

        with style_resolver(resolve):
            return self.get_response(request)
