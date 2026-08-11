import os

from django.conf import settings


class MissingAPIKeyError(RuntimeError):
    """Raised at process startup when a required API key is not configured."""


def require_api_keys():
    """Fails fast if a key this process needs to serve requests is not set.

    ANTHROPIC_API_KEY is needed in every mode — both the demo and the real
    deployment generate weekly summaries with Claude. NOTION_API_KEY is only
    needed outside DEMO_MODE: demo mode reads fixture data from demo_data.py
    and never imports notion.py's client.

    Called from wsgi.py rather than registered as a Django system check — see
    RequiredApiKeysTest's docstring in tests.py for why.
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
