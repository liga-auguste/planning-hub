#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys

from dotenv import dotenv_values, load_dotenv

from planning_hub.env import apply_credentials

# See planning_hub/env.py: .env fills in what the environment is missing, and
# its credentials additionally beat what is already there. Switches like
# DEMO_MODE stay overridable per invocation.
#
# No effect on either deployment: .dockerignore keeps .env out of the image,
# so there is no file to read there — compose supplies the variables through
# env_file/environment, and gunicorn serves wsgi.py, which never calls this.
load_dotenv()
apply_credentials(os.environ, dotenv_values())


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "planning_hub.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
