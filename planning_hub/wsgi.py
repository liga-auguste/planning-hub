"""
WSGI config for planning_hub project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "planning_hub.settings")

application = get_wsgi_application()

# Only settings.DEMO_MODE is readable this early, hence the import here rather
# than at module level. See RequiredApiKeysTest in projects/tests.py for why
# this lives in wsgi.py instead of Django's system check framework.
from projects.startup import require_api_keys

require_api_keys()
