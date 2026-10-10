import os

from django.conf import settings

from .date_format import DATE_STYLES


class MissingAPIKeyError(RuntimeError):
    """Raised at process startup when a required API key is not configured."""


class InvalidDateStyleError(RuntimeError):
    """Raised at process startup when DATE_STYLE names no known style."""


def require_api_keys():
    """Fails fast if a key this process needs to serve requests is not set.

    ANTHROPIC_API_KEY is needed in every mode — both the demo and the real
    deployment generate weekly summaries with Claude. NOTION_API_KEY is only
    needed outside DEMO_MODE: demo mode reads fixture data from demo_data.py
    and never imports notion.py's client.

    Called from wsgi.py rather than registered as a Django system check — see
    RequiredApiKeysTest's docstring in tests/test_config.py for why.
    """
    missing = []
    if not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if not settings.DEMO_MODE and not os.environ.get("NOTION_API_KEY"):
        missing.append("NOTION_API_KEY")
    if missing:
        raise MissingAPIKeyError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Set them in the environment or .env file before starting the server."
        )


def require_valid_date_style():
    """Fails fast if DATE_STYLE names no style in date_format.DATE_STYLES.

    date_format raises on an unknown style too, but only once a date is
    rendered, and no deploy check renders one: the healthcheck reads
    /health/, the demo check the landing page, the production check only
    nginx's 401. A typo would deploy green and then fail every dashboard
    request. Raising here keeps the container from ever turning healthy, so
    the deploy itself fails instead.

    An empty value counts as unknown: `DATE_STYLE=` in .env sets an empty
    string rather than falling back to the default.
    """
    if settings.DATE_STYLE not in DATE_STYLES:
        raise InvalidDateStyleError(
            f"DATE_STYLE={settings.DATE_STYLE!r} is not a known date style. "
            f"Expected one of {', '.join(sorted(DATE_STYLES))}."
        )
