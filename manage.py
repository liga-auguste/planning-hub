#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys

from dotenv import load_dotenv

# override=True, because load_dotenv's default is the opposite of what a
# project-local .env is for: it leaves an already-exported variable alone, so
# a stale ANTHROPIC_API_KEY sitting in the login session (launchctl setenv,
# an old export) silently shadows the working key in .env and every Claude
# call comes back 401 with nothing to point at. The file next to the code is
# the local authority; the environment is not.
#
# No effect on either deployment: .dockerignore keeps .env out of the image,
# so there is no file to read there — compose supplies the variables through
# env_file/environment, and gunicorn serves wsgi.py, which never calls this.
load_dotenv(override=True)


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
